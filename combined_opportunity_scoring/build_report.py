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


def normalize_apn_digits(v):
    return re.sub(r"[^0-9]", "", str(v or ""))


def entitled_pipeline_note(apn_raw, address, city):
    """Returns the project label if this row is a known entitled/in-pipeline
    development site (config.ENTITLED_PIPELINE_APNS), else None. Multi-parcel
    assemblage rows carry a "; "-joined APN list — check each APN
    individually rather than normalizing the whole string into one
    unmatchable digit blob (same failure mode as the zoning lookup bug fixed
    earlier). Rows with no APN at all fall back to exact address+city
    matching via config.ENTITLED_PIPELINE_ADDRESSES."""
    for part in re.split(r"[;,]", str(apn_raw or "")):
        apn = normalize_apn_digits(part)
        if apn and apn in config.ENTITLED_PIPELINE_APNS:
            return config.ENTITLED_PIPELINE_APNS[apn]
    key = f"{str(city or '').strip().upper()}|{str(address or '').strip().upper()}"
    return config.ENTITLED_PIPELINE_ADDRESSES.get(key)


def load_zoning_gp_lookup():
    """Loads the precomputed current-zoning + General Plan designation table
    (built by build_zoning_gp_lookup.py) keyed by normalized APN. Returns {}
    if the file doesn't exist so this stays a gracefully-skipped optional
    signal, same as any other missing data."""
    path = Path(getattr(config, "ZONING_GP_LOOKUP_FILE", ""))
    if not path.exists():
        return {}
    df = pd.read_csv(path, dtype=str)

    def clean_val(v):
        return v if isinstance(v, str) and v.strip() and v.strip().lower() != "nan" else None

    return {
        row["apn_norm"]: {"current_zoning": clean_val(row.get("current_zoning")), "gp_designation": clean_val(row.get("gp_designation"))}
        for _, row in df.iterrows()
        if isinstance(row.get("apn_norm"), str) and row.get("apn_norm").strip()
    }


ZONING_GP_LOOKUP = load_zoning_gp_lookup()


def load_coord_backfill():
    """Loads lat/lng backfilled for properties missing LATITUDE/LONGITUDE in
    the source data (see geocode_los_altos.py) — captured from the Street
    View metadata endpoint's free geocoding side-effect. Keyed by normalized
    APN. Returns {} if the file doesn't exist, same graceful-skip pattern as
    ZONING_GP_LOOKUP."""
    path = Path(getattr(config, "COORD_BACKFILL_FILE", ""))
    if not path.exists():
        return {}
    df = pd.read_csv(path, dtype=str)
    result = {}
    for _, row in df.iterrows():
        apn_norm = row.get("apn_norm")
        if not (isinstance(apn_norm, str) and apn_norm.strip()):
            continue
        try:
            result[apn_norm] = (float(row["lat"]), float(row["lng"]))
        except (TypeError, ValueError):
            continue
    return result


COORD_BACKFILL = load_coord_backfill()


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


def years_held(last_sale_date):
    """Years since the last recorded sale, from build_regional_master.py's
    "Last Sale Recording Date" (already normalized to M/D/YYYY there, or
    blank if no source had one). Feeds score_property_info()'s "Long
    ownership" distress signal, which has been dead code all session --
    years_held was previously hardcoded to None with no source data."""
    d = clean(last_sale_date)
    if not d:
        return None
    try:
        sale = datetime.strptime(d, "%m/%d/%Y")
    except ValueError:
        return None
    return (datetime.now() - sale).days / 365.25


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
    """Reads the already-merged atherton_commercial_properties_cleaned.xlsx
    (built by build_atherton_master.py from PropertyRadar+LandVision+
    PropStream+Google Maps) instead of a raw single-source export like the
    Santa Clara pipeline does. Business data is already joined in per-row
    here, so each record carries its own `businesses` list directly —
    no separate load_businesses()/join_businesses() pass is needed."""
    df = pd.read_excel(config.PROPERTIES_FILE)
    records = []
    for i, row in df.iterrows():
        lat = number(row.get("LATITUDE"))
        lng = number(row.get("LONGITUDE"))
        if lat is None or lng is None:
            backfilled = COORD_BACKFILL.get(normalize_apn_digits(row.get("APN")))
            if backfilled:
                lat, lng = backfilled
        lot_sqft = number(row.get("Lot Sqft"))
        building_sqft = number(row.get("Building Sqft"))
        year_built = number(row.get("Year Built"))
        far = (building_sqft / lot_sqft) if (lot_sqft and building_sqft and lot_sqft > 0) else None
        est_value = number(row.get("Estimated Market Value")) or number(row.get("Assessed Value"))
        total_assessed_value = number(row.get("Assessed Value"))
        land_value = number(row.get("Land Value"))
        land_value_ratio = (
            min(1.0, land_value / total_assessed_value)
            if (land_value and total_assessed_value and total_assessed_value > 0) else None
        )
        owner_occupied_raw = row.get("Owner Occupied")
        owner_occupied = boolean(owner_occupied_raw) if clean(owner_occupied_raw) else None
        vacant_raw = row.get("Vacant")
        vacant = boolean(vacant_raw) if clean(vacant_raw) else None
        foreclosure_raw = row.get("Foreclosure")
        foreclosure = boolean(foreclosure_raw) if clean(foreclosure_raw) else False

        phone_number = format_phone(row.get("Phone"))
        mailing_address = clean(row.get("Mailing Address"))

        address = clean(row.get("Address"))
        norm_addr = normalize_address(address)

        # Business data is already joined per-row (see build_regional_master.py).
        # A single address can be a multi-tenant building (a shopping center
        # anchored by a supermarket, with several smaller tenants) — the merge
        # script keeps every matched business "||"-delimited, sorted by
        # review count, rather than only the first one the scrape happened to
        # list. Split them back out here into one dict per business so
        # classify_business() can judge the site by its most prominent
        # tenant instead of an arbitrary one.
        BIZ_SEP = "||"
        businesses = []
        names = clean(row.get("Business Name")).split(BIZ_SEP) if clean(row.get("Business Name")) else []
        categories = clean(row.get("Business Category")).split(BIZ_SEP)
        reviews = clean(row.get("Business Reviews")).split(BIZ_SEP)
        ratings = clean(row.get("Business Rating")).split(BIZ_SEP)
        closed_flags = clean(row.get("Business Closed")).split(BIZ_SEP)
        for idx, name in enumerate(names):
            name = name.strip()
            if not name:
                continue
            category = categories[idx].strip() if idx < len(categories) else ""
            closed_str = closed_flags[idx].strip() if idx < len(closed_flags) else ""
            businesses.append({
                "title": name,
                "norm_addr": norm_addr,
                "lat": lat, "lng": lng,
                "category": category,
                "categories_text": category.lower(),
                "permanently_closed": closed_str == "True",
                "temporarily_closed": False,
                "reviews_count": number(reviews[idx]) if idx < len(reviews) else None,
                "rating": number(ratings[idx]) if idx < len(ratings) else None,
            })

        record = {
            "id": i,
            "address": address,
            "city": clean(row.get("City")).title() or "Atherton",
            "state": "CA",
            "zip": clean_zip(row.get("Zip")),
            "apn": clean(row.get("APN")),
            "is_entitled_pipeline": entitled_pipeline_note(row.get("APN"), address, row.get("City")) is not None,
            "entitled_note": entitled_pipeline_note(row.get("APN"), address, row.get("City")) or "",
            "lat": lat,
            "lng": lng,
            "norm_addr": norm_addr,
            "lot_sqft": lot_sqft,
            "building_sqft": building_sqft,
            "year_built": int(year_built) if year_built else None,
            "far": far,
            "est_value": est_value,
            "land_value": land_value,
            "land_value_ratio": land_value_ratio,
            "owner_occupied": owner_occupied,
            "vacant": vacant,
            "foreclosure": foreclosure,
            "preforeclosure": False,
            "auction": False,
            "bank_owned": False,
            "pre_probate": False,
            "deceased_owner": False,
            "equity_pct": number(row.get("Equity %")),
            "last_sale_date": clean(row.get("Last Sale Recording Date")),
            "years_held": years_held(row.get("Last Sale Recording Date")),
            "condition_text": "",
            "property_url": "",
            "zoning": clean(row.get("Zoning")),
            "business_photo_url": clean(row.get("Business Photo URL")) or None,
            "owner": clean(row.get("Owner")),
            "owner_first_name": "",
            "owner_last_name": "",
            "phone_number": phone_number,
            "mailing_address": mailing_address,
            "mail_street": mailing_address,
            "mail_city": "",
            "mail_state": "",
            "mail_zip": "",
            "email1": clean(row.get("Email")),
            "email2": "",
            "phones": ([{"number": phone_number, "type": "", "status": ""}] if phone_number else []),
            "zone_code": None,  # no Atherton zoning-density reference data yet
            "gp_designation": ZONING_GP_LOOKUP.get(normalize_apn_digits(row.get("APN")), {}).get("gp_designation"),
            "current_zoning_lookup": ZONING_GP_LOOKUP.get(normalize_apn_digits(row.get("APN")), {}).get("current_zoning"),
            "businesses": businesses,
        }
        records.append(record)
    return records


# ── Load + join zoning ────────────────────────────────────────────────

def load_zoning(path=None, field="ZONGDSGN"):
    import geopandas as gpd
    path = Path(path or config.ZONING_GEOJSON_FILE)
    if not path.exists():
        return None
    gdf = gpd.read_file(path)[[field, "geometry"]]
    if field != "ZONGDSGN":
        gdf = gdf.rename(columns={field: "ZONGDSGN"})
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
    """Multi-tenant properties (a shopping center anchored by a supermarket,
    with several smaller tenants) are judged by their most PROMINENT tenant
    — the one with the most Google reviews, a proxy for how established/
    visible it is — rather than any single listing that happens to match a
    declining keyword. Without this, a thriving shopping center with one
    small "photo lab" sub-tenant would get flagged as a declining business
    based on that one minor listing, which is exactly backwards."""
    if not businesses:
        return "unknown", "No matching business record found for this address", None

    multi_note = f" (site has {len(businesses)} businesses on record)" if len(businesses) > 1 else ""
    primary = max(businesses, key=lambda b: b.get("reviews_count") or 0)

    if primary["permanently_closed"]:
        return "declining", f"Permanently closed business on site: {primary['title']}{multi_note}", primary
    if primary["temporarily_closed"]:
        return "declining", f"Temporarily closed business on site: {primary['title']}{multi_note}", primary

    text = f"{primary['title']} {primary['categories_text']}".lower()
    if not any(ex in text for ex in config.DECLINING_EXCLUDE_KEYWORDS):
        hit = next((kw for kw in config.DECLINING_BUSINESS_KEYWORDS if kw in text), None)
        if hit:
            label = f"{primary['title']} ({primary['category'] or hit})"
            return "declining", f"Declining-category business on site: {label}{multi_note}", primary

    label = f"{primary['title']} ({primary['category']})" if primary["category"] else primary["title"]
    return "neutral", f"Active business on site: {label}{multi_note}", primary


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


def refine_with_parking(label, business_score, business_reason, vision_result):
    """A busy/full parking area (lot OR garage/structure) is direct visual
    evidence of active foot traffic that should override a category-keyword
    or closed-flag "declining" read — per user direction, a property that
    LOOKS like a struggling business on paper but has full parking in the
    photo probably isn't actually declining. Only overrides when vision
    reports a clear "busy" reading; a quiet/empty reading is deliberately
    NOT used to reinforce "declining" (a single photo could just be an
    off-peak time of day, not a reliable negative signal)."""
    if label != "declining" or not vision_result:
        return label, business_score, business_reason
    activity = str(vision_result.get("parking_activity", "")).lower()
    if activity != "busy":
        return label, business_score, business_reason
    note = "Busy/full parking (lot or garage) visible in the photo — likely still an active, healthy business despite the category/closure signal"
    return "neutral", 30.0, f"{business_reason} — {note}"


# ── Property/financial scoring ───────────────────────────────────────

def score_property_info(p):
    """Ownership/financial signal: is this a motivated seller at a good price?"""
    parts = {}
    parts["Vacant"] = 20 if p["vacant"] else 0
    parts["Foreclosure/distress"] = 20 if (p["foreclosure"] or p["preforeclosure"] or p["auction"] or p["bank_owned"]) else 0
    parts["Pre-probate/estate"] = 15 if (p["pre_probate"] or p["deceased_owner"]) else 0
    # Absentee ownership is the norm for commercial real estate (88-96% of
    # properties in this dataset), not a distress signal, so it was never a
    # flat bonus here. The narrower "owner-user + another distress sign"
    # combo was tried and dropped too: for a REDEVELOPMENT-focused tool (not
    # general resale-motivation screening), ownership structure doesn't
    # clearly predict who's more sellable to a developer -- an absentee
    # landlord with a failing tenant may be an easier sell than an
    # owner-operator who'd have to relocate their own business. It's also
    # not reliably measurable here: unlike residential, a commercial owner
    # routes mail to their home/accountant regardless of whether they
    # operate the business on-site, so mailing-address-matches-property
    # doesn't test owner-occupancy for this asset class the way it would
    # for a house.
    if p["equity_pct"] is not None:
        parts["High equity"] = 15 if p["equity_pct"] >= 70 else (8 if p["equity_pct"] >= 40 else 0)
    if p["years_held"] is not None:
        parts["Long ownership"] = 10 if p["years_held"] >= 15 else (5 if p["years_held"] >= 8 else 0)
    if p["condition_text"] and re.search(r"POOR|FAIR|FIXER|NEEDS|DEFERRED|BELOW AVERAGE", p["condition_text"], re.I):
        parts["Reported poor condition"] = 15

    score = min(100, sum(parts.values()))
    reasons = [k for k, v in parts.items() if v]

    available = sum([
        p["equity_pct"] is not None, p["years_held"] is not None,
        p["vacant"] is not None,
    ])
    return score, reasons, available / 3.0


# ── Buildability scoring ─────────────────────────────────────────────

def compute_lot_percentiles(properties):
    vals = sorted(p["lot_sqft"] for p in properties if p["lot_sqft"])
    if not vals:
        return 0, 0

    def pct(q):
        return vals[min(len(vals) - 1, round((len(vals) - 1) * q))]

    return pct(0.75), pct(0.90)


SQFT_PER_ACRE = 43560.0


def lot_size_multiplier(lot_sqft):
    """Multiplier applied to the whole buildability score based on lot
    size in acres — a distressed/underbuilt property that's too small
    can't actually support a townhome development, so this suppresses
    every buildability signal together rather than scoring size as one
    independent line item. Piecewise-linear between config's control
    points; full credit (1.0) at 1 acre and above."""
    if not lot_sqft:
        return 1.0, None
    acres = lot_sqft / SQFT_PER_ACRE
    points = config.LOT_SIZE_MULTIPLIER_POINTS
    if acres >= points[-1][0]:
        return 1.0, acres
    if acres <= points[0][0]:
        return points[0][1], acres
    for (a0, m0), (a1, m1) in zip(points, points[1:]):
        if a0 <= acres <= a1:
            frac = (acres - a0) / (a1 - a0)
            return m0 + frac * (m1 - m0), acres
    return 1.0, acres


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

    size_mult, lot_acres = lot_size_multiplier(p["lot_sqft"])

    # Current zoning: prefer p["current_zoning_lookup"] (real per-parcel/GIS
    # data for Los Altos and Palo Alto — see build_zoning_gp_lookup.py) over
    # p["zone_code"] (Menlo Park's real GIS spatial join) over p["zoning"],
    # the raw merged column, which for some cities (Menlo Park, Atherton) is
    # a non-municipal county encoding rather than a real zone code.
    zoning_key = (p["current_zoning_lookup"] or p["zone_code"] or p["zoning"] or "").strip().upper() or None
    current_zone_fit = config.ZONE_TOWNHOME_FIT.get(zoning_key) if zoning_key else None

    # General Plan designation: a DIFFERENT signal from current zoning — the
    # city's committed future land-use vision, which may require a rezoning
    # to actually build against. Real data for Los Altos, Palo Alto, and
    # Menlo Park only (build_zoning_gp_lookup.py); Atherton has neither.
    gp_key = (p["gp_designation"] or "").strip() or None
    gp_fit_raw = config.GP_TOWNHOME_FIT.get(gp_key) if gp_key else None

    # Combine the two: both favorable = strongest case (buildable today AND
    # city-committed) — take the stronger in full, the weaker as a 25%
    # confirmation bump, same pattern already used for FAR/land-value above.
    # Only GP favorable (current zoning unfavorable/unknown) means a rezoning
    # is still needed, so it's discounted rather than given full credit.
    # Only current zoning favorable behaves exactly as before GP data existed.
    zone_fit = None
    zone_fit_label = None
    if current_zone_fit and gp_fit_raw:
        zone_fit = max(current_zone_fit, gp_fit_raw) + round(min(current_zone_fit, gp_fit_raw) * 0.25)
        zone_fit_label = f"Zoned {zoning_key} (current) + GP {gp_key} (both townhome fit)"
    elif current_zone_fit:
        zone_fit = current_zone_fit
        zone_fit_label = f"Zoned {zoning_key} (townhome fit)"
    elif gp_fit_raw:
        zone_fit = round(gp_fit_raw * config.GP_ONLY_DISCOUNT)
        zone_fit_label = f"GP designation \"{gp_key}\" suggests future rezoning potential (entitlement pending)"

    # A verified zone_fit match plus a lot big enough for 2-story townhomes
    # (~0.5 acre+) already tells us the site is buildable regardless of how
    # built-up the CURRENT structure is — a thriving restaurant on a large
    # El Camino Real corridor lot doesn't stop it from being torn down and
    # redeveloped. Low/moderate FAR still earn full credit as normal (a
    # genuinely underbuilt lot is still a genuinely underbuilt lot) — only
    # the high-FAR tier, which would otherwise score zero, gets raised
    # instead of fully penalized, per user direction.
    far_deemphasized = zone_fit is not None and lot_acres is not None and lot_acres >= 0.5

    far_score = None
    if p["far"] is not None:
        if p["far"] <= config.FAR_LOW:
            far_score = 30
        elif p["far"] <= config.FAR_MODERATE:
            far_score = 15
        else:
            far_score = 15 if far_deemphasized else 0
    land_ratio_score = None
    if p["land_value_ratio"] is not None:
        land_ratio_score = (
            25 if p["land_value_ratio"] >= config.LAND_VALUE_RATIO_HIGH else
            (12 if p["land_value_ratio"] >= config.LAND_VALUE_RATIO_MODERATE else 0)
        )
    # Low FAR and land-value-dominates-improvement are largely the same
    # underlying fact (a small/old building relative to its land) — when
    # both fire, count the stronger one in full and the second only as a
    # small confirmation bump rather than stacking both to their full value.
    if far_score and land_ratio_score:
        parts["Low FAR (underbuilt lot)"] = max(far_score, land_ratio_score)
        parts["Land value dominates improvement (confirms underbuilt lot)"] = round(min(far_score, land_ratio_score) * 0.25)
    else:
        if far_score:
            parts["Low FAR (underbuilt lot)"] = far_score
        if land_ratio_score:
            parts["Land value dominates improvement"] = land_ratio_score
    if p["lot_sqft"]:
        parts["Large lot (dataset-relative)"] = 20 if p["lot_sqft"] >= lot_p90 else (10 if p["lot_sqft"] >= lot_p75 else 0)
    if p["year_built"]:
        parts["Old building"] = 10 if p["year_built"] <= config.OLD_BUILDING_YEAR else (5 if p["year_built"] <= config.AGING_BUILDING_YEAR else 0)

    if zone_fit is not None:
        parts[zone_fit_label] = zone_fit

    hint = None
    if p["far"] is None and p["building_sqft"] is None:
        hint = land_use_hint(p["businesses"])
        if hint:
            parts[f"Land-use hint: {hint}"] = 20

    score = min(100, sum(parts.values())) * size_mult
    reasons = [k for k, v in parts.items() if v]
    if lot_acres is not None and size_mult < 0.95:
        reasons.append(f"Lot only {lot_acres:.2f} ac — too small for realistic townhome redevelopment, buildability score suppressed")
    if far_deemphasized and p["far"] is not None and p["far"] > config.FAR_MODERATE:
        reasons.append(f"{zone_fit_label} + {lot_acres:.2f} ac lot — high existing FAR not held against a redevelopment-ready site")

    available = sum([
        p["far"] is not None, p["land_value_ratio"] is not None,
        bool(p["lot_sqft"]), bool(p["year_built"]), zone_fit is not None, bool(hint),
    ])
    return round(score, 1), reasons, available / 6.0


# ── Street View ───────────────────────────────────────────────────────

def street_view_location_param(p):
    if p["lat"] is not None and p["lng"] is not None:
        return f"{p['lat']},{p['lng']}"
    return f"{p['address']}, {p['city']}, CA {p['zip']}".strip()


def fetch_street_view(p, session):
    """Returns (image_path, status, photo_date). photo_date is Street View's
    own capture date (e.g. "2023-06") from the metadata endpoint — free to
    query, and lets the report flag when a photo may be years out of date
    (Street View has no way to request a newer capture; this is always
    whatever Google's latest drive-by happens to be)."""
    key = safe_key(p["norm_addr"] + "_" + str(p["id"]))
    img_path = IMAGES_DIR / f"{key}.jpg"
    date_path = IMAGES_DIR / f"{key}.date.txt"
    if img_path.exists():
        photo_date = date_path.read_text(encoding="utf-8").strip() if date_path.exists() else None
        return str(img_path), "OK", photo_date

    if not GOOGLE_MAPS_API_KEY:
        return None, "NO_API_KEY", None

    location = street_view_location_param(p)
    try:
        meta = session.get(
            "https://maps.googleapis.com/maps/api/streetview/metadata",
            params={"location": location, "key": GOOGLE_MAPS_API_KEY},
            timeout=20,
        ).json()
    except requests.RequestException as e:
        return None, f"METADATA_ERROR: {e}", None

    status = meta.get("status", "UNKNOWN")
    if status != "OK":
        return None, status, None
    photo_date = meta.get("date")

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
        if photo_date:
            date_path.write_text(photo_date, encoding="utf-8")
        return str(img_path), "OK", photo_date
    except requests.RequestException as e:
        return None, f"IMAGE_ERROR: {e}", None


def fetch_business_photo(p, session):
    """Downloads the Google Maps listing photo (business-uploaded or recent
    visitor photo) when the business join found one — often much fresher
    than a Street View drive-by, since it comes from the business's own
    listing rather than a car that may have passed years ago. No capture
    date is available for these, unlike Street View's metadata."""
    url = p.get("business_photo_url")
    if not url:
        return None, "NO_BUSINESS_PHOTO"
    key = safe_key(p["norm_addr"] + "_" + str(p["id"]) + "_bizphoto")
    img_path = IMAGES_DIR / f"{key}.jpg"
    if img_path.exists():
        return str(img_path), "OK"
    try:
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
        img_path.write_bytes(resp.content)
        return str(img_path), "OK"
    except requests.RequestException as e:
        return None, f"IMAGE_ERROR: {e}"


def fetch_property_photo(p, session):
    """Prefers the business's own Google Maps listing photo when available
    (fresher, represents the actual current storefront); falls back to
    Street View otherwise. Returns (image_path, status, photo_source,
    photo_date) — photo_date is only ever set for the Street View fallback."""
    biz_path, biz_status = fetch_business_photo(p, session)
    if biz_path:
        return biz_path, biz_status, "business_photo", None
    sv_path, sv_status, sv_date = fetch_street_view(p, session)
    return sv_path, sv_status, "street_view", sv_date


# ── Gemini vision ─────────────────────────────────────────────────────

VISION_PROMPT = """You are screening a commercial property photo for a real-estate investor \
looking for redevelopment/acquisition opportunities. Rate the PHYSICAL CONDITION of the \
building and site visible in this photo (it may be a Google Street View capture or a \
business's own listing photo).

Property context: {context}

Also assess parking if any is visible — a surface lot OR a parking garage/structure (multi-level \
parking counts too, not just open lots): a busy/full parking area (lot or garage) is real \
evidence of active foot traffic and should be reported even if the property otherwise looks \
aging or the business category seems like it might be struggling — don't let building \
condition or category assumptions override what the parking actually shows. A quiet or \
empty parking area is NOT strong evidence of anything by itself (a single photo could just be \
an off-peak time of day) — only report "busy" when it's clearly true, don't guess "empty" \
as a negative signal.

Respond with ONLY a JSON object, no markdown, matching this schema:
{{"condition_rating": "poor" | "fair" | "good" | "excellent",
  "visible_vacancy_or_disrepair": true | false,
  "parking_activity": "busy" | "moderate" | "empty" | "not_visible",
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


def score_visual(vision_result, already_known_vacant=False):
    """already_known_vacant: whether the property/financial data already
    flags this property as vacant. When the photo also shows vacancy, that's
    confirming the same underlying fact rather than new information, so it
    only gets a small bump instead of stacking the full amount on top of
    the Property layer's own vacancy points."""
    if not vision_result:
        return None
    rating = str(vision_result.get("condition_rating", "")).lower()
    base = {"poor": 90, "fair": 60, "good": 25, "excellent": 10}.get(rating)
    if base is None:
        return None
    if vision_result.get("visible_vacancy_or_disrepair"):
        bonus = 5 if already_known_vacant else 15
        base = min(100, base + bonus)
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

def process_property(p, lot_p75, lot_p90, session, skip_vision, skip_images):
    raw_label, business_reason, top_business = classify_business(p["businesses"])
    label, business_score, review_note = refine_with_reviews(raw_label, top_business)
    if review_note:
        business_reason = f"{business_reason} — {review_note}"

    property_score, property_reasons, property_cov = score_property_info(p)
    buildability_score, buildability_reasons, buildability_cov = score_buildability(p, lot_p75, lot_p90)
    business_cov = 1.0 if business_score is not None else 0.0

    image_path, image_status, vision_result, vision_status = None, "SKIPPED", None, "SKIPPED"
    photo_source, photo_date = None, None
    if not skip_images:
        image_path, image_status, photo_source, photo_date = fetch_property_photo(p, session)
        if image_path and not skip_vision:
            cache_key = p["city"] + "|" + p["norm_addr"]
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

        # A busy parking lot in the photo is direct evidence that overrides
        # a "declining" read from category keywords/closures — per user
        # direction, checked after vision so it can actually see the lot.
        label, business_score, business_reason = refine_with_parking(label, business_score, business_reason, vision_result)
        business_cov = 1.0 if business_score is not None else 0.0

    visual_score = score_visual(vision_result, already_known_vacant=bool(p["vacant"]))
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
    if photo_source == "street_view" and photo_date:
        try:
            photo_year = int(str(photo_date)[:4])
            if photo_year <= datetime.now().year - 4:
                reasons.append(f"Street View photo is from {photo_date} — may not reflect the property's current condition")
        except (ValueError, TypeError):
            pass

    return {
        **{k: p[k] for k in (
            "id", "address", "city", "state", "zip", "apn", "is_entitled_pipeline", "entitled_note", "lat", "lng", "lot_sqft", "building_sqft",
            "year_built", "est_value", "land_value", "land_value_ratio",
            "last_sale_date", "years_held",
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
        "parking_activity": (vision_result or {}).get("parking_activity", ""),
        "photo_source": photo_source or "",
        "photo_date": photo_date or "",
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
   sage green #059669 (their own secondary accent, used here for "high
   opportunity"), cream #FCF9F5 (light text on navy). Headings use their
   actual brand font (Trirong, serif, on Google Fonts); body uses Manrope
   as a close open substitute for their proprietary Wix Madefor font,
   which isn't available outside Wix. */
:root{{--navy:#082F7B;--blue:#1D4ED8;--ink:#0F172A;--muted:#64748B;--paper:#FAFAFA;--card:#fff;--line:#E2E8F0;--sage:#047857;--sage-bg:#ECFDF5;--cream:#F8FAFC;--amber:#B45309;--amber-bg:#FFFBEB;--shadow:0 1px 2px rgba(15,23,42,.06)}}
*{{box-sizing:border-box}}
/* html carries the navy so an over-scroll bounce past the top of the page shows
   the header colour rather than a white band; overscroll-behavior stops the
   rubber-band entirely where supported. */
html{{background:var(--navy);overscroll-behavior-y:none}}
body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 Manrope,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif;overscroll-behavior-y:none;min-height:100vh}}
h1,h2{{font-family:Trirong,Georgia,serif}}
/* Slim sticky top bar: the old header was ~200px of navy carrying no data.
   Sticky so branding stays put while the results scroll. */
.topbar{{position:sticky;top:0;z-index:40;display:flex;align-items:center;gap:13px;height:56px;
  padding:0 18px;background:var(--navy);color:var(--cream)}}
.logo{{height:30px;width:auto;display:block;flex:0 0 auto}}
.brand-text{{display:flex;flex-direction:column;justify-content:center;gap:5px}}
h1{{font-weight:700;font-size:23px;line-height:1.05;margin:0;letter-spacing:.01em}}
.eyebrow{{font-size:8.5px;letter-spacing:.16em;color:#93A9CE;margin:0;line-height:1;font-weight:600}}
/* App shell: sticky filter rail on the left, results own the rest. */
/* No transition on grid-template-columns: browsers handle interpolating grid
   tracks inconsistently and here it blocked the collapse outright. The rail
   snaps instead, and the sidebar's own properties carry the softening. */
.shell{{display:grid;grid-template-columns:262px minmax(0,1fr);align-items:start}}
.sidebar{{transition:padding .15s ease}}
.shell.nav-mini{{grid-template-columns:58px minmax(0,1fr)}}
.shell.nav-mini .sidebar{{padding:14px 9px}}
.shell.nav-mini .sidebar-title,
.shell.nav-mini .filter-grid{{display:none}}
/* icon rail shown only while collapsed */
.mini-rail{{display:none;flex-direction:column;gap:5px}}
.shell.nav-mini .mini-rail{{display:flex}}
.mini-btn{{position:relative;width:40px;height:40px;border-radius:9px;display:grid;place-items:center;
  background:transparent;border:1px solid transparent;color:var(--muted);cursor:pointer;transition:background .13s,color .13s}}
.mini-btn:hover{{background:#F1F5F9;color:var(--navy)}}
.mini-btn svg{{width:17px;height:17px;stroke:currentColor;fill:none;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}}
/* blue dot marks a filter that is currently narrowing the results */
.mini-btn.active{{color:var(--blue);background:#EFF4FF}}
.mini-btn.active::after{{content:"";position:absolute;top:5px;right:5px;width:6px;height:6px;border-radius:50%;background:var(--blue)}}
.mini-btn .tip{{position:absolute;left:46px;white-space:nowrap;background:var(--ink);color:#fff;font-size:11px;
  font-weight:600;padding:4px 9px;border-radius:6px;opacity:0;pointer-events:none;transition:opacity .12s;z-index:60}}
.mini-btn:hover .tip{{opacity:1}}
.mini-sep{{height:1px;background:var(--line);margin:4px 6px}}
/* a grid item won't shrink past its content's min-content width unless this is set */
.sidebar{{min-width:0}}
.icon-btn{{background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.20);color:var(--cream);
  width:32px;height:32px;border-radius:8px;display:grid;place-items:center;font-size:14px;line-height:1;
  cursor:pointer;flex:0 0 auto;transition:background .15s}}
.icon-btn:hover{{background:rgba(255,255,255,.24)}}
.sidebar{{position:sticky;top:56px;align-self:start;max-height:calc(100vh - 56px);overflow-y:auto;
  padding:16px 15px 26px;background:#fff;border-right:1px solid var(--line)}}
.sidebar-title{{font:700 10.5px/1 Manrope,sans-serif;letter-spacing:.13em;text-transform:uppercase;
  color:var(--muted);margin:0 0 13px}}
.content{{padding:14px 18px 18px;min-width:0;display:flex;flex-direction:column;height:calc(100vh - 56px)}}
.notice{{background:#FFFBEB;border-left:4px solid var(--amber);padding:12px 16px;margin-bottom:18px;border-radius:0 9px 9px 0;color:#92400E}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}}
/* Stacked in the rail -- no more fighting to fit 8 controls on one row. */
.filter-grid{{display:flex;flex-direction:column;gap:13px}}
.note-cell{{white-space:normal;min-width:230px;max-width:320px;font-size:11.5px;line-height:1.45;cursor:text}}
.note-cell:hover{{background:#F8FAFC}}
.note-cell:focus{{outline:2px solid var(--blue);outline-offset:-2px;background:#fff}}
.note-cell:empty::before{{content:"Click to add a note…";color:#94A3B8;font-style:italic}}
.note-cell.pipeline{{background:#FFFBEB;border-left:3px solid var(--amber)}}
.note-cell.user-edited{{background:#ECFDF5}}
/* flex-direction must be stated: .toggle-chip is a <label>, and the global
   `label` rule sets column, which would stack the checkbox above the text. */
.toggle-chip{{display:flex;flex-direction:row;align-items:center;gap:7px;background:#F1F5F9;border:1px solid var(--line);padding:9px 11px;border-radius:8px;font-size:11.5px;line-height:1.3;font-weight:600;color:var(--navy);cursor:pointer;min-height:38px}}
.toggle-chip input{{flex:0 0 auto;margin:0}}
.toggle-chip input{{width:auto;cursor:pointer}}
label,.field{{display:flex;flex-direction:column;gap:5px;font-size:12px;color:var(--muted);font-weight:700}}
.field span{{visibility:hidden}}
input,select{{width:100%;padding:9px 10px;border:1px solid #CBD5E1;border-radius:8px;background:#fff;color:var(--ink);font:inherit}}
button{{font:inherit;cursor:pointer;border:0}}
.ghost{{background:#F1F5F9;border:1px solid var(--line);padding:9px 12px;border-radius:8px;color:var(--navy);font-weight:600;width:100%}}
.primary{{background:var(--blue);border:1px solid var(--blue);padding:9px 14px;border-radius:8px;color:#fff;font-weight:700}}
.primary:disabled{{opacity:.45;cursor:not-allowed}}
/* Compact stat strip: same six numbers, ~40% of the previous height. */
.kpis{{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 13px}}
.kpis article{{position:relative;flex:1 1 148px;background:var(--card);border:1px solid var(--line);
  border-radius:10px;padding:8px 11px;text-align:center}}
/* symmetric padding so the centred caption clears the info icon on the right */
.kpis > article > span:not(.info-icon){{display:block;color:var(--muted);font-size:10.5px;line-height:1.3;padding:0 14px}}
.kpis strong{{display:block;font:600 21px/1.15 Trirong,Georgia,serif;margin-top:1px;color:var(--navy)}}
.info-icon{{position:absolute;top:7px;right:8px;width:15px;height:15px;border-radius:50%;display:grid;place-items:center;background:transparent;border:1.4px solid #CBD5E1;color:#94A3B8;font:600 10px/1 Manrope,ui-sans-serif,system-ui,sans-serif;cursor:pointer;transition:background .15s,border-color .15s,color .15s}}
.info-icon:hover,.info-icon:focus{{background:var(--blue);border-color:var(--blue);color:#fff;outline:none}}
.info-popover{{display:none;position:absolute;top:24px;right:-6px;width:270px;background:#fff;border:1px solid var(--line);border-radius:10px;box-shadow:var(--shadow);padding:13px 15px;text-align:left;z-index:30;cursor:default}}
.info-icon:hover .info-popover,.info-icon:focus .info-popover{{display:block}}
.info-popover .pop-title{{display:block;font:700 12.5px/1.35 Manrope,sans-serif;color:var(--navy);margin-bottom:8px}}
.info-popover ul{{margin:0;padding-left:15px;font-size:11px;line-height:1.55;color:var(--muted);font-weight:400}}
.info-popover li{{margin-bottom:5px}}
.info-popover li:last-child{{margin-bottom:0}}
.info-popover b{{color:var(--ink)}}
.table-card{{overflow:hidden;flex:1;display:flex;flex-direction:column;min-height:0}}
.table-head{{display:flex;align-items:end;justify-content:space-between;padding:18px 20px;border-bottom:1px solid var(--line)}}
h2{{font-weight:600;font-size:22px;margin:0;color:var(--navy)}}
.table-wrap{{overflow:auto;flex:1;min-height:0}}
table{{border-collapse:collapse;width:100%;min-width:1300px}}
th{{position:sticky;top:0;background:#F1F5F9;color:var(--navy);text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.04em;padding:10px;border-bottom:1px solid var(--line);cursor:pointer;z-index:1}}
th:first-child,td:first-child{{width:34px;cursor:default}}
.property-cell{{width:170px;max-width:170px}}
.business-cell{{width:150px;max-width:150px}}
.property-cell .address,.business-cell .sub{{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:170px}}
td{{padding:10px;border-bottom:1px solid #F1F5F9;vertical-align:top}}
tbody tr{{cursor:pointer}}
tbody tr:hover{{background:#F8FAFC}}
.thumb{{width:96px;height:64px;object-fit:cover;border-radius:6px;background:#E2E8F0;display:block;cursor:zoom-in}}
#imgLightbox{{background:transparent;box-shadow:none;padding:0;width:min(90vw,900px);border:0}}
#imgLightbox::backdrop{{background:rgba(8,20,45,.85)}}
#imgLightbox img{{width:100%;border-radius:10px;display:block}}
#imgLightbox figcaption{{color:#fff;text-align:center;margin-top:10px;font-weight:600}}
#imgLightbox .close{{position:absolute;top:10px;right:10px;width:36px;height:36px;border-radius:50%;background:rgba(8,20,45,.7);color:#fff;font-size:22px;line-height:1;display:grid;place-items:center;padding:0}}
.address{{font-weight:750}}
.address a{{color:inherit;text-decoration:none}}
.address a:hover{{color:var(--blue);text-decoration:underline}}
.sub{{font-size:12px;color:var(--muted);margin-top:2px}}
.sub a{{color:var(--blue)}}
.score{{display:inline-grid;place-items:center;min-width:44px;padding:6px 8px;border-radius:999px;font-weight:750;background:#F1F5F9;color:var(--navy)}}
.score.high{{background:var(--sage-bg);color:var(--sage)}}
.score.mid{{background:#FDE68A;color:#B45309}}
.chip{{display:inline-block;padding:3px 8px;border-radius:999px;font-size:11px;font-weight:700}}
.chip.declining{{background:#FEE2E2;color:#B91C1C}}
.chip.neutral{{background:#F1F5F9;color:var(--muted)}}
.chip.unknown{{background:#F1F5F9;color:#94A3B8}}
.reason{{min-width:420px;color:#334155;font-size:12.5px}}
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
@media(max-width:1100px){{
  .shell{{grid-template-columns:1fr}}
  .sidebar{{position:static;max-height:none;border-right:0;border-bottom:1px solid var(--line)}}
  .filter-grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:11px}}
  .kpis article{{flex-basis:calc(33.3% - 8px)}}
  .content{{height:auto}}
  .table-wrap{{max-height:none}}
  .detail-grid{{grid-template-columns:repeat(2,1fr)}}
}}
</style>
</head>
<body>
<header class="topbar">
  <button id="sidebarToggle" class="icon-btn" aria-label="Collapse or expand filters" title="Collapse / expand filters">&#9776;</button>
  <img class="logo" src="data:image/png;base64,{logo_b64}" alt="PropScore AI logo">
  <div class="brand-text">
    <h1>PropScore AI</h1>
    <p class="eyebrow">AI-POWERED PROPERTY SCREENING</p>
  </div>
</header>
<div class="shell">
  <aside class="sidebar">
    <div class="mini-rail">
      <button class="mini-btn" data-mini="city" data-target="city" aria-label="City"><svg viewBox="0 0 16 16"><path d="M2 14h12M4.5 14V5l3.5-2 3.5 2v9"/><path d="M6.6 8h.01M9.4 8h.01M6.6 11h.01M9.4 11h.01"/></svg><span class="tip">City</span></button>
      <button class="mini-btn" data-mini="zip" data-target="zip" aria-label="Zipcode"><svg viewBox="0 0 16 16"><path d="M3 6.2h10M3 10h10M6.6 3l-1.1 10M10.6 3l-1.1 10"/></svg><span class="tip">Zipcode</span></button>
      <button class="mini-btn" data-mini="search" data-target="search" aria-label="Address"><svg viewBox="0 0 16 16"><circle cx="7" cy="7" r="4.2"/><path d="M10.2 10.2 14 14"/></svg><span class="tip">Address</span></button>
      <button class="mini-btn" data-mini="lot" data-target="min_lot_acres" aria-label="Lot size range"><svg viewBox="0 0 16 16"><rect x="2.5" y="2.5" width="11" height="11" rx="1.5"/><path d="M5.4 5.4h2.1M5.4 5.4v2.1M10.6 10.6H8.5M10.6 10.6V8.5"/></svg><span class="tip">Lot size range</span></button>
      <button class="mini-btn" data-mini="min_score" data-target="min_score" aria-label="Minimum score"><svg viewBox="0 0 16 16"><path d="M8 2.4 9.75 6l3.85.55-2.8 2.7.66 3.85L8 11.28 4.54 13.1l.66-3.85-2.8-2.7L6.25 6z"/></svg><span class="tip">Minimum score</span></button>
      <button class="mini-btn" data-mini="pctile" data-target="pctile" aria-label="Score percentile"><svg viewBox="0 0 16 16"><path d="M3 13V9.2M6.6 13V5.4M10.2 13V7.4M13.8 13V3"/></svg><span class="tip">Score percentile</span></button>
      <button class="mini-btn" data-mini="sort" data-target="sort" aria-label="Sort by"><svg viewBox="0 0 16 16"><path d="M4.8 3.2v9.6M4.8 12.8 2.4 10.4M4.8 12.8l2.4-2.4M11.2 12.8V3.2M11.2 3.2 8.8 5.6M11.2 3.2l2.4 2.4"/></svg><span class="tip">Sort by</span></button>
      <div class="mini-sep"></div>
      <button class="mini-btn" data-mini="hideFiltered" data-target="hideFiltered" aria-label="Hide entitled &amp; recently sold"><svg viewBox="0 0 16 16"><path d="M1.6 8S4.1 3.6 8 3.6 14.4 8 14.4 8 11.9 12.4 8 12.4 1.6 8 1.6 8z"/><circle cx="8" cy="8" r="1.85"/></svg><span class="tip">Hide entitled &amp; recently sold</span></button>
      <button class="mini-btn" data-mini="reset" data-target="resetBtn" aria-label="Reset filters"><svg viewBox="0 0 16 16"><path d="M13.2 8a5.2 5.2 0 1 1-1.7-3.85"/><path d="M13.6 2.2v3.1h-3.1"/></svg><span class="tip">Reset filters</span></button>
    </div>
    <p class="sidebar-title">Filters</p>
    <div class="filter-grid">
      <label>City<select id="city"><option value="">All cities</option></select></label>
      <label>Zipcode<select id="zip"><option value="">All zipcodes</option></select></label>
      <label>Address<input id="search" placeholder="Address"></label>
      <label>Lot size range (acres)<div style="display:flex;gap:6px">
        <input id="min_lot_acres" type="number" min="0" step="0.1" placeholder="Min">
        <input id="max_lot_acres" type="number" min="0" step="0.1" placeholder="Max">
      </div></label>
      <label>Minimum score<input id="min_score" type="number" min="0" max="100" value="0"></label>
      <label title="Ranks against the whole dataset, not the current filter, so the tiers line up with the score colours: Top 5% is exactly the green band, Top 25% the amber one.">Score percentile<select id="pctile">
        <option value="">All properties</option>
        <option value="0.95">Top 5% (green tier)</option>
        <option value="0.90">Top 10%</option>
        <option value="0.75">Top 25% (amber tier)</option>
        <option value="0.50">Top 50%</option>
      </select></label>
      <label>Sort by<select id="sort">
        <option value="opportunity_score">Total Score</option>
        <option value="business_score">Business Value</option>
        <option value="property_score">Distressed Signal</option>
        <option value="buildability_score">Buildability</option>
        <option value="visual_score">Visual Condition</option>
        <option value="data_coverage">Data Coverage</option>
        <option value="lot_sqft">Lot Size</option>
      </select></label>
      <label class="toggle-chip" title="Hides properties with a development already approved, under construction, or in a city review pipeline, plus any property sold within the last 5 years. Both are sites someone else has already moved on. Untick to see the full list."><input type="checkbox" id="hideFiltered" checked> Hide entitled &amp; recently sold</label>
      <button id="resetBtn" class="ghost">Reset filters</button>
    </div>
  </aside>
  <main class="content">
  <section class="kpis">
    <article><span class="info-icon" tabindex="0">i<span class="info-popover"><span class="pop-title">Matching Properties</span><ul><li>Count of properties currently shown after your filters (city, zip, address, lot size, minimum score) are applied.</li></ul></span></span><span>Matching Properties</span><strong id="kpiCount">—</strong></article>
    <article><span class="info-icon" tabindex="0">i<span class="info-popover"><span class="pop-title">Overall Opportunity Score (blend of all 4 layers)</span><ul><li>Weighted average of Buildability (<b>35%</b>), Condition (<b>25%</b>), Distressed Signal (<b>25%</b>), and Business Value (<b>15%</b>).</li><li>Multiplied by a confidence factor (85%&ndash;100%) based on how much underlying data was available &mdash; a property we know less about is scored a bit more conservatively.</li><li>Higher score = stronger opportunity.</li></ul></span></span><span>Avg. Overall Score</span><strong id="kpiAvg">—</strong></article>
    <article><span class="info-icon" tabindex="0">i<span class="info-popover"><span class="pop-title">Buildability (35% of Overall Score)</span><ul><li><b>FAR</b> &le;0.25: 30pts &middot; &le;0.50: 15pts &middot; higher: 0pts (raised to 15 if zoned + lot &ge;0.5 acre &mdash; existing building doesn't rule out redevelopment on a confirmed-buildable site)</li><li><b>Land value</b> &ge;75% of assessed value: 25pts &middot; &ge;60%: 12pts (blended with FAR, not stacked &mdash; they're correlated signals)</li><li><b>Lot size</b>: &ge;90th percentile of dataset: 20pts &middot; &ge;75th: 10pts</li><li><b>Building age</b>: pre-1970: 10pts &middot; pre-1980: 5pts</li><li><b>Zoning + General Plan</b> townhome fit: up to 25pts &mdash; full credit from either signal alone, a bonus if both agree</li></ul></span></span><span>Avg. Buildability Score</span><strong id="kpiBuildability">—</strong></article>
    <article><span class="info-icon" tabindex="0">i<span class="info-popover"><span class="pop-title">Condition (25% of Overall Score)</span><ul><li>AI (Gemini) reads the property's photo and rates it: <b>Poor</b> 90pts &middot; <b>Fair</b> 60pts &middot; <b>Good</b> 25pts &middot; <b>Excellent</b> 10pts.</li><li>A busy parking lot/garage can soften a "declining" business read; an empty one is never held against it (could just be time of day).</li><li>Higher score = worse visible condition = more opportunity.</li></ul></span></span><span>Avg. Condition Score</span><strong id="kpiVisual">—</strong></article>
    <article><span class="info-icon" tabindex="0">i<span class="info-popover"><span class="pop-title">Distressed Signal (25% of Overall Score)</span><ul><li><b>Vacant</b>: 20pts &middot; <b>Foreclosure/auction/bank-owned</b>: 20pts &middot; <b>Pre-probate/estate</b>: 15pts</li><li><b>High equity</b> &ge;70%: 15pts &middot; &ge;40%: 8pts</li><li><b>Long ownership</b> &ge;15yrs: 10pts &middot; &ge;8yrs: 5pts</li><li><b>Reported poor/fixer condition</b> in source data: 15pts</li></ul></span></span><span>Avg. Distressed Score</span><strong id="kpiProperty">—</strong></article>
    <article><span class="info-icon" tabindex="0">i<span class="info-popover"><span class="pop-title">Business Value (15% of Overall Score)</span><ul><li>Base: <b>declining-category or closed</b> business: 85pts &middot; other active business: 35pts.</li><li>Adjusted by Google review volume/rating: few reviews + low rating pushes toward declining even without a category match; many reviews + high rating pulls toward thriving even if the category matched a declining keyword.</li><li>Multi-tenant sites (e.g. a shopping center) are judged by their most-reviewed tenant, not an arbitrary listing.</li></ul></span></span><span>Avg. Business Value Score</span><strong id="kpiBusiness">—</strong></article>
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
          <th title="Development status, name, and source for entitled/pipeline sites — click any cell to add your own notes (saved in this browser)">Notes</th>
        </tr></thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
  </section>
  </main>
</div>
<dialog id="detail"><button class="close" aria-label="Close">×</button><div id="detailBody"></div></dialog>
<dialog id="imgLightbox"><button class="close" aria-label="Close">×</button><figure style="margin:0"><img id="lightboxImg" src="" alt=""><figcaption id="lightboxCaption"></figcaption></figure></dialog>
<script>
const DATA = {data_json};
const PIPELINE_SOURCES = {sources_json};
function percentile(arr, p){{
  const vals = arr.map(r=>r.opportunity_score).filter(v=>v!=null).sort((a,b)=>a-b);
  if(!vals.length) return 0;
  return vals[Math.min(vals.length-1, Math.floor(p*(vals.length-1)))];
}}
const HIGH_THRESHOLD = percentile(DATA, 0.95);
const MID_THRESHOLD = percentile(DATA, 0.75);
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
        minScore=+$('#min_score').value||0, minLotAcres=+$('#min_lot_acres').value||0,
        maxLotAcres=+$('#max_lot_acres').value||Infinity,
        hideFiltered=$('#hideFiltered').checked,
        pctRaw=$('#pctile').value,
        // Threshold comes from the FULL dataset, so "Top 5%" means the same
        // properties no matter what else is filtered -- same basis as the
        // score colours.
        pctFloor=pctRaw ? percentile(DATA, +pctRaw) : null;
  let rows = DATA.filter(r=>
    (!city||r.city===city) &&
    (!zip||r.zip===zip) &&
    (r.opportunity_score>=minScore) &&
    ((r.lot_sqft||0)/43560>=minLotAcres) &&
    ((r.lot_sqft||0)/43560<=maxLotAcres) &&
    (!q || r.address.toLowerCase().includes(q)) &&
    (!hideFiltered || !(r.is_entitled_pipeline || (r.years_held!=null && r.years_held<5))) &&
    (pctFloor===null || r.opportunity_score>=pctFloor)
  );
  rows.sort(sortRows);
  updateSortIndicators();
  markActiveFilters();
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
    <td class="property-cell"><div class="address" title="Open in Google Maps"><a href="https://www.google.com/maps/search/?api=1&query=${{encodeURIComponent(r.address+', '+r.city+', CA '+(r.zip||''))}}" target="_blank" rel="noreferrer">${{r.address}}</a>${{r.is_entitled_pipeline?` <span class="chip" style="background:#FEF3C7;color:#B45309" title="${{(r.entitled_note||'Known entitled/pipeline project').replace(/"/g,'&quot;')}}">entitled</span>`:''}}</div><div class="sub">${{r.city}} ${{r.zip}}${{r.apn?` · ${{r.apn}}`:''}}</div>${{r.property_url?`<div class="sub"><a href="${{r.property_url}}" target="_blank" rel="noreferrer">Property record ↗</a></div>`:''}}</td>
    <td class="business-cell"><span class="chip ${{r.business_label}}">${{r.business_label}}</span><div class="sub" title="${{r.business_name||''}}">${{r.business_name||'—'}}${{r.business_category?` · ${{r.business_category}}`:''}}</div>${{r.business_rating?`<div class="sub">${{r.business_rating.toFixed(1)}}★ (${{num(r.business_reviews)}} reviews)</div>`:''}}</td>
    <td><span class="score ${{r.opportunity_score>=HIGH_THRESHOLD?'high':r.opportunity_score>=MID_THRESHOLD?'mid':''}}">${{r.opportunity_score.toFixed(1)}}</span></td>
    <td>${{r.business_score===null?'—':r.business_score.toFixed(0)}}</td>
    <td>${{r.property_score.toFixed(0)}}</td>
    <td>${{r.buildability_score.toFixed(0)}}</td>
    <td>${{r.visual_score===null?'—':r.visual_score.toFixed(0)}}</td>
    <td>${{Math.round(r.data_coverage*100)}}%</td>
    <td class="reason">${{r.reasons||'—'}}</td>
    ${{noteCellHtml(r)}}
  </tr>`).join('');
  const byKey = Object.fromEntries(rows.map(r=>[rowKey(r),r]));
  document.querySelectorAll('#rows tr').forEach(tr=>{{
    const key = tr.dataset.key;
    tr.querySelector('.rowCheck').addEventListener('change',e=>{{
      if (e.target.checked) selected.add(key); else selected.delete(key);
      updateSelection();
    }});
    tr.addEventListener('click',e=>{{
      if (e.target.closest('.rowCheck')||e.target.closest('a')||e.target.closest('.thumb')||e.target.closest('.note-cell')) return;
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
  $('#detailBody').innerHTML = `<p class="eyebrow">${{r.city.toUpperCase()}} · ${{r.zip}}</p><h2><a href="https://www.google.com/maps/search/?api=1&query=${{encodeURIComponent(r.address+', '+r.city+', CA '+(r.zip||''))}}" target="_blank" rel="noreferrer" style="color:inherit;text-decoration:none" title="Open in Google Maps">${{r.address}}</a></h2><p class="sub">${{r.apn?`APN ${{r.apn}}`:''}}</p>
  <div class="detail-grid">
    <div><span>Opportunity score</span><strong>${{r.opportunity_score.toFixed(1)}}</strong></div>
    <div><span>Business value</span><strong>${{r.business_score===null?'—':r.business_score.toFixed(0)}}</strong></div>
    <div><span>Distressed signal</span><strong>${{r.property_score.toFixed(0)}}</strong></div>
    <div><span>Buildability</span><strong>${{r.buildability_score.toFixed(0)}}</strong></div>
    <div><span>Visual condition</span><strong>${{r.visual_score===null?'—':r.visual_score.toFixed(0)}}</strong></div>
    <div title="Share of scoring data available, not a probability of selling or getting approved"><span>Data coverage</span><strong>${{Math.round(r.data_coverage*100)}}%</strong></div>
    <div><span>Lot size</span><strong>${{lotSize(r.lot_sqft)}}</strong></div>
    <div><span>Building size</span><strong>${{num(r.building_sqft)}} sqft</strong></div>
    <div><span>Est. value</span><strong>${{money(r.est_value)}}</strong></div>
    <div><span>Zoning</span><strong>${{r.zone_code||'—'}}</strong></div>
    <div><span>Last sale (recorded)</span><strong>${{r.last_sale_date||'—'}}</strong></div>
    <div><span>Years held</span><strong>${{r.years_held!=null?r.years_held.toFixed(1)+' yrs':'—'}}</strong></div>
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
    'Notes': `PropScore ${{r.opportunity_score.toFixed(1)}}: ${{r.reasons||''}}${{(ROW_NOTES[rowKey(r)]??pipelineDefaultNote(r))?` | Note: ${{ROW_NOTES[rowKey(r)]??pipelineDefaultNote(r)}}`:''}}`,
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
  ['city','zip','search','min_score','min_lot_acres','max_lot_acres','hideFiltered','pctile'].forEach(id=>$('#'+id).addEventListener(id==='search'?'input':'change',apply));
  $('#sort').addEventListener('change',()=>{{sortKey=$('#sort').value;sortAsc=false;apply()}});
  // Reset returns to the site's ENTRY state — both pipeline/recent-sale
  // filters back ON (the default view), not a fully unfiltered view.
  $('#resetBtn').onclick=()=>{{$('#city').value='';$('#zip').value='';$('#search').value='';$('#min_score').value=0;$('#min_lot_acres').value='';$('#max_lot_acres').value='';$('#sort').value='opportunity_score';$('#pctile').value='';$('#hideFiltered').checked=true;sortKey=null;sortAsc=false;apply()}};
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
// ── Per-property Notes column ────────────────────────────────────────
// Entitled/pipeline rows are pre-filled with a category line ("Already
// being developed" / "Entitled (approved)" / "In the pipeline"), the
// development name, status, and source (from PIPELINE_SOURCES). Every
// row's cell is click-to-edit; edits overlay the defaults via
// localStorage, so they persist across visits IN THIS BROWSER only
// (static site, no backend — notes are per-viewer, not shared).
const ROW_NOTES_KEY = 'propscore_row_notes_v1';
function loadRowNotes(){{ try {{ return JSON.parse(localStorage.getItem(ROW_NOTES_KEY)) || {{}}; }} catch(e) {{ return {{}}; }} }}
let ROW_NOTES = loadRowNotes();
function saveRowNotes(){{ try {{ localStorage.setItem(ROW_NOTES_KEY, JSON.stringify(ROW_NOTES)); }} catch(e) {{}} }}
function pipelineDefaultNote(r){{
  if(!r.is_entitled_pipeline) return '';
  const note = r.entitled_note || '';
  const sep = note.indexOf(' — ');
  const name = sep>0 ? note.slice(0,sep) : (note || 'Known pipeline project');
  const status = sep>0 ? note.slice(sep+3) : 'see city records';
  const s = status.toLowerCase();
  const category = s.includes('under construction') ? 'ALREADY BEING DEVELOPED'
                 : s.includes('approved') ? 'ENTITLED (APPROVED)'
                 : 'IN THE PIPELINE';
  const source = PIPELINE_SOURCES[name] || 'City planning records';
  return `${{category}} — ${{name}}. Status: ${{status}}. Source: ${{source}}.`;
}}
function noteCellHtml(r){{
  const key = rowKey(r);
  const stored = ROW_NOTES[key];
  const text = stored!=null ? stored : pipelineDefaultNote(r);
  const cls = 'note-cell' + (r.is_entitled_pipeline?' pipeline':'') + (stored!=null?' user-edited':'');
  const esc = s=>String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  return `<td class="${{cls}}" contenteditable="true" spellcheck="false" data-notekey="${{key.replace(/"/g,'&quot;')}}" title="Click to edit — saves in this browser">${{esc(text)}}</td>`;
}}
$('#rows').addEventListener('focusout', e=>{{
  const td = e.target.closest('.note-cell');
  if(!td) return;
  const key = td.dataset.notekey;
  ROW_NOTES[key] = td.textContent.trim();
  saveRowNotes();
  td.classList.add('user-edited');
}});
// Sidebar show/hide, remembered per browser.
const NAV_KEY='propscore_nav_mini';
const shellEl=document.querySelector('.shell');
try{{ if(localStorage.getItem(NAV_KEY)==='1') shellEl.classList.add('nav-mini'); }}catch(e){{}}
function setMini(on){{
  shellEl.classList.toggle('nav-mini',on);
  try{{ localStorage.setItem(NAV_KEY,on?'1':'0'); }}catch(e){{}}
}}
$('#sidebarToggle').onclick=()=>setMini(!shellEl.classList.contains('nav-mini'));
// Clicking a rail icon expands the panel and jumps to that control; the reset
// icon just resets without expanding.
document.querySelectorAll('.mini-btn').forEach(b=>{{
  b.addEventListener('click',()=>{{
    if(b.dataset.mini==='reset'){{ $('#resetBtn').click(); return; }}
    setMini(false);
    const el=document.getElementById(b.dataset.target);
    if(el) setTimeout(()=>{{ try{{el.focus({{preventScroll:true}});}}catch(e){{}} }},200);
  }});
}});
// Mark which filters are actually narrowing the results, so the collapsed rail
// still tells you something is applied.
function markActiveFilters(){{
  const on={{
    city: !!$('#city').value, zip: !!$('#zip').value, search: !!$('#search').value.trim(),
    lot: !!($('#min_lot_acres').value||$('#max_lot_acres').value),
    min_score: (+$('#min_score').value||0)>0, pctile: !!$('#pctile').value,
    sort: $('#sort').value!=='opportunity_score', hideFiltered: $('#hideFiltered').checked, reset:false
  }};
  document.querySelectorAll('.mini-btn').forEach(b=>b.classList.toggle('active',!!on[b.dataset.mini]));
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
            "address", "city", "state", "zip", "apn", "is_entitled_pipeline", "entitled_note", "property_url", "zone_code", "zone_info",
            "owner", "owner_first_name", "owner_last_name",
            "phone_number", "mailing_address", "mail_street", "mail_city", "mail_state", "mail_zip",
            "email1", "email2", "phones",
            "lot_sqft", "building_sqft", "year_built", "est_value",
            "last_sale_date", "years_held",
            "business_label", "business_name", "business_category",
            "business_reviews", "business_rating",
            "business_score", "property_score", "buildability_score", "visual_score", "data_coverage",
            "opportunity_score", "image_path", "condition_rating", "vision_notes", "reasons",
            "parking_activity", "photo_source", "photo_date",
        )})
    favicon_b64 = base64.b64encode(Path(config.LOGO_FAVICON_FILE).read_bytes()).decode("ascii")
    logo_b64 = base64.b64encode(Path(config.LOGO_HEADER_FILE).read_bytes()).decode("ascii")
    html = HTML_TEMPLATE.format(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        count=len(records),
        favicon_b64=favicon_b64,
        logo_b64=logo_b64,
        data_json=json.dumps(slim),
        sources_json=json.dumps(config.ENTITLED_PROJECT_SOURCES),
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
        "last_sale_date", "years_held", "is_entitled_pipeline", "entitled_note",
        "property_url", "image_path",
        "image_status", "vision_status", "parking_activity", "photo_source", "photo_date",
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
    with_business = sum(1 for p in properties if p["businesses"])
    print(f"  Business data already joined per-property: {with_business}/{len(properties)} matched")

    print("Loading zoning...")
    zoning_gdf = load_zoning()
    if zoning_gdf is not None:
        zoned = join_zoning(properties, zoning_gdf)
        with_coords = sum(1 for p in properties if p["lat"] is not None and p["lng"] is not None)
        print(f"  {zoned}/{with_coords} geocoded properties matched to a zoning parcel "
              f"({len(properties) - with_coords} properties have no lat/long to join on)")
    else:
        print(f"  WARNING: zoning file not found at {config.ZONING_GEOJSON_FILE} — skipping zoning signal")

    for city_name, (geojson_path, field) in getattr(config, "CITY_ZONING_GEOJSON", {}).items():
        city_gdf = load_zoning(geojson_path, field)
        if city_gdf is None:
            print(f"  WARNING: zoning file not found for {city_name} at {geojson_path}")
            continue
        city_properties = [p for p in properties if p["city"] == city_name]
        zoned = join_zoning(city_properties, city_gdf)
        with_coords = sum(1 for p in city_properties if p["lat"] is not None and p["lng"] is not None)
        print(f"  {city_name}: {zoned}/{with_coords} geocoded properties matched to a zoning parcel "
              f"({len(city_properties) - with_coords} have no lat/long to join on)")

    if args.limit:
        properties = properties[: args.limit]
        print(f"Limiting to first {len(properties)} properties")

    lot_p75, lot_p90 = compute_lot_percentiles(properties)
    print(f"  Lot sqft p75/p90 across dataset: {lot_p75:.0f} / {lot_p90:.0f}" if lot_p90 else "  Lot percentiles unavailable")

    print(f"Scoring {len(properties)} properties (workers={args.workers})...")
    results = []
    done = 0
    session = requests.Session()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(process_property, p, lot_p75, lot_p90, session, args.skip_vision, args.skip_images): p
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
    # CSV first: it's the more essential structured output, and much
    # cheaper to write — if the larger HTML render hits a resource issue,
    # the scored data is still safely on disk either way.
    render_csv(results, csv_path)
    render_html(results, html_path)

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
