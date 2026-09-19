"""
build_regional_master.py
-------------------------
Generalized version of build_atherton_master.py — merges each city's 4
source datasets (PropertyRadar, LandVision, PropStream, Google Maps
business scrape) into a per-city cleaned master, then concatenates all
cities into one combined master file.

Outputs (per city + combined):
    <city>_merged_properties.xlsx              raw side-by-side, source-
                                                 prefixed columns (PR_/LV_/PS_)
    <city>_commercial_properties_cleaned.xlsx   flattened one-row-per-property
                                                 master + joined Google Maps
                                                 business data
    combined_commercial_properties_cleaned.xlsx all cities' cleaned masters
                                                 concatenated, with a "City"
                                                 column carried through to
                                                 distinguish them
"""
import re
import pandas as pd


def normalize_date(v):
    """The three sale-date sources each use a different format (PropStream
    "2020-11-19", LandVision "8/5/2015 12:00:00 AM", PropertyRadar mixed) --
    normalize whatever comes through pick() to plain M/D/YYYY (no leading
    zeros, no time component) so the report doesn't show three different
    date styles side by side. Built manually rather than via strftime's
    "%-m"/"%-d" (no-leading-zero) codes, which are Linux/Mac-only -- Windows
    needs "%#m"/"%#d" instead, so a platform-specific format string would
    silently break on the other OS."""
    if v is None:
        return None
    try:
        ts = pd.to_datetime(v, errors="coerce")
    except (ValueError, TypeError):
        return None
    if pd.isna(ts):
        return None
    return f"{ts.month}/{ts.day}/{ts.year}"

ROOT = r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring"

CITIES = [
    {
        "name": "Atherton",
        "pr": "Atherton Property Radar Dataset.csv",
        "lv": "Atherton LandVision Dataset.csv",
        "ps": "Property Export Atherton+Commercial PropStream Dataset.csv",
        "gm": "Atherton Google Map Business Dataset.csv",
    },
    {
        "name": "Los Altos",
        "pr": "Los Altos Property Radar Dataset.csv",
        "lv": "Los Altos LandVision Dataset Updated.csv",
        "ps": "Los Altos PropStream Dataset Updated.csv",
        "gm": "Los Altos Google Map Business Dataset.csv",
    },
    {
        "name": "Menlo Park",
        "pr": "Menlo Park Property Radar Dataset.csv",
        "lv": "Menlo Park Landvision Dataset.csv",
        "ps": "Menlo Park PropStream Dataset Updated.xlsx",
        "gm": "Menlo Park Google Map Business Dataset.csv",
    },
    {
        "name": "Palo Alto",
        "pr": "Palo Alto Property Radar Dataset.csv",
        "lv": "Palo Alto LandVision Dataset.csv",
        # Swapped for a larger, more complete PropStream re-export (1,215
        # rows vs. the original 402) — verified 88% overlap with the old
        # file/LandVision/PropertyRadar, plus 103 genuinely new properties.
        "ps": "Property Export Palo+Alto+Commercial PropStream Dataset v2.xlsx",
        "gm": "Palo Alto Google Map Businesses Dataset.csv",
    },
]
# New cities must be APPENDED to the end of this list, never inserted or
# reordered — the scoring pipeline's vision cache is keyed by row index, so
# keeping earlier cities' row order stable means their cached Street
# View/Gemini results stay valid and only newly-added cities need fresh
# API calls on the next scoring run.


def read_table(path):
    return pd.read_excel(path) if path.lower().endswith((".xlsx", ".xls")) else pd.read_csv(path, low_memory=False)


def normalize_address(address):
    addr = str(address or "").split(",")[0].strip().upper()
    addr = re.sub(r"[^\w\s]", "", addr)
    suffix_pattern = (
        r"\b(STREET|AVENUE|DRIVE|LANE|WAY|BOULEVARD|ROAD|COURT|PLACE|"
        r"TERRACE|CIRCLE|HIGHWAY|FREEWAY|LOOP|RUN|TRAIL|PASS|PARKWAY|"
        r"ST|AVE|DR|LN|BLVD|RD|CT|PL|TER|CIR|HWY|FWY|TRL|PKWY)\b"
    )
    addr = re.sub(suffix_pattern, "", addr)
    addr = re.sub(r"\s+", " ", addr).strip()
    return addr


def clean_apn(v):
    s = str(v or "").strip()
    return s if s and s.lower() != "nan" else ""


# City/county/special-district/university-owned parcels (parks, government
# buildings, universities, hospitals) aren't real acquisition targets — the
# owner is a public agency or a large institution, not a landlord who might
# sell. Matched against the OWNER name, not business category, since a
# private business can be a legitimate redevelopment candidate even if its
# category sounds institution-adjacent (a dental office is not a hospital).
# Every keyword here was checked against this session's actual owner-name
# data before being added — several obvious-seeming ones were rejected
# because they collided with real private businesses: bare "USA" matches
# "MCDONALDS USA LLC"/"CHEVRON USA INC", bare "UNIVERSITY"/"STANFORD" match
# "STANFORD FEDERAL CREDIT UNION"/"STANFORD THEATER FOUNDATION"/"119
# STANFORD LLC", and bare "SCHOOL" matches private schools like "GIRLS
# MIDDLE SCHOOL" which are legitimately private real estate. "HOSPITAL" is
# matched as a whole word so it doesn't fire on "Palmetto HOSPITALity".
INSTITUTIONAL_OWNER_KEYWORDS = [
    "CITY OF", "COUNTY OF", "TOWN OF", "STATE OF CALIFORNIA",
    "UNITED STATES OF AMERICA", "UNITED STATES POSTAL SERVICE",
    "SCHOOL DISTRICT", "SCHOOL DIST", "COMMUNITY COLLEGE DISTRICT",
    "FIRE PROTECTION DISTRICT", "FIRE PROTECTION DIS", "FIRE DISTRICT",
    "SANITARY DISTRICT", "WATER DISTRICT", "FLOOD CONTROL",
    "TRANSIT DISTRICT", "TRANSPORTATION AUTHORITY",
    "REDEVELOPMENT AGENCY", "HOUSING AUTHORITY",
    "MIDPENINSULA REGIONAL", "REGIONAL PARK DISTRICT",
    "OPEN SPACE TRUST", "OPEN SPACE DISTRICT",
    "LELAND STANFORD JR UNIVERSITY", "LELAND STANFORD JUNIOR UNIVERSITY",
]
INSTITUTIONAL_OWNER_REGEX = re.compile(
    "|".join(re.escape(kw) for kw in INSTITUTIONAL_OWNER_KEYWORDS) + r"|\bHOSPITAL\b"
)


def is_institutional_owner(owner):
    return bool(INSTITUTIONAL_OWNER_REGEX.search(str(owner or "").upper()))


# A thriving, fully-built-out multi-unit development (a real "town center" —
# an anchor-tenant shopping mall, or an occupied apartment complex) isn't a
# redevelopment target no matter how it scores on other signals — nobody is
# tearing down a healthy Trader Joe's-anchored mall. "Shopping mall" and
# "Apartment rental agency" are the categories actually observed in this
# session's Google Maps scrapes for exactly this pattern (checked against
# real data before adding, same as the institutional-owner list).
BUILT_OUT_COMPLEX_CATEGORIES = {"SHOPPING MALL", "APARTMENT RENTAL AGENCY"}
UNITS_EXCLUDE_THRESHOLD = 10


def primary_business_category(business_category_field, sep="||"):
    """Business Category is "||"-joined, sorted by review count (see
    business_fields()) — the first entry is the most prominent tenant."""
    s = str(business_category_field or "")
    return s.split(sep)[0].strip().upper() if s else ""


def is_built_out_complex(business_category_field):
    return primary_business_category(business_category_field) in BUILT_OUT_COMPLEX_CATEGORIES


def normalize_apn_digits(v):
    return re.sub(r"[^0-9]", "", str(v or ""))


def load_units_by_apn():
    """Real existing-unit counts, keyed by digit-only normalized APN, per
    city — only Los Altos's parcel GIS layer (downloaded this session) has a
    genuine NUMBER_OF_UNITS field; PropertyRadar/LandVision/PropStream carry
    no true unit-count field for any city, so other cities are handled only
    by the built-out-complex category check above."""
    result = {}
    try:
        import geopandas as gpd
        la = gpd.read_file(rf"{ROOT}\los_altos_parcels.geojson")
        result["Los Altos"] = {
            normalize_apn_digits(row["APN"]): row["NUMBER_OF_UNITS"]
            for _, row in la.iterrows()
            if pd.notna(row.get("NUMBER_OF_UNITS"))
        }
    except (ImportError, FileNotFoundError, OSError):
        pass
    return result


CITY_UNITS_BY_APN = load_units_by_apn()


def clean_zip(v):
    """Zip codes come through pandas as floats (e.g. 94301.0) when the
    source column has any missing values; strip that to a plain digit
    string. Same bug/fix as the Santa Clara scoring pipeline — that fix
    was never ported into this merge script until now."""
    s = str(v or "").strip()
    if not s or s.lower() == "nan":
        return None
    return s[:-2] if s.endswith(".0") else s


def normalize_owner(owner):
    """Loose owner-name match for the address-dedup step below — catches
    "Mc Candless Limited" vs "Mccandless Limited" style spacing/punctuation
    variants without conflating genuinely different owners."""
    return re.sub(r"[^A-Z0-9]", "", str(owner or "").upper())


# Palo Alto foothill parcels that pass every other filter but are clearly
# not commercial/redevelopment targets: a golf & country club, a nonprofit
# farm/wilderness preserve (PropStream mislabels these as "Warehouse
# (Industrial)" — simply wrong), and a public open-space-district parcel.
# All are 115-480 acres with no building sqft. Identified by APN since
# their Zoning field is blank or an unrecognized code ("AW"), so the
# zoning-filter regex can't catch them.
PALO_ALTO_EXCLUDE_APNS = {
    "182-35-035",  # 3000 Alexis Dr — Palo Alto Hills Golf & Country Club
    "351-04-017", "351-04-018", "351-04-028", "351-06-002",  # Montebello Rd — Hidden Villa Trust
    "351-12-006",  # 1405 Skyline Blvd — Midpeninsula Regional Open Space District
}


def pick(row, *candidates):
    for c in candidates:
        v = row.get(c)
        # pd.isna() catches None/NaN/NaT in one call -- a plain string
        # comparison against "nan" (the old check) misses NaT, pandas' own
        # sentinel for a missing datetime value, which stringifies to "NaT"
        # and would otherwise be treated as a real value.
        if not pd.isna(v) and str(v).strip():
            return v
    return None


def pick_bool(row, *candidates):
    v = pick(row, *candidates)
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("1", "1.0", "yes", "y", "true"):
        return "Yes"
    if s in ("0", "0.0", "no", "n", "false"):
        return "No"
    return v


def palo_alto_drop_zoning(zoning):
    """True if this zoning code should be EXCLUDED from the Palo Alto
    master: confirmed single-family residential, Public Facility, or Open
    Space (verified against Palo Alto's actual municipal code — Chapter
    18.16 for CN/CC/CS/CD commercial, Chapter 18.20 for RP/GM/MOR/ROLM
    office-research-manufacturing). Multi-family (RM-series) and Planned
    Community (PC) residential are deliberately KEPT — they're
    income-producing apartment properties, not noise, per user direction.

    RT and a bare R- prefix used to be in the single-family pattern below,
    but the city's General Plan Housing Element confirms RT-35/RT-50 are
    actually Palo Alto's high-density multi-family zones (up to 50
    units/acre) — the opposite of single-family — and the bare R- prefix
    is broad enough to also catch real multi-family codes like R-4. Both
    were removed after that was verified against the General Plan PDF."""
    z = str(zoning or "").strip().upper()
    if re.match(r"^(R1|R2|R3G|RA)\b", z) or z in ("R1929", "R1743", "R1B7", "R1B8"):
        return True  # single-family residential
    if re.match(r"^(PF|ZN PF)", z):
        return True  # public facility
    if z in ("OS", "0S"):
        return True  # open space
    return False


SUM_FIELDS = ["Lot Sqft", "Building Sqft", "Assessed Value", "Land Value"]
FIRST_NONNULL_FIELDS = [
    "City", "Zip", "Owner", "Zoning", "Property Type (land use category)",
    "Year Built", "Estimated Market Value", "Owner Occupied", "Vacant",
    "Foreclosure", "Equity %", "LATITUDE", "LONGITUDE", "Mailing Address",
    "Phone", "Email", "Business Name", "Business Category", "Business City",
    "Business Reviews", "Business Rating", "Business Closed", "Business Photo URL",
    "Last Sale Recording Date",
]


def merge_duplicate_addresses(cleaned_df, city_name):
    """Two properties normalizing to the same street address are usually
    either (a) the same physical site split across multiple assessor
    parcels — should be one row, physical fields summed, e.g. the parking
    lot + building parcels behind one restaurant — or (b) genuinely
    different properties that happen to collide after address
    normalization. Grouping by (address, owner) instead of address alone
    tells them apart: same owner at the same address is almost certainly
    (a); different owners are almost certainly (b) and are left untouched."""
    cleaned_df = cleaned_df.copy()
    cleaned_df["_owner_key"] = cleaned_df["Owner"].apply(normalize_owner)
    group_cols = ["norm_addr", "_owner_key"]

    dupe_mask = cleaned_df.duplicated(subset=group_cols, keep=False) & (cleaned_df["_owner_key"] != "")
    if not dupe_mask.any():
        return cleaned_df.drop(columns="_owner_key")

    kept_rows = [row for _, row in cleaned_df[~dupe_mask].iterrows()]
    merged_count = 0
    for _, group in cleaned_df[dupe_mask].groupby(group_cols):
        merged = group.iloc[0].copy()
        merged["APN"] = "; ".join(sorted(set(group["APN"].dropna().astype(str)) - {"", "nan"}))
        for f in SUM_FIELDS:
            vals = group[f].dropna()
            merged[f] = vals.sum() if len(vals) else None
        for f in FIRST_NONNULL_FIELDS:
            nonnull = group[f].dropna()
            merged[f] = nonnull.iloc[0] if len(nonnull) else None
        merged["source_count"] = group["source_count"].max()
        for f in ("in_PropertyRadar", "in_LandVision", "in_PropStream"):
            merged[f] = bool(group[f].any())
        kept_rows.append(merged)
        merged_count += 1

    result = pd.DataFrame(kept_rows).drop(columns="_owner_key").reset_index(drop=True)
    print(f"[{city_name}] Merged {dupe_mask.sum()} same-address/same-owner rows into {merged_count} "
          f"combined properties ({len(cleaned_df)} -> {len(result)})")
    return result


def build_city_master(city_name, pr_file, lv_file, ps_file, gm_file, zoning_filter=None, exclude_apns=None):
    pr = read_table(f"{ROOT}\\{pr_file}")
    pr = pr[pr["Address"].notna()].copy()
    pr["norm_addr"] = pr["Address"].apply(normalize_address)
    pr["apn"] = ""

    lv = read_table(f"{ROOT}\\{lv_file}")
    lv = lv[lv["SITE_ADDR"].notna()].copy()
    lv["norm_addr"] = lv["SITE_ADDR"].apply(normalize_address)
    lv["apn"] = lv["APN"].apply(clean_apn)

    ps = read_table(f"{ROOT}\\{ps_file}")
    ps = ps[ps["Address"].notna()].copy()
    ps["norm_addr"] = ps["Address"].apply(normalize_address)
    ps["apn"] = ps["APN"].apply(clean_apn)

    gm = read_table(f"{ROOT}\\{gm_file}")
    gm = gm[gm["address"].notna()].copy()
    gm["norm_addr"] = gm["street"].apply(normalize_address)

    print(f"[{city_name}] Loaded: PropertyRadar={len(pr)}, LandVision={len(lv)}, "
          f"PropStream={len(ps)}, GoogleMap={len(gm)}")

    def key_for(row):
        return f"APN:{row['apn']}" if row["apn"] else f"ADDR:{row['norm_addr']}"

    pr["key"] = pr.apply(key_for, axis=1)
    lv["key"] = lv.apply(key_for, axis=1)
    ps["key"] = ps.apply(key_for, axis=1)

    addr_to_apn = {}
    for df in (lv, ps):
        for _, r in df.iterrows():
            if r["apn"]:
                addr_to_apn.setdefault(r["norm_addr"], r["apn"])

    def reconcile_key(row):
        if not row["apn"] and row["norm_addr"] in addr_to_apn:
            return f"APN:{addr_to_apn[row['norm_addr']]}"
        return row["key"]

    pr["key"] = pr.apply(reconcile_key, axis=1)

    all_keys = sorted(set(pr["key"]) | set(lv["key"]) | set(ps["key"]))
    print(f"[{city_name}] Unique properties after APN/address reconciliation: {len(all_keys)}")

    merged_rows = []
    for key in all_keys:
        row = {"key": key}
        pr_match = pr[pr["key"] == key]
        lv_match = lv[lv["key"] == key]
        ps_match = ps[ps["key"] == key]

        row["in_PropertyRadar"] = len(pr_match) > 0
        row["in_LandVision"] = len(lv_match) > 0
        row["in_PropStream"] = len(ps_match) > 0
        row["source_count"] = sum([row["in_PropertyRadar"], row["in_LandVision"], row["in_PropStream"]])

        if len(pr_match):
            for c in pr.columns:
                if c != "key":
                    row[f"PR_{c}"] = pr_match.iloc[0][c]
        if len(lv_match):
            for c in lv.columns:
                if c != "key":
                    row[f"LV_{c}"] = lv_match.iloc[0][c]
        if len(ps_match):
            for c in ps.columns:
                if c != "key":
                    row[f"PS_{c}"] = ps_match.iloc[0][c]
        merged_rows.append(row)

    merged_df = pd.DataFrame(merged_rows)
    safe_name = city_name.lower().replace(" ", "_")
    merged_path = f"{ROOT}\\{safe_name}_merged_properties.xlsx"
    merged_df.to_excel(merged_path, index=False)
    print(f"[{city_name}] Wrote {merged_path} ({len(merged_df)} rows, {len(merged_df.columns)} cols)")

    cleaned_rows = []
    for _, row in merged_df.iterrows():
        address = pick(row, "LV_SITE_ADDR", "PS_Address", "PR_Address")
        apn = pick(row, "LV_APN", "PS_APN")
        c = {
            "Address": address,
            "City": (pick(row, "LV_SITE_CITY", "PS_City", "PR_City") or city_name).title(),
            "Zip": clean_zip(pick(row, "LV_SITE_ZIP", "PS_Zip", "PR_ZIP")),
            "APN": apn,
            "Owner": pick(row, "PS_Owner 1 Last Name", "LV_OWNER_NAME_1", "PR_Owner"),
            "Zoning": pick(row, "LV_ZONING", "PR_Zoning"),
            "Property Type (land use category)": pick(row, "PS_Property Type"),
            "Lot Sqft": pick(row, "LV_LAND_SQFT", "PS_Lot Size Sqft", "PR_Lot SqFt"),
            "Building Sqft": pick(row, "LV_BUILDING_SQFT", "PS_Building Sqft", "PR_Sq Ft"),
            "Year Built": pick(row, "LV_YR_BLT", "PS_Year Built", "PR_Yr Built"),
            "Assessed Value": pick(row, "LV_VAL_ASSD", "PS_Total Assessed Value"),
            "Estimated Market Value": pick(row, "PS_Est. Value", "PR_Est Value"),
            "Land Value": pick(row, "LV_VAL_ASSD_LAND", "PS_Assessed Land Value"),
            "Owner Occupied": pick_bool(row, "PS_Owner Occupied", "PR_Owner Occ?"),
            "Vacant": pick_bool(row, "PS_Vacant", "LV_VACANT_LOT"),
            "Foreclosure": pick_bool(row, "PR_Foreclosure?"),
            "Equity %": pick(row, "PS_Est. Loan-to-Value", "PR_Est Equity %"),
            "LATITUDE": pick(row, "LV_LATITUDE"),
            "LONGITUDE": pick(row, "LV_LONGITUDE"),
            "Mailing Address": pick(row, "PS_Mailing Address", "LV_MAIL_ADDR", "PR_Mail Address"),
            # Three independent sources each cover roughly half the dataset
            # on their own (PropStream 50%, LandVision 53%, PropertyRadar
            # 55%) but barely overlap -- combining them via fallback lifts
            # real coverage to ~78%. PropStream's own recording-date field is
            # the most literal match for "recording date"; LandVision's
            # DATE_TRANSFER is the county-recorded transfer date (same
            # concept); PropertyRadar's Purchase Date is the closest
            # available fallback (a contract/purchase date, not explicitly
            # labeled "recording") when neither of the first two has a value.
            "Last Sale Recording Date": normalize_date(pick(row, "PS_Last Sale Recording Date", "LV_DATE_TRANSFER", "PR_Purchase Date")),
            "Phone": pick(row, "PS_Phone 1", "PR_Primary Phone1", "LV_OWNER_PHONE"),
            "Email": pick(row, "PS_Email 1", "PR_Primary Email1"),
            "in_PropertyRadar": row["in_PropertyRadar"],
            "in_LandVision": row["in_LandVision"],
            "in_PropStream": row["in_PropStream"],
            "source_count": row["source_count"],
        }
        c["norm_addr"] = normalize_address(address)
        cleaned_rows.append(c)

    cleaned_df = pd.DataFrame(cleaned_rows)

    gm_by_addr = {}
    for _, r in gm.iterrows():
        gm_by_addr.setdefault(r["norm_addr"], []).append(r)

    BIZ_SEP = "||"  # delimiter for multi-tenant properties — see below

    def business_fields(norm_addr):
        matches = gm_by_addr.get(norm_addr, [])
        if not matches:
            return pd.Series({"Business Name": None, "Business Category": None,
                               "Business City": None, "Business Reviews": None,
                               "Business Rating": None, "Business Closed": None,
                               "Business Photo URL": None})
        # A single address can be a multi-tenant building (a shopping center
        # anchored by a supermarket, with several smaller tenants) — keep
        # EVERY matched business, not just the first one the scrape happened
        # to list, so build_report.py's classify_business() can judge the
        # site by its most prominent tenant instead of an arbitrary one.
        # Sorted by review count (most prominent/established first) so the
        # primary tenant is easy to recover even without re-parsing rank.
        matches_sorted = sorted(matches, key=lambda r: (r.get("reviewsCount") or 0), reverse=True)
        primary = matches_sorted[0]

        def join(key, cast=str):
            vals = []
            for r in matches_sorted:
                v = r.get(key)
                vals.append(cast(v) if pd.notna(v) else "")
            return BIZ_SEP.join(vals)

        return pd.Series({
            "Business Name": join("title"),
            "Business Category": join("categoryName"),
            "Business City": primary["city"],
            "Business Reviews": join("reviewsCount"),
            "Business Rating": join("totalScore"),
            "Business Closed": BIZ_SEP.join(
                str(bool(r.get("permanentlyClosed")) or bool(r.get("temporarilyClosed")))
                for r in matches_sorted
            ),
            # Google Maps listing photo (business-uploaded or recent visitor
            # photo) — often much fresher than a Street View drive-by capture,
            # which may be years old. Used as the preferred vision-analysis
            # image in build_report.py when available, falling back to Street
            # View otherwise. Only the primary (most-reviewed) tenant's photo
            # is kept — one representative image per property.
            "Business Photo URL": (str(primary.get("imageUrl")).strip() if pd.notna(primary.get("imageUrl")) else None) or None,
        })

    cleaned_df = pd.concat([cleaned_df, cleaned_df["norm_addr"].apply(business_fields)], axis=1)

    if exclude_apns:
        before = len(cleaned_df)
        cleaned_df = cleaned_df[~cleaned_df["APN"].isin(exclude_apns)].reset_index(drop=True)
        print(f"[{city_name}] Manually excluded {before - len(cleaned_df)} confirmed non-commercial parcel(s)")

    before = len(cleaned_df)
    cleaned_df = cleaned_df[~cleaned_df["Owner"].apply(is_institutional_owner)].reset_index(drop=True)
    dropped = before - len(cleaned_df)
    if dropped:
        print(f"[{city_name}] Dropped {dropped} city/county/university/hospital-owned parcel(s) (not real acquisition targets)")

    before = len(cleaned_df)
    cleaned_df = cleaned_df[~cleaned_df["Business Category"].apply(is_built_out_complex)].reset_index(drop=True)
    dropped = before - len(cleaned_df)
    if dropped:
        print(f"[{city_name}] Dropped {dropped} already-built-out complex(es) (thriving mall/apartment complex — not a redevelopment target)")

    units_by_apn = CITY_UNITS_BY_APN.get(city_name)
    if units_by_apn:
        before = len(cleaned_df)
        units = cleaned_df["APN"].apply(lambda a: units_by_apn.get(normalize_apn_digits(a)))
        cleaned_df = cleaned_df[~(units.fillna(0) >= UNITS_EXCLUDE_THRESHOLD)].reset_index(drop=True)
        dropped = before - len(cleaned_df)
        if dropped:
            print(f"[{city_name}] Dropped {dropped} parcel(s) with {UNITS_EXCLUDE_THRESHOLD}+ existing units (already built out)")

    cleaned_df = merge_duplicate_addresses(cleaned_df, city_name)

    if zoning_filter is not None:
        before = len(cleaned_df)
        cleaned_df = cleaned_df[~cleaned_df["Zoning"].apply(zoning_filter)].reset_index(drop=True)
        print(f"[{city_name}] Zoning filter dropped {before - len(cleaned_df)} rows "
              f"(single-family/public-facility/open-space; multifamily and industrial kept)")

    cleaned_path = f"{ROOT}\\{safe_name}_commercial_properties_cleaned.xlsx"
    cleaned_df.to_excel(cleaned_path, index=False)
    matched_biz = cleaned_df["Business Name"].notna().sum()
    print(f"[{city_name}] Wrote {cleaned_path} ({len(cleaned_df)} rows) — "
          f"{matched_biz} Google Maps business matches")
    return cleaned_df


if __name__ == "__main__":
    # Only Palo Alto's zoning vocabulary has been researched/verified so far
    # (see the "Compare and contrast" + drop-list conversation) — other
    # cities use different zoning code systems that haven't been classified
    # this way yet, so this filter is deliberately Palo-Alto-only for now.
    ZONING_FILTERS = {"Palo Alto": palo_alto_drop_zoning}
    EXCLUDE_APNS = {"Palo Alto": PALO_ALTO_EXCLUDE_APNS}

    all_cleaned = []
    for spec in CITIES:
        df = build_city_master(
            spec["name"], spec["pr"], spec["lv"], spec["ps"], spec["gm"],
            zoning_filter=ZONING_FILTERS.get(spec["name"]),
            exclude_apns=EXCLUDE_APNS.get(spec["name"]),
        )
        all_cleaned.append(df)

    combined = pd.concat(all_cleaned, ignore_index=True)
    combined_path = f"{ROOT}\\combined_commercial_properties_cleaned.xlsx"
    combined.to_excel(combined_path, index=False)
    print()
    print(f"Combined master: {combined_path} ({len(combined)} rows)")
    print(combined["City"].value_counts())
