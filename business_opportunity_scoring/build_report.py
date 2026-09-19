"""
build_report.py
----------------
Scores Santa Clara commercial properties as redevelopment/acquisition
opportunities by combining three signals:

  1. Business signal  — is the business currently operating there a
     "declining" category (appliance repair, video rental, etc.) or
     permanently/temporarily closed? (joined in from scraped_batch*.csv)
  2. Property signal   — ownership/financial/zoning facts already present
     in the main list (vacancy, foreclosure, equity, FAR, lot size, hold
     period), scored with the same weighted-point methodology used in
     palm_property_intelligence/build_database.py.
  3. Visual signal      — a Google Street View Static photo of the site,
     read by Gemini vision and rated for physical condition / visible
     vacancy or disrepair.

Output: a self-contained HTML report (santa_clara_opportunity_report.html)
plus a companion CSV, both written into this folder.

Usage:
    python build_report.py                  # full run
    python build_report.py --limit 15        # quick sample run
    python build_report.py --skip-vision     # skip Gemini (faster, no AI cost)
    python build_report.py --skip-images     # skip Street View + vision entirely
    python build_report.py --workers 6       # concurrency (default 4)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

import config

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
GEMINI_API_KEY = (os.environ.get("GEMINI_API_KEY", "").strip() or GOOGLE_MAPS_API_KEY)

IMAGES_DIR = Path(config.IMAGES_DIR)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

GEMINI_SEMAPHORE = threading.Semaphore(config.GEMINI_CONCURRENCY)
# Once a hard daily-quota response is seen, stop attempting further Gemini
# calls immediately (no retries) instead of burning minutes of backoff per
# property on a quota that won't recover until the next day.
GEMINI_DAILY_QUOTA_EXHAUSTED = threading.Event()

VISION_CACHE_PATH = Path(config.VISION_CACHE_FILE)
VISION_CACHE_LOCK = threading.Lock()
try:
    VISION_CACHE = json.loads(VISION_CACHE_PATH.read_text(encoding="utf-8")) if VISION_CACHE_PATH.exists() else {}
except (json.JSONDecodeError, OSError):
    VISION_CACHE = {}


def save_vision_cache():
    with VISION_CACHE_LOCK:
        VISION_CACHE_PATH.write_text(json.dumps(VISION_CACHE), encoding="utf-8")


# ── Small helpers ──────────────────────────────────────────────────────

def clean(v):
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return str(v).strip()


def boolean(v):
    """Handles 0/1 floats, 'Yes'/'No', True/False, blanks."""
    s = clean(v).lower()
    if s in ("1", "1.0", "yes", "y", "true", "x"):
        return True
    if s in ("", "0", "0.0", "no", "n", "false", "nan"):
        return False
    return False


def number(v):
    try:
        s = re.sub(r"[^0-9.\-]", "", clean(v))
        return float(s) if s not in ("", "-", ".") else None
    except (TypeError, ValueError):
        return None


def normalize_address(address):
    """
    Normalize to 'NUMBER STREETNAME' for matching across sources.
    Mirrors normalize_address() in ../../mls_to_gsheet.py so the same
    matching behavior is reused rather than reinvented.
    """
    addr = clean(address).split(",")[0].strip().upper()
    addr = re.sub(r"[^\w\s]", "", addr)
    suffix_pattern = (
        r"\b(STREET|AVENUE|DRIVE|LANE|WAY|BOULEVARD|ROAD|COURT|PLACE|"
        r"TERRACE|CIRCLE|HIGHWAY|FREEWAY|LOOP|RUN|TRAIL|PASS|PARKWAY|"
        r"ST|AVE|DR|LN|BLVD|RD|CT|PL|TER|CIR|HWY|FWY|TRL|PKWY)\b"
    )
    addr = re.sub(suffix_pattern, "", addr)
    addr = re.sub(r"\s+", " ", addr).strip()
    return addr


def haversine_m(lat1, lng1, lat2, lng2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def safe_key(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:16]


def clean_zip(v):
    """Zip codes come through pandas as floats (e.g. 95050.0) when the
    column has any missing values; strip that to a plain digit string."""
    s = clean(v)
    return s[:-2] if s.endswith(".0") else s


def format_phone(v):
    """Normalize to XXX-XXX-XXXX. Source phone columns are inconsistently
    formatted ("(408) 249-5253", "408.249.5253", raw digits, and — since
    pandas reads unformatted digit-only columns as floats — "3108901741.0").
    Leaves anything that isn't a clean 10 (or 11-with-leading-1) digit US
    number as-is rather than mangling it."""
    s = clean(v)
    if s.endswith(".0"):
        s = s[:-2]
    digits = re.sub(r"\D", "", s)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return clean(v)


# ── Load properties ──────────────────────────────────────────────────

def load_properties():
    df = pd.read_excel(config.PROPERTIES_FILE)
    records = []
    for i, row in df.iterrows():
        lat = number(row.get("LATITUDE"))
        lng = number(row.get("LONGITUDE"))
        lot_sqft = number(row.get("Lot Sqft"))
        building_sqft = number(row.get("Building Sqft"))
        year_built = number(row.get("Year Built"))
        far = (building_sqft / lot_sqft) if (lot_sqft and building_sqft and lot_sqft > 0) else None
        est_value = number(row.get("Estimated Market Value")) or number(row.get("Assessed Value"))
        total_assessed_value = number(row.get("Assessed Value"))
        land_value = number(row.get("Assessed Land Value"))
        # Clamped to 1.0: the multi-source merge (PropertyRadar/LandVision/
        # PropStream) has ~12 rows where Assessed Land Value exceeds total
        # Assessed Value (source data inconsistency, not a real >100% split).
        land_value_ratio = (
            min(1.0, land_value / total_assessed_value)
            if (land_value and total_assessed_value and total_assessed_value > 0) else None
        )
        price_per_sqft = number(row.get("PRICE_PER_SQFT"))
        if not price_per_sqft and est_value and building_sqft:
            price_per_sqft = est_value / building_sqft
        last_sale_date = pd.to_datetime(clean(row.get("Last Sale Date")), errors="coerce")
        years_held = None
        if pd.notna(last_sale_date):
            years_held = max(0, datetime.now().year - last_sale_date.year)
        condition_text = " ".join(
            clean(row.get(c))
            for c in ("Total Condition", "Interior Condition", "Exterior Condition", "Bathroom Condition")
            if clean(row.get(c))
        )
        owner_occ_raw = row.get("Owner Occupied")
        non_owner_raw = row.get("Non-Owner Occ?")
        if clean(owner_occ_raw):
            owner_occupied = boolean(owner_occ_raw)
        elif clean(non_owner_raw):
            owner_occupied = not boolean(non_owner_raw)
        else:
            owner_occupied = None

        vacant_raw = row.get("Vacant")
        vacant = boolean(vacant_raw) if clean(vacant_raw) else None

        phone_number = format_phone(next(
            (row.get(c) for c in (
                "Primary Mobile Phone1", "Primary Phone1", "Phone 1",
                "Secondary Mobile Phone1", "Secondary Phone1", "Trustee Phone",
            ) if clean(row.get(c))),
            "",
        ))
        mail_street = " ".join(filter(None, [clean(row.get("Mailing Address")), clean(row.get("Mailing Unit #"))]))
        mail_city = clean(row.get("Mailing City"))
        mail_state = clean(row.get("Mailing State"))
        mail_zip = clean_zip(row.get("Mailing Zip"))
        mailing_address = ", ".join(filter(None, [mail_street, " ".join(filter(None, [mail_city, mail_state, mail_zip]))]))

        # Structured phone list (with type/DNC metadata) from the flat
        # Phone 1..5 columns — separate from `phone_number` above, which
        # picks the single best-guess contact number across several
        # different source columns that don't carry this metadata.
        phones = []
        for n in range(1, 6):
            num = format_phone(row.get(f"Phone {n}"))
            if num:
                dnc = clean(row.get(f"Phone {n} DNC"))
                phones.append({
                    "number": num,
                    "type": clean(row.get(f"Phone {n} Type")),
                    "status": "DNC" if dnc else "",
                })

        record = {
            "id": i,
            "address": clean(row.get("Address")),
            "city": clean(row.get("City")).title(),
            "state": clean(row.get("State")) or "CA",
            "zip": clean_zip(row.get("Zip")),
            "apn": clean(row.get("APN")),
            "lat": lat,
            "lng": lng,
            "norm_addr": normalize_address(row.get("Address")),
            "lot_sqft": lot_sqft,
            "building_sqft": building_sqft,
            "year_built": int(year_built) if year_built else None,
            "far": far,
            "est_value": est_value,
            "land_value": land_value,
            "land_value_ratio": land_value_ratio,
            "price_per_sqft": price_per_sqft,
            "owner_occupied": owner_occupied,
            "vacant": vacant,
            "foreclosure": boolean(row.get("Foreclosure?")),
            "preforeclosure": boolean(row.get("Preforeclosure?")),
            "auction": boolean(row.get("Auction?")),
            "bank_owned": boolean(row.get("Bank Owned?")),
            "pre_probate": boolean(row.get("Pre-Probate?")),
            "deceased_owner": boolean(row.get("Deceased Owner")),
            "equity_pct": number(row.get("Est Equity %")),
            "years_held": years_held,
            "condition_text": condition_text,
            "property_url": clean(row.get("Property URL")),
            "zoning": clean(row.get("Zoning")),
            "owner": clean(row.get("Owner")) or clean(row.get("Primary Name")),
            "owner_first_name": clean(row.get("Owner 1 First Name")),
            "owner_last_name": clean(row.get("Owner 1 Last Name")),
            "phone_number": phone_number,
            "mailing_address": mailing_address,
            "mail_street": mail_street,
            "mail_city": mail_city,
            "mail_state": mail_state,
            "mail_zip": mail_zip,
            "email1": clean(row.get("Primary Email1")),
            "email2": clean(row.get("Secondary Email1")),
            "phones": phones,
            "zone_code": None,
        }
        records.append(record)
    return records


# ── Load + join zoning ────────────────────────────────────────────────

def load_zoning():
    import geopandas as gpd
    path = Path(config.ZONING_GEOJSON_FILE)
    if not path.exists():
        return None
    gdf = gpd.read_file(path)[["ZONGDSGN", "geometry"]]
    return gdf[gdf["ZONGDSGN"].notna()]


def join_zoning(properties, zoning_gdf):
    """Spatial point-in-polygon join. Santa Clara's zoning data has
    overlapping "combining district" polygons at some locations (e.g. a
    base zone plus a historic-district overlay) — where a property point
    falls inside more than one polygon, keep the larger-area one as the
    primary zone."""
    if zoning_gdf is None or zoning_gdf.empty:
        return 0

    import geopandas as gpd

    have_coords = [p for p in properties if p["lat"] is not None and p["lng"] is not None]
    if not have_coords:
        return 0

    pts = gpd.GeoDataFrame(
        {"pid": [p["id"] for p in have_coords]},
        geometry=gpd.points_from_xy(
            [p["lng"] for p in have_coords], [p["lat"] for p in have_coords]
        ),
        crs="EPSG:4326",
    )

    zoning_gdf = zoning_gdf.reset_index(drop=True)
    zoning_proj = zoning_gdf.to_crs(3857)
    zoning_proj["_area"] = zoning_proj.geometry.area

    joined = gpd.sjoin(pts, zoning_gdf, how="left", predicate="within")
    joined = joined.join(zoning_proj["_area"], on="index_right")
    joined = joined.sort_values("_area", ascending=False).drop_duplicates(subset="pid", keep="first")

    zone_by_pid = dict(zip(joined["pid"], joined["ZONGDSGN"]))
    by_id = {p["id"]: p for p in properties}
    matched = 0
    for pid, zone in zone_by_pid.items():
        if isinstance(zone, str) and zone:
            by_id[pid]["zone_code"] = zone
            matched += 1
    return matched


# ── Load + join businesses ───────────────────────────────────────────

BUSINESS_COLS = [
    "title", "address", "street", "city", "location/lat", "location/lng",
    "categoryName", "categories/0", "categories/1", "categories/2",
    "phone", "website", "totalScore", "reviewsCount",
    "permanentlyClosed", "temporarilyClosed", "placeId",
]


def load_businesses():
    frames = []
    for path in config.BUSINESS_FILES:
        if not Path(path).exists():
            continue
        df = pd.read_csv(path, low_memory=False)
        cols = [c for c in BUSINESS_COLS if c in df.columns]
        frames.append(df[cols])
    if not frames:
        return []
    all_df = pd.concat(frames, ignore_index=True)
    all_df = all_df.drop_duplicates(subset=["placeId"]) if "placeId" in all_df.columns else all_df.drop_duplicates()

    businesses = []
    for _, row in all_df.iterrows():
        street = clean(row.get("street")) or clean(row.get("address"))
        lat = number(row.get("location/lat"))
        lng = number(row.get("location/lng"))
        categories = [clean(row.get(c)) for c in ("categoryName", "categories/0", "categories/1", "categories/2")]
        categories = [c for c in categories if c]
        businesses.append({
            "title": clean(row.get("title")),
            "norm_addr": normalize_address(street),
            "lat": lat,
            "lng": lng,
            "category": clean(row.get("categoryName")),
            "categories_text": " ".join(categories).lower(),
            "permanently_closed": boolean(row.get("permanentlyClosed")),
            "temporarily_closed": boolean(row.get("temporarilyClosed")),
            "reviews_count": number(row.get("reviewsCount")),
            "rating": number(row.get("totalScore")),
        })
    return businesses


def join_businesses(properties, businesses):
    by_addr = {}
    for b in businesses:
        by_addr.setdefault(b["norm_addr"], []).append(b)

    geo_businesses = [b for b in businesses if b["lat"] is not None and b["lng"] is not None]

    matched_count = 0
    fallback_count = 0
    for p in properties:
        matches = by_addr.get(p["norm_addr"], [])
        if matches:
            matched_count += 1
        elif p["lat"] is not None and p["lng"] is not None and geo_businesses:
            near = []
            for b in geo_businesses:
                d = haversine_m(p["lat"], p["lng"], b["lat"], b["lng"])
                if d <= config.BUSINESS_MATCH_MAX_METERS:
                    near.append((d, b))
            if near:
                near.sort(key=lambda x: x[0])
                matches = [b for _, b in near[:5]]
                fallback_count += 1
        p["businesses"] = matches
    print(f"  Business join: {matched_count} matched by address, {fallback_count} matched by proximity, "
          f"{len(properties) - matched_count - fallback_count} unmatched")
    return properties


# ── Business classification ──────────────────────────────────────────

def classify_business(businesses):
    if not businesses:
        return "unknown", "No matching business record found for this address", None

    names = [b["title"] for b in businesses if b["title"]]

    closed = [b for b in businesses if b["permanently_closed"]]
    if closed:
        return "declining", f"Permanently closed business on site: {closed[0]['title']}", closed[0]

    temp_closed = [b for b in businesses if b["temporarily_closed"]]
    if temp_closed:
        return "declining", f"Temporarily closed business on site: {temp_closed[0]['title']}", temp_closed[0]

    for b in businesses:
        text = f"{b['title']} {b['categories_text']}".lower()
        if any(ex in text for ex in config.DECLINING_EXCLUDE_KEYWORDS):
            continue
        hit = next((kw for kw in config.DECLINING_BUSINESS_KEYWORDS if kw in text), None)
        if hit:
            return "declining", f"Declining-category business on site: {b['title']} ({b['category'] or hit})", b

    top = businesses[0]
    label = f"{top['title']} ({top['category']})" if top["category"] else top["title"]
    return "neutral", f"Active business on site: {label}", top


BASE_BUSINESS_SCORE = {"declining": 85.0, "neutral": 35.0}


def refine_with_reviews(label, business):
    """
    Adjusts the category/closed-based label + score using Google review
    volume and rating: a business with few reviews and a low rating reads
    as struggling regardless of category; a business with many reviews and
    a high rating reads as healthy even if its category matched a
    "declining" keyword (e.g. a busy, well-reviewed auto repair shop isn't
    actually a good target just because "auto repair" is on the list).
    Returns (final_label, business_score, review_note).
    """
    if label == "unknown":
        return label, None, ""

    base = BASE_BUSINESS_SCORE[label]
    reviews = business.get("reviews_count") if business else None
    rating = business.get("rating") if business else None
    if reviews is None or rating is None:
        return label, base, ""

    low = reviews <= config.REVIEW_COUNT_LOW and rating <= config.RATING_LOW
    high = reviews >= config.REVIEW_COUNT_HIGH and rating >= config.RATING_HIGH

    if low:
        final_label = "declining"
        score = min(100.0, base + 10) if label == "declining" else 90.0
        note = (
            f"Low review volume ({int(reviews)}) and rating ({rating:.1f}/5) reinforce declining signal"
            if label == "declining" else
            f"Low review volume ({int(reviews)}) and rating ({rating:.1f}/5) suggest a struggling business"
        )
        return final_label, score, note

    if high:
        final_label = "neutral" if label == "declining" else label
        score = max(15.0, base - 45) if label == "declining" else 30.0
        note = (
            f"High review volume ({int(reviews)}) and rating ({rating:.1f}/5) suggest a thriving business despite category match"
            if label == "declining" else
            f"High review volume ({int(reviews)}) and rating ({rating:.1f}/5) confirm a healthy business"
        )
        return final_label, score, note

    return label, base, ""


# ── Property/financial scoring ───────────────────────────────────────

def compute_ppsf_median(properties):
    vals = [p["price_per_sqft"] for p in properties if p["price_per_sqft"]]
    vals.sort()
    if not vals:
        return 0
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


def score_property_info(p, median_ppsf):
    """Ownership/financial signal: is this a motivated seller at a good price?"""
    parts = {}
    parts["Vacant"] = 20 if p["vacant"] else 0
    parts["Absentee owner"] = 12 if p["owner_occupied"] is False else 0
    parts["Foreclosure/distress"] = 20 if (p["foreclosure"] or p["preforeclosure"] or p["auction"] or p["bank_owned"]) else 0
    parts["Pre-probate/estate"] = 15 if (p["pre_probate"] or p["deceased_owner"]) else 0
    if p["equity_pct"] is not None:
        parts["High equity"] = 15 if p["equity_pct"] >= 70 else (8 if p["equity_pct"] >= 40 else 0)
    if p["price_per_sqft"] and median_ppsf:
        parts["Below-median $/sqft"] = 15 if p["price_per_sqft"] <= median_ppsf * 0.8 else (8 if p["price_per_sqft"] <= median_ppsf * 0.95 else 0)
    if p["years_held"] is not None:
        parts["Long ownership"] = 10 if p["years_held"] >= 15 else (5 if p["years_held"] >= 8 else 0)
    if p["condition_text"] and re.search(r"POOR|FAIR|FIXER|NEEDS|DEFERRED|BELOW AVERAGE", p["condition_text"], re.I):
        parts["Reported poor condition"] = 15

    score = min(100, sum(parts.values()))
    reasons = [k for k, v in parts.items() if v]

    available = sum([
        p["equity_pct"] is not None, bool(p["price_per_sqft"]), p["years_held"] is not None,
        p["vacant"] is not None, p["owner_occupied"] is not None,
    ])
    return score, reasons, available / 5.0


# ── Buildability scoring ─────────────────────────────────────────────

def compute_lot_percentiles(properties):
    vals = sorted(p["lot_sqft"] for p in properties if p["lot_sqft"])
    if not vals:
        return 0, 0

    def pct(q):
        return vals[min(len(vals) - 1, round((len(vals) - 1) * q))]

    return pct(0.75), pct(0.90)


def land_use_hint(businesses):
    for b in businesses or []:
        text = f"{b['title']} {b['categories_text']}".lower()
        hit = next((kw for kw in config.LAND_USE_HINT_KEYWORDS if kw in text), None)
        if hit:
            return b["category"] or hit
    return None


def score_buildability(p, lot_p75, lot_p90):
    """Physical redevelopment upside: is the land underused relative to
    what's built on it? Independent of whether the current tenant business
    is healthy or struggling (that's the business-value layer)."""
    parts = {}

    if p["far"] is not None:
        parts["Low FAR (underbuilt lot)"] = 30 if p["far"] <= config.FAR_LOW else (15 if p["far"] <= config.FAR_MODERATE else 0)
    if p["land_value_ratio"] is not None:
        parts["Land value dominates improvement"] = (
            25 if p["land_value_ratio"] >= config.LAND_VALUE_RATIO_HIGH else
            (12 if p["land_value_ratio"] >= config.LAND_VALUE_RATIO_MODERATE else 0)
        )
    if p["lot_sqft"]:
        parts["Large lot (dataset-relative)"] = 20 if p["lot_sqft"] >= lot_p90 else (10 if p["lot_sqft"] >= lot_p75 else 0)
    if p["year_built"]:
        parts["Old building"] = 10 if p["year_built"] <= config.OLD_BUILDING_YEAR else (5 if p["year_built"] <= config.AGING_BUILDING_YEAR else 0)

    zone_fit = config.ZONE_TOWNHOME_FIT.get(p["zone_code"]) if p["zone_code"] else None
    if zone_fit is not None:
        parts[f"Zoned {p['zone_code']} (townhome fit)"] = zone_fit

    hint = None
    if p["far"] is None and p["building_sqft"] is None:
        hint = land_use_hint(p["businesses"])
        if hint:
            parts[f"Land-use hint: {hint}"] = 20

    score = min(100, sum(parts.values()))
    reasons = [k for k, v in parts.items() if v]

    available = sum([
        p["far"] is not None, p["land_value_ratio"] is not None,
        bool(p["lot_sqft"]), bool(p["year_built"]), zone_fit is not None, bool(hint),
    ])
    return score, reasons, available / 6.0


# ── Street View ───────────────────────────────────────────────────────

def street_view_location_param(p):
    if p["lat"] is not None and p["lng"] is not None:
        return f"{p['lat']},{p['lng']}"
    return f"{p['address']}, {p['city']}, CA {p['zip']}".strip()


def fetch_street_view(p, session):
    key = safe_key(p["norm_addr"] + "_" + str(p["id"]))
    img_path = IMAGES_DIR / f"{key}.jpg"
    if img_path.exists():
        return str(img_path), "OK"

    if not GOOGLE_MAPS_API_KEY:
        return None, "NO_API_KEY"

    location = street_view_location_param(p)
    try:
        meta = session.get(
            "https://maps.googleapis.com/maps/api/streetview/metadata",
            params={"location": location, "key": GOOGLE_MAPS_API_KEY},
            timeout=20,
        ).json()
    except requests.RequestException as e:
        return None, f"METADATA_ERROR: {e}"

    status = meta.get("status", "UNKNOWN")
    if status != "OK":
        return None, status

    try:
        resp = session.get(
            "https://maps.googleapis.com/maps/api/streetview",
            params={
                "size": config.STREET_VIEW_IMAGE_SIZE,
                "location": location,
                "fov": config.STREET_VIEW_FOV,
                "pitch": config.STREET_VIEW_PITCH,
                "key": GOOGLE_MAPS_API_KEY,
            },
            timeout=20,
        )
        resp.raise_for_status()
        img_path.write_bytes(resp.content)
        return str(img_path), "OK"
    except requests.RequestException as e:
        return None, f"IMAGE_ERROR: {e}"


# ── Gemini vision ─────────────────────────────────────────────────────

VISION_PROMPT = """You are screening a commercial property photo for a real-estate investor \
looking for redevelopment/acquisition opportunities. Rate the PHYSICAL CONDITION of the \
building and site visible in this Google Street View photo.

Property context: {context}

Respond with ONLY a JSON object, no markdown, matching this schema:
{{"condition_rating": "poor" | "fair" | "good" | "excellent",
  "visible_vacancy_or_disrepair": true | false,
  "notes": "one short sentence describing what you see"}}
"""


def call_gemini_vision(image_path, context_text, session):
    if not GEMINI_API_KEY:
        return None, "NO_API_KEY"

    try:
        img_bytes = Path(image_path).read_bytes()
    except OSError as e:
        return None, f"READ_ERROR: {e}"

    b64 = base64.b64encode(img_bytes).decode("ascii")
    prompt = VISION_PROMPT.format(context=context_text)
    body = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
            ]
        }],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    if GEMINI_DAILY_QUOTA_EXHAUSTED.is_set():
        return None, "QUOTA_EXHAUSTED"

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent"

    last_err = "UNKNOWN"
    with GEMINI_SEMAPHORE:
        if GEMINI_DAILY_QUOTA_EXHAUSTED.is_set():
            return None, "QUOTA_EXHAUSTED"
        for attempt in range(config.GEMINI_MAX_RETRIES + 1):
            try:
                resp = session.post(
                    url, params={"key": GEMINI_API_KEY}, json=body,
                    timeout=config.GEMINI_REQUEST_TIMEOUT,
                )
                if resp.status_code == 429:
                    body_text = resp.text
                    if "free_tier_requests" in body_text or "RESOURCE_EXHAUSTED" in body_text:
                        # Hard daily quota, not a transient burst — retrying
                        # within this run won't help, so stop trying entirely.
                        GEMINI_DAILY_QUOTA_EXHAUSTED.set()
                        return None, "QUOTA_EXHAUSTED"
                    last_err = "RATE_LIMITED"
                    if attempt < config.GEMINI_MAX_RETRIES:
                        time.sleep(min(30, 5 * (2 ** attempt)))
                    continue
                if resp.status_code == 503:
                    last_err = "MODEL_OVERLOADED"
                    if attempt < config.GEMINI_MAX_RETRIES:
                        time.sleep(min(30, 5 * (2 ** attempt)))
                    continue
                if resp.status_code == 404:
                    # Model retired/unavailable for this account — permanent,
                    # retrying won't help.
                    return None, "MODEL_NOT_FOUND"
                resp.raise_for_status()
                data = resp.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    # Some models (e.g. Gemma) ignore responseMimeType and
                    # emit a reasoning preamble before a ```json fenced
                    # block — pull the last {...} object out of the text.
                    blocks = re.findall(r"\{[^{}]*\}", text, re.S)
                    if not blocks:
                        raise
                    parsed = json.loads(blocks[-1])
                return parsed, "OK"
            except (requests.RequestException, KeyError, IndexError, json.JSONDecodeError) as e:
                last_err = f"{type(e).__name__}: {e}"
                if attempt < config.GEMINI_MAX_RETRIES:
                    time.sleep(min(30, 3 * (2 ** attempt)))
    return None, last_err


def score_visual(vision_result):
    if not vision_result:
        return None
    rating = str(vision_result.get("condition_rating", "")).lower()
    base = {"poor": 90, "fair": 60, "good": 25, "excellent": 10}.get(rating)
    if base is None:
        return None
    if vision_result.get("visible_vacancy_or_disrepair"):
        base = min(100, base + 15)
    return base


# ── Combine ────────────────────────────────────────────────────────────

def combine_scores(business_score, business_cov, property_score, property_cov,
                    buildability_score, buildability_cov, visual_score, visual_cov):
    """Blends the 4 layers into one score, plus a `data_coverage` figure.

    data_coverage is NOT a probability that this property will sell, get
    approved, or be a good deal — it's how much of the underlying scoring
    data we actually had to work with, weighted by each layer's importance
    and (for Property/Buildability, which each check several independent
    facts) how many of THOSE individual facts were present. A property can
    have 100% data coverage and still be a poor opportunity, or low
    coverage and still be a good one we're just less sure about."""
    signals = [
        (business_score, config.WEIGHT_BUSINESS, business_cov),
        (property_score, config.WEIGHT_PROPERTY, property_cov),
        (buildability_score, config.WEIGHT_BUILDABILITY, buildability_cov),
        (visual_score, config.WEIGHT_VISUAL, visual_cov),
    ]
    available = [(s, w, c) for s, w, c in signals if s is not None]
    if not available:
        return 0.0, 0.0
    weight_sum = sum(w for _, w, _ in available)
    weighted_avg = sum(s * w for s, w, _ in available) / weight_sum
    # Missing layers contribute 0 coverage; present layers contribute their
    # own internal completeness (1.0 for business/visual, which are each a
    # single fact once matched, or a fraction for property/buildability).
    data_coverage = sum(w * c for _, w, c in available) / sum(
        (config.WEIGHT_BUSINESS, config.WEIGHT_PROPERTY, config.WEIGHT_BUILDABILITY, config.WEIGHT_VISUAL)
    )
    multiplier = config.CONFIDENCE_FLOOR + config.CONFIDENCE_RANGE * data_coverage
    return round(weighted_avg * multiplier, 1), round(data_coverage, 2)


# ── Per-property pipeline ────────────────────────────────────────────

def process_property(p, median_ppsf, lot_p75, lot_p90, session, skip_vision, skip_images):
    raw_label, business_reason, top_business = classify_business(p["businesses"])
    label, business_score, review_note = refine_with_reviews(raw_label, top_business)
    if review_note:
        business_reason = f"{business_reason} — {review_note}"

    property_score, property_reasons, property_cov = score_property_info(p, median_ppsf)
    buildability_score, buildability_reasons, buildability_cov = score_buildability(p, lot_p75, lot_p90)
    business_cov = 1.0 if business_score is not None else 0.0

    image_path, image_status, vision_result, vision_status = None, "SKIPPED", None, "SKIPPED"
    if not skip_images:
        image_path, image_status = fetch_street_view(p, session)
        if image_path and not skip_vision:
            cache_key = str(p["id"])
            cached = VISION_CACHE.get(cache_key)
            if cached:
                vision_result, vision_status = cached, "CACHED"
            else:
                context = (
                    f"{p['address']}, {p['city']}, CA {p['zip']}. "
                    f"Business on site: {business_reason}. "
                    f"Lot {p['lot_sqft'] or 'unknown'} sqft, building {p['building_sqft'] or 'unknown'} sqft, "
                    f"built {p['year_built'] or 'unknown'}."
                )
                vision_result, vision_status = call_gemini_vision(image_path, context, session)
                if vision_status == "OK":
                    with VISION_CACHE_LOCK:
                        VISION_CACHE[cache_key] = vision_result
                    save_vision_cache()

    visual_score = score_visual(vision_result)
    visual_cov = 1.0 if visual_score is not None else 0.0

    opportunity_score, data_coverage = combine_scores(
        business_score, business_cov, property_score, property_cov,
        buildability_score, buildability_cov, visual_score, visual_cov,
    )

    reasons = []
    if label != "unknown":
        reasons.append(business_reason)
    if property_reasons:
        reasons.append("Property signals: " + "; ".join(property_reasons))
    if buildability_reasons:
        reasons.append("Buildability: " + "; ".join(buildability_reasons))
    if vision_result and vision_result.get("notes"):
        reasons.append(f"Visual: {vision_result['notes']}")

    return {
        **{k: p[k] for k in (
            "id", "address", "city", "state", "zip", "apn", "lat", "lng", "lot_sqft", "building_sqft",
            "year_built", "est_value", "land_value", "land_value_ratio", "price_per_sqft",
            "property_url", "zoning", "zone_code", "owner", "owner_first_name", "owner_last_name",
            "phone_number", "mailing_address", "mail_street", "mail_city", "mail_state", "mail_zip",
            "email1", "email2", "phones",
        )},
        "zone_info": config.ZONE_INFO.get(p["zone_code"], "") if p["zone_code"] else "",
        "business_label": label,
        "business_name": top_business["title"] if top_business else "",
        "business_category": top_business["category"] if top_business else "",
        "business_reviews": top_business["reviews_count"] if top_business else None,
        "business_rating": top_business["rating"] if top_business else None,
        "business_score": business_score,
        "property_score": property_score,
        "buildability_score": buildability_score,
        "visual_score": visual_score,
        "data_coverage": data_coverage,
        "opportunity_score": opportunity_score,
        "image_path": (Path(image_path).relative_to(ROOT).as_posix() if image_path else None),
        "image_status": image_status,
        "vision_status": vision_status,
        "vision_notes": (vision_result or {}).get("notes", ""),
        "condition_rating": (vision_result or {}).get("condition_rating", ""),
        "reasons": " | ".join(reasons),
    }


# ── HTML report ───────────────────────────────────────────────────────

HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PropScore AI</title>
<link rel="icon" type="image/png" href="data:image/png;base64,{favicon_b64}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Trirong:wght@400;600;700&family=Manrope:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
/* AlphaX RE Capital brand palette, pulled from alphax-capital.com:
   navy #082F7B (headings/header), accent blue #1A6AFF (links/CTAs),
   sage green #6B9E72 (their own secondary accent, used here for "high
   opportunity"), cream #FCF9F5 (light text on navy). Headings use their
   actual brand font (Trirong, serif, on Google Fonts); body uses Manrope
   as a close open substitute for their proprietary Wix Madefor font,
   which isn't available outside Wix. */
:root{{--navy:#082F7B;--blue:#1A6AFF;--ink:#1c2637;--muted:#5c6880;--paper:#F6FBFF;--card:#fff;--line:#dce2ea;--sage:#4f7a57;--sage-bg:#e1f0e0;--cream:#FCF9F5;--amber:#c98532;--shadow:0 12px 32px rgba(8,47,123,.08)}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 Manrope,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif}}
h1,h2{{font-family:Trirong,Georgia,serif}}
header{{padding:28px clamp(20px,5vw,72px);background:var(--navy);color:var(--cream)}}
.header-brand{{display:flex;align-items:center;gap:16px}}
.logo{{height:clamp(40px,6vw,56px);width:auto;display:block}}
h1{{font-weight:700;font-size:clamp(26px,4vw,40px);line-height:1.05;margin:3px 0}}
.eyebrow{{font-size:11px;letter-spacing:.18em;color:#a9c2f2;margin:0}}
.subtitle{{color:#d7e2f7;margin:7px 0 0}}
main{{max-width:1500px;margin:auto;padding:24px clamp(14px,3vw,44px) 56px}}
.notice{{background:#fff7e9;border-left:4px solid var(--amber);padding:12px 16px;margin-bottom:18px;border-radius:0 9px 9px 0;color:#5e4a2e}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}}
.filters{{padding:18px;margin-bottom:18px}}
.filter-grid{{display:grid;grid-template-columns:repeat(7,minmax(120px,1fr));gap:12px;align-items:end}}
label,.field{{display:flex;flex-direction:column;gap:5px;font-size:12px;color:var(--muted);font-weight:700}}
.field span{{visibility:hidden}}
input,select{{width:100%;padding:9px 10px;border:1px solid #c9d3e0;border-radius:8px;background:#fff;color:var(--ink);font:inherit}}
button{{font:inherit;cursor:pointer;border:0}}
.ghost{{background:#eaf1fc;border:1px solid var(--line);padding:9px 12px;border-radius:8px;color:var(--navy);font-weight:600;width:100%}}
.primary{{background:var(--blue);border:1px solid var(--blue);padding:9px 14px;border-radius:8px;color:#fff;font-weight:700}}
.primary:disabled{{opacity:.45;cursor:not-allowed}}
.kpis{{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin:0 0 18px}}
.kpis article{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:15px 17px}}
.kpis span{{color:var(--muted);font-size:12px}}
.kpis strong{{display:block;font:600 26px/1.2 Trirong,Georgia,serif;margin-top:5px;color:var(--navy)}}
.table-card{{overflow:hidden}}
.table-head{{display:flex;align-items:end;justify-content:space-between;padding:18px 20px;border-bottom:1px solid var(--line)}}
h2{{font-weight:600;font-size:22px;margin:0;color:var(--navy)}}
.table-wrap{{overflow-x:auto}}
table{{border-collapse:collapse;width:100%;min-width:1300px}}
th{{position:sticky;top:0;background:#eaf1fc;color:var(--navy);text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.04em;padding:10px;border-bottom:1px solid var(--line);cursor:pointer;z-index:1}}
th:first-child,td:first-child{{width:34px;cursor:default}}
.property-cell{{width:170px;max-width:170px}}
.business-cell{{width:150px;max-width:150px}}
.property-cell .address,.business-cell .sub{{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:170px}}
td{{padding:10px;border-bottom:1px solid #e8edf4;vertical-align:top}}
tbody tr{{cursor:pointer}}
tbody tr:hover{{background:#f2f7fd}}
.thumb{{width:96px;height:64px;object-fit:cover;border-radius:6px;background:#e3e9f2;display:block;cursor:zoom-in}}
#imgLightbox{{background:transparent;box-shadow:none;padding:0;width:min(90vw,900px);border:0}}
#imgLightbox::backdrop{{background:rgba(8,20,45,.85)}}
#imgLightbox img{{width:100%;border-radius:10px;display:block}}
#imgLightbox figcaption{{color:#fff;text-align:center;margin-top:10px;font-weight:600}}
#imgLightbox .close{{position:absolute;top:10px;right:10px;width:36px;height:36px;border-radius:50%;background:rgba(8,20,45,.7);color:#fff;font-size:22px;line-height:1;display:grid;place-items:center;padding:0}}
.address{{font-weight:750}}
.sub{{font-size:12px;color:var(--muted);margin-top:2px}}
.sub a{{color:var(--blue)}}
.score{{display:inline-grid;place-items:center;min-width:44px;padding:6px 8px;border-radius:999px;font-weight:750;background:#eaf1fc;color:var(--navy)}}
.score.high{{background:var(--sage-bg);color:var(--sage)}}
.score.mid{{background:#fae9c8;color:#80551e}}
.chip{{display:inline-block;padding:3px 8px;border-radius:999px;font-size:11px;font-weight:700}}
.chip.declining{{background:#f9d9d9;color:#8a2323}}
.chip.neutral{{background:#eaf1fc;color:var(--muted)}}
.chip.unknown{{background:#eef1f6;color:#9aa8bb}}
.reason{{min-width:420px;color:#3a4658;font-size:12.5px}}
dialog{{border:0;border-radius:16px;box-shadow:0 30px 80px rgba(8,47,123,.25);width:min(720px,92vw);padding:26px;color:var(--ink)}}
dialog::backdrop{{background:rgba(8,20,45,.55)}}
dialog .close{{position:absolute;right:14px;top:10px;background:none;font-size:28px;color:var(--muted)}}
dialog h2{{margin:2px 0}}
.detail-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:16px 0}}
.detail-grid div{{background:var(--paper);padding:12px;border-radius:9px}}
.detail-grid span{{display:block;color:var(--muted);font-size:11px}}
.detail-grid strong{{font-size:16px}}
.detail-contact{{background:var(--paper);padding:12px;border-radius:9px;margin-top:6px}}
.detail-contact span{{display:block;color:var(--muted);font-size:11px}}
.reason-box{{padding:12px;background:var(--paper);border-radius:9px;margin-top:9px}}
@media(max-width:1100px){{.filter-grid{{grid-template-columns:repeat(3,1fr)}}.kpis{{grid-template-columns:repeat(3,1fr)}}.detail-grid{{grid-template-columns:repeat(2,1fr)}}}}
</style>
</head>
<body>
<header>
  <div class="header-brand">
    <img class="logo" src="data:image/png;base64,{logo_b64}" alt="PropScore AI logo">
    <div>
      <p class="eyebrow">AI-POWERED PROPERTY SCREENING</p>
      <h1>PropScore AI</h1>
      <p class="subtitle" id="subtitle">All Cities · Business Property Scoring</p>
    </div>
  </div>
</header>
<main>
  <section class="filters card">
    <div class="filter-grid">
      <label>City<select id="city"><option value="">All cities</option></select></label>
      <label>Zipcode<select id="zip"><option value="">All zipcodes</option></select></label>
      <label>Address<input id="search" placeholder="Address"></label>
      <label>Minimum lot size (sqft)<input id="min_lot" type="number" min="0" step="1000" placeholder="Any"></label>
      <label>Minimum score<input id="min_score" type="number" min="0" max="100" value="0"></label>
      <label>Sort by<select id="sort">
        <option value="opportunity_score">Total Score</option>
        <option value="business_score">Business Value</option>
        <option value="property_score">Property Info</option>
        <option value="buildability_score">Buildability</option>
        <option value="visual_score">Visual Condition</option>
        <option value="data_coverage">Data Coverage</option>
        <option value="lot_sqft">Lot Size</option>
      </select></label>
      <div class="field"><span>&nbsp;</span><button id="resetBtn" class="ghost">Reset filters</button></div>
    </div>
  </section>
  <section class="kpis">
    <article><span>Matching Properties</span><strong id="kpiCount">—</strong></article>
    <article><span>Avg. Score</span><strong id="kpiAvg">—</strong></article>
    <article><span>Avg. Business Value</span><strong id="kpiBusiness">—</strong></article>
    <article><span>Avg. Seller Motivation</span><strong id="kpiProperty">—</strong></article>
    <article><span>Avg. Buildability</span><strong id="kpiBuildability">—</strong></article>
    <article><span>Avg. Visual Condition</span><strong id="kpiVisual">—</strong></article>
  </section>
  <section class="card table-card">
    <div class="table-head">
      <div><h2>Ranked properties</h2><p id="resultMeta" class="sub">Loading…</p></div>
      <button id="exportBtn" class="primary" disabled>Export selected CSV (<span id="selCount">0</span>)</button>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th><input type="checkbox" id="selectAll"></th>
          <th data-k="opportunity_score">Rank</th><th data-k="image_path">Photo</th>
          <th data-k="address">Property</th><th data-k="business_name">Business</th>
          <th data-k="opportunity_score">Score</th><th data-k="business_score">Biz</th>
          <th data-k="property_score">Prop</th><th data-k="buildability_score">Build</th>
          <th data-k="visual_score">Visual</th>
          <th data-k="data_coverage" title="Share of the underlying scoring data we actually had for this property — not a probability it will sell or get approved.">Data Coverage</th>
          <th data-k="reasons">Key reasons</th>
        </tr></thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
  </section>
</main>
<dialog id="detail"><button class="close" aria-label="Close">×</button><div id="detailBody"></div></dialog>
<dialog id="imgLightbox"><button class="close" aria-label="Close">×</button><figure style="margin:0"><img id="lightboxImg" src="" alt=""><figcaption id="lightboxCaption"></figcaption></figure></dialog>
<script>
const DATA = {data_json};
const $=s=>document.querySelector(s);
const num=v=>v===null||v===undefined||v===''?'—':(typeof v==='number'?v.toLocaleString():v);
const money=v=>v?new Intl.NumberFormat('en-US',{{style:'currency',currency:'USD',maximumFractionDigits:0}}).format(v):'—';
const lotSize=v=>v?`${{num(v)}} sqft (${{(v/43560).toFixed(2)}} ac)`:'—';
function avg(rows,key){{
  const vals = rows.map(r=>r[key]).filter(v=>v!==null&&v!==undefined);
  return vals.length ? vals.reduce((a,b)=>a+b,0)/vals.length : null;
}}
const rowKey=r=>r.apn||r.address;
const selected=new Set();
let sortKey=null, sortAsc=false; // null = default order (opportunity_score, high to low), not yet explicitly clicked
function sortRows(a,b){{
  const key = sortKey || 'opportunity_score';
  let av=a[key], bv=b[key];
  if (typeof av==='string' || typeof bv==='string') {{
    av=(av||'').toLowerCase(); bv=(bv||'').toLowerCase();
  }} else {{
    av = (av===null||av===undefined)?-Infinity:av;
    bv = (bv===null||bv===undefined)?-Infinity:bv;
  }}
  const r = av<bv?-1:av>bv?1:0;
  return sortAsc?r:-r;
}}
function updateSortIndicators(){{
  const active = sortKey || 'opportunity_score';
  document.querySelectorAll('th[data-k]').forEach(th=>{{
    th.textContent = th.dataset.label + (th.dataset.k===active ? (sortAsc?' ▲':' ▼') : '');
  }});
}}
function apply(){{
  const city=$('#city').value, zip=$('#zip').value, q=$('#search').value.toLowerCase(),
        minScore=+$('#min_score').value||0, minLot=+$('#min_lot').value||0;
  let rows = DATA.filter(r=>
    (!city||r.city===city) &&
    (!zip||r.zip===zip) &&
    (r.opportunity_score>=minScore) &&
    (!minLot || (r.lot_sqft||0)>=minLot) &&
    (!q || r.address.toLowerCase().includes(q))
  );
  rows.sort(sortRows);
  $('#subtitle').textContent = `${{city||'All Cities'}} · Business Property Scoring`;
  updateSortIndicators();
  render(rows);
}}
function render(rows){{
  $('#resultMeta').textContent = `Showing ${{rows.length}} of ${{DATA.length}}`;
  $('#kpiCount').textContent = rows.length.toLocaleString();
  const fmt=v=>v===null?'—':v.toFixed(1);
  $('#kpiAvg').textContent = fmt(avg(rows,'opportunity_score'));
  $('#kpiBusiness').textContent = fmt(avg(rows,'business_score'));
  $('#kpiProperty').textContent = fmt(avg(rows,'property_score'));
  $('#kpiBuildability').textContent = fmt(avg(rows,'buildability_score'));
  $('#kpiVisual').textContent = fmt(avg(rows,'visual_score'));
  $('#rows').innerHTML = rows.map((r,i)=>`<tr data-key="${{rowKey(r).replace(/"/g,'&quot;')}}">
    <td><input type="checkbox" class="rowCheck" ${{selected.has(rowKey(r))?'checked':''}}></td>
    <td>${{i+1}}</td>
    <td>${{r.image_path?`<img class="thumb" loading="lazy" src="${{r.image_path}}" data-caption="${{r.address}}" alt="">`:'<div class="thumb"></div>'}}</td>
    <td class="property-cell"><div class="address" title="${{r.address}}">${{r.address}}</div><div class="sub">${{r.city}} ${{r.zip}}${{r.apn?` · ${{r.apn}}`:''}}</div>${{r.property_url?`<div class="sub"><a href="${{r.property_url}}" target="_blank" rel="noreferrer">Property record ↗</a></div>`:''}}</td>
    <td class="business-cell"><span class="chip ${{r.business_label}}">${{r.business_label}}</span><div class="sub" title="${{r.business_name||''}}">${{r.business_name||'—'}}${{r.business_category?` · ${{r.business_category}}`:''}}</div>${{r.business_rating?`<div class="sub">${{r.business_rating.toFixed(1)}}★ (${{num(r.business_reviews)}} reviews)</div>`:''}}</td>
    <td><span class="score ${{r.opportunity_score>=60?'high':r.opportunity_score>=40?'mid':''}}">${{r.opportunity_score.toFixed(1)}}</span></td>
    <td>${{r.business_score===null?'—':r.business_score.toFixed(0)}}</td>
    <td>${{r.property_score.toFixed(0)}}</td>
    <td>${{r.buildability_score.toFixed(0)}}</td>
    <td>${{r.visual_score===null?'—':r.visual_score.toFixed(0)}}</td>
    <td>${{Math.round(r.data_coverage*100)}}%</td>
    <td class="reason">${{r.reasons||'—'}}</td>
  </tr>`).join('');
  const byKey = Object.fromEntries(rows.map(r=>[rowKey(r),r]));
  document.querySelectorAll('#rows tr').forEach(tr=>{{
    const key = tr.dataset.key;
    tr.querySelector('.rowCheck').addEventListener('change',e=>{{
      if (e.target.checked) selected.add(key); else selected.delete(key);
      updateSelection();
    }});
    tr.addEventListener('click',e=>{{
      if (e.target.closest('.rowCheck')||e.target.closest('a')||e.target.closest('.thumb')) return;
      detail(byKey[key]);
    }});
    const thumb = tr.querySelector('img.thumb');
    if (thumb) thumb.addEventListener('click',e=>{{
      e.stopPropagation();
      showImage(thumb.src, thumb.dataset.caption);
    }});
  }});
  updateSelection();
  $('#selectAll').checked = rows.length>0 && rows.every(r=>selected.has(rowKey(r)));
}}
function updateSelection(){{
  $('#selCount').textContent = selected.size;
  $('#exportBtn').disabled = selected.size===0;
}}
function showImage(src,caption){{
  $('#lightboxImg').src=src;
  $('#lightboxCaption').textContent=caption;
  $('#imgLightbox').showModal();
}}
function detail(r){{
  $('#detailBody').innerHTML = `<p class="eyebrow">${{r.city.toUpperCase()}} · ${{r.zip}}</p><h2>${{r.address}}</h2><p class="sub">${{r.apn?`APN ${{r.apn}}`:''}}</p>
  <div class="detail-grid">
    <div><span>Opportunity score</span><strong>${{r.opportunity_score.toFixed(1)}}</strong></div>
    <div><span>Business value</span><strong>${{r.business_score===null?'—':r.business_score.toFixed(0)}}</strong></div>
    <div><span>Seller motivation</span><strong>${{r.property_score.toFixed(0)}}</strong></div>
    <div><span>Buildability</span><strong>${{r.buildability_score.toFixed(0)}}</strong></div>
    <div><span>Visual condition</span><strong>${{r.visual_score===null?'—':r.visual_score.toFixed(0)}}</strong></div>
    <div title="Share of scoring data available, not a probability of selling or getting approved"><span>Data coverage</span><strong>${{Math.round(r.data_coverage*100)}}%</strong></div>
    <div><span>Lot size</span><strong>${{lotSize(r.lot_sqft)}}</strong></div>
    <div><span>Building size</span><strong>${{num(r.building_sqft)}} sqft</strong></div>
    <div><span>Est. value</span><strong>${{money(r.est_value)}}</strong></div>
    <div><span>Zoning</span><strong>${{r.zone_code||'—'}}</strong></div>
  </div>
  <div class="detail-contact">
    <span>Owner</span><strong>${{r.owner||'—'}}</strong>
    <span style="margin-top:8px">Phone</span><strong>${{r.phone_number||'—'}}</strong>
    <span style="margin-top:8px">Mailing address</span><strong>${{r.mailing_address||'—'}}</strong>
  </div>
  <div class="reason-box"><strong>Business</strong><p>${{r.business_name||'—'}}${{r.business_category?` · ${{r.business_category}}`:''}}${{r.business_rating?` · ${{r.business_rating.toFixed(1)}}★ (${{num(r.business_reviews)}} reviews)`:''}}</p></div>
  ${{r.zone_code?`<div class="reason-box"><strong>Zoning: ${{r.zone_code}}</strong><p>${{r.zone_info||'No density/use details on file for this zone code.'}}</p></div>`:''}}
  ${{r.condition_rating?`<div class="reason-box"><strong>Visual condition: ${{r.condition_rating}}</strong><p>${{r.vision_notes||''}}</p></div>`:''}}
  <div class="reason-box"><strong>Key reasons</strong><p>${{r.reasons||'—'}}</p></div>
  ${{r.property_url?`<p><a href="${{r.property_url}}" target="_blank" rel="noreferrer">Open property record ↗</a></p>`:''}}`;
  $('#detail').showModal();
}}
const EXPORT_COLS = [
  'Property Street','Property City','Property State','Property ZIP Code','APN','Tags','Lists',
  'Notes','Status','Last Direct Mailed','Direct Mail Attempts','Full Name/Company/Trust',
  'Owner First Name','Owner Last Name','Owner Primary Phone','Owner Street','Owner City',
  'Owner State','Owner ZIP Code','Email 1','Email 2',
  'Phone 1 Number','Phone 1 Tags','Phone 1 Type','Phone 1 Status',
  'Phone 2 Number','Phone 2 Tags','Phone 2 Type','Phone 2 Status',
];
function toExportRow(r){{
  const p1 = (r.phones&&r.phones[0])||{{}}, p2 = (r.phones&&r.phones[1])||{{}};
  return {{
    'Property Street': r.address, 'Property City': r.city, 'Property State': r.state,
    'Property ZIP Code': r.zip, 'APN': r.apn, 'Tags': '', 'Lists': '',
    'Notes': `PropScore ${{r.opportunity_score.toFixed(1)}}: ${{r.reasons||''}}`,
    'Status': '', 'Last Direct Mailed': '', 'Direct Mail Attempts': '',
    'Full Name/Company/Trust': r.owner, 'Owner First Name': r.owner_first_name,
    'Owner Last Name': r.owner_last_name, 'Owner Primary Phone': r.phone_number,
    'Owner Street': r.mail_street, 'Owner City': r.mail_city, 'Owner State': r.mail_state,
    'Owner ZIP Code': r.mail_zip, 'Email 1': r.email1, 'Email 2': r.email2,
    'Phone 1 Number': p1.number||'', 'Phone 1 Tags': '', 'Phone 1 Type': p1.type||'', 'Phone 1 Status': p1.status||'',
    'Phone 2 Number': p2.number||'', 'Phone 2 Tags': '', 'Phone 2 Type': p2.type||'', 'Phone 2 Status': p2.status||'',
  }};
}}
function exportSelected(){{
  const rows = DATA.filter(r=>selected.has(rowKey(r))).map(toExportRow);
  const esc = v => `"${{String(v??'').replace(/"/g,'""')}}"`;
  const csv = [EXPORT_COLS.map(esc).join(',')].concat(rows.map(r=>EXPORT_COLS.map(c=>esc(r[c])).join(','))).join('\\r\\n');
  const blob = new Blob([csv], {{type:'text/csv;charset=utf-8;'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'propscore_selected_properties.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}}
function initFilters(){{
  document.querySelectorAll('th[data-k]').forEach(th=>{{ th.dataset.label = th.textContent.trim(); }});
  const cities=[...new Set(DATA.map(r=>r.city))].filter(Boolean).sort();
  cities.forEach(c=>$('#city').add(new Option(c,c)));
  const zips=[...new Set(DATA.map(r=>r.zip))].filter(Boolean).sort();
  zips.forEach(z=>$('#zip').add(new Option(z,z)));
  ['city','zip','search','min_score','min_lot'].forEach(id=>$('#'+id).addEventListener(id==='search'?'input':'change',apply));
  $('#sort').addEventListener('change',()=>{{sortKey=$('#sort').value;sortAsc=false;apply()}});
  $('#resetBtn').onclick=()=>{{$('#city').value='';$('#zip').value='';$('#search').value='';$('#min_score').value=0;$('#min_lot').value='';$('#sort').value='opportunity_score';sortKey=null;sortAsc=false;apply()}};
  document.querySelectorAll('th[data-k]').forEach(th=>th.onclick=()=>{{
    const k=th.dataset.k;
    if (sortKey===null) {{
      // Very first explicit sort click of the session. If it's on the
      // column already shown sorted (opportunity_score, high-to-low by
      // default), just confirm that direction instead of flipping it out
      // from under the user on their first click.
      sortKey = k;
      sortAsc = (k==='opportunity_score') ? sortAsc : (typeof DATA.find(r=>r[k]!=null)?.[k]==='string');
    }} else if (k===sortKey) {{
      sortAsc=!sortAsc;
    }} else {{
      sortKey=k; sortAsc = typeof DATA.find(r=>r[k]!=null)?.[k]==='string';
    }}
    const opt=[...document.querySelectorAll('#sort option')].find(o=>o.value===k);
    if (opt) $('#sort').value=k;
    apply();
  }});
  $('#selectAll').addEventListener('change',e=>{{
    document.querySelectorAll('#rows tr').forEach(tr=>{{
      if (e.target.checked) selected.add(tr.dataset.key); else selected.delete(tr.dataset.key);
      tr.querySelector('.rowCheck').checked = e.target.checked;
    }});
    updateSelection();
  }});
  $('#exportBtn').onclick=exportSelected;
  document.querySelectorAll('dialog').forEach(d=>d.querySelector('.close').onclick=()=>d.close());
}}
initFilters(); apply();
</script>
</body>
</html>
"""


def render_html(records, out_path):
    slim = []
    for r in records:
        slim.append({k: r[k] for k in (
            "address", "city", "state", "zip", "apn", "property_url", "zone_code", "zone_info",
            "owner", "owner_first_name", "owner_last_name",
            "phone_number", "mailing_address", "mail_street", "mail_city", "mail_state", "mail_zip",
            "email1", "email2", "phones",
            "lot_sqft", "building_sqft", "year_built", "est_value",
            "business_label", "business_name", "business_category",
            "business_reviews", "business_rating",
            "business_score", "property_score", "buildability_score", "visual_score", "data_coverage",
            "opportunity_score", "image_path", "condition_rating", "vision_notes", "reasons",
        )})
    favicon_b64 = base64.b64encode(Path(config.LOGO_FAVICON_FILE).read_bytes()).decode("ascii")
    logo_b64 = base64.b64encode(Path(config.LOGO_HEADER_FILE).read_bytes()).decode("ascii")
    html = HTML_TEMPLATE.format(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        count=len(records),
        favicon_b64=favicon_b64,
        logo_b64=logo_b64,
        data_json=json.dumps(slim),
    )
    Path(out_path).write_text(html, encoding="utf-8")


def render_csv(records, out_path):
    cols = [
        "id", "address", "city", "zip", "apn", "zone_code", "zone_info", "owner", "phone_number", "mailing_address",
        "business_label", "business_name",
        "business_category", "business_reviews", "business_rating",
        "business_score", "property_score", "buildability_score", "visual_score", "data_coverage",
        "opportunity_score", "condition_rating", "vision_notes", "reasons", "lot_sqft", "lot_acres",
        "building_sqft", "year_built", "est_value", "land_value", "land_value_ratio",
        "price_per_sqft", "property_url", "image_path",
        "image_status", "vision_status",
    ]
    df = pd.DataFrame(records)
    df["lot_acres"] = (df["lot_sqft"] / 43560).round(2)
    df = df.reindex(columns=cols)
    df = df.sort_values("opportunity_score", ascending=False)
    df.to_csv(out_path, index=False)


# ── Main ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Score Santa Clara commercial properties as build/acquisition opportunities")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N properties (for a quick test run)")
    parser.add_argument("--skip-vision", action="store_true", help="Skip Gemini vision calls (still fetches Street View images)")
    parser.add_argument("--skip-images", action="store_true", help="Skip Street View + vision entirely (business + property signals only)")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent worker threads (default 4)")
    args = parser.parse_args()

    if not GOOGLE_MAPS_API_KEY:
        print("WARNING: GOOGLE_MAPS_API_KEY not set in .env — Street View photos will be skipped.")
    if not args.skip_vision and not args.skip_images and not GEMINI_API_KEY:
        print("WARNING: GEMINI_API_KEY not set in .env — visual condition scoring will be skipped.")

    print("Loading properties...")
    properties = load_properties()
    print(f"  {len(properties)} properties loaded")

    print("Loading businesses...")
    businesses = load_businesses()
    print(f"  {len(businesses)} unique businesses loaded")

    join_businesses(properties, businesses)

    print("Loading zoning...")
    zoning_gdf = load_zoning()
    if zoning_gdf is not None:
        zoned = join_zoning(properties, zoning_gdf)
        with_coords = sum(1 for p in properties if p["lat"] is not None and p["lng"] is not None)
        print(f"  {zoned}/{with_coords} geocoded properties matched to a zoning parcel "
              f"({len(properties) - with_coords} properties have no lat/long to join on)")
    else:
        print(f"  WARNING: zoning file not found at {config.ZONING_GEOJSON_FILE} — skipping zoning signal")

    if args.limit:
        properties = properties[: args.limit]
        print(f"Limiting to first {len(properties)} properties")

    median_ppsf = compute_ppsf_median(properties)
    print(f"  Median $/sqft across dataset: {median_ppsf:.0f}" if median_ppsf else "  Median $/sqft unavailable")
    lot_p75, lot_p90 = compute_lot_percentiles(properties)
    print(f"  Lot sqft p75/p90 across dataset: {lot_p75:.0f} / {lot_p90:.0f}" if lot_p90 else "  Lot percentiles unavailable")

    print(f"Scoring {len(properties)} properties (workers={args.workers})...")
    results = []
    done = 0
    session = requests.Session()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(process_property, p, median_ppsf, lot_p75, lot_p90, session, args.skip_vision, args.skip_images): p
            for p in properties
        }
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                p = futures[fut]
                print(f"  ERROR scoring {p['address']}: {e}")
            done += 1
            if done % 25 == 0 or done == len(properties):
                print(f"  {done}/{len(properties)} scored")

    results.sort(key=lambda r: r["opportunity_score"], reverse=True)

    html_path = Path(config.OUTPUT_DIR) / config.HTML_REPORT_FILE
    csv_path = Path(config.OUTPUT_DIR) / config.CSV_REPORT_FILE
    render_html(results, html_path)
    render_csv(results, csv_path)

    declining = sum(1 for r in results if r["business_label"] == "declining")
    high = sum(1 for r in results if r["opportunity_score"] >= 60)
    with_vision = sum(1 for r in results if r["condition_rating"])
    with_image = sum(1 for r in results if r["image_path"])

    print("\n── Done ──")
    print(f"  Scored: {len(results)}")
    print(f"  Declining-business properties: {declining}")
    print(f"  Score >= 60: {high}")
    print(f"  Photos fetched: {with_image} | Vision-analyzed: {with_vision}")
    print(f"  HTML report: {html_path}")
    print(f"  CSV report:  {csv_path}")


if __name__ == "__main__":
    main()
