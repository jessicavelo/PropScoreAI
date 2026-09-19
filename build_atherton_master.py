"""
build_atherton_master.py
-------------------------
Merges the 4 Atherton source datasets (PropertyRadar, LandVision, PropStream,
Google Maps business scrape) into one master property file.

Join strategy: APN first (LandVision + PropStream share a consistent
"XXX-XXX-XXX" format), falling back to normalized street address for
PropertyRadar (no APN column) and the Google Maps business join. Atherton
has almost no commercial zoning, so this is a small, high-precision merge
(under 20 unique properties), not a bulk fuzzy-match job like Santa Clara's.

Outputs:
    atherton_merged_properties.xlsx      one row per property, source-prefixed
                                          columns (PR_*/LV_*/PS_*) + in_X flags
    atherton_commercial_properties_cleaned.xlsx
                                          flattened single-value-per-field
                                          master + joined Google Maps business
"""
import re
import pandas as pd

ROOT = r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring"


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


# ── Load sources ──────────────────────────────────────────────────────

pr = pd.read_csv(f"{ROOT}\\Atherton Property Radar Dataset.csv", low_memory=False)
pr = pr[pr["Address"].notna()].copy()
pr["norm_addr"] = pr["Address"].apply(normalize_address)
pr["apn"] = ""  # PropertyRadar export has no APN column

lv = pd.read_csv(f"{ROOT}\\Atherton LandVision Dataset.csv")
lv = lv[lv["SITE_ADDR"].notna()].copy()
lv["norm_addr"] = lv["SITE_ADDR"].apply(normalize_address)
lv["apn"] = lv["APN"].apply(clean_apn)

ps = pd.read_csv(f"{ROOT}\\Property Export Atherton+Commercial PropStream Dataset.csv", low_memory=False)
ps = ps[ps["Address"].notna()].copy()
ps["norm_addr"] = ps["Address"].apply(normalize_address)
ps["apn"] = ps["APN"].apply(clean_apn)

gm = pd.read_csv(f"{ROOT}\\Atherton Google Map Business Dataset.csv", low_memory=False)
gm = gm[gm["address"].notna()].copy()
gm["norm_addr"] = gm["street"].apply(normalize_address)

print(f"Loaded: PropertyRadar={len(pr)}, LandVision={len(lv)}, PropStream={len(ps)}, GoogleMap={len(gm)}")

# ── Build unified key per property: prefer APN, fall back to norm_addr ──

def key_for(row):
    return f"APN:{row['apn']}" if row["apn"] else f"ADDR:{row['norm_addr']}"

pr["key"] = pr.apply(key_for, axis=1)
lv["key"] = lv.apply(key_for, axis=1)
ps["key"] = ps.apply(key_for, axis=1)

# LandVision/PropStream APNs are the anchor; if an address-keyed PropertyRadar
# row's norm_addr matches an APN-keyed LandVision/PropStream row, re-key it
# to that APN so they merge into one row instead of staying separate.
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
print(f"Unique properties after APN/address reconciliation: {len(all_keys)}")

# ── Merge into one row per property, source-prefixed columns ────────────

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
            if c not in ("key",):
                row[f"PR_{c}"] = pr_match.iloc[0][c]
    if len(lv_match):
        for c in lv.columns:
            if c not in ("key",):
                row[f"LV_{c}"] = lv_match.iloc[0][c]
    if len(ps_match):
        for c in ps.columns:
            if c not in ("key",):
                row[f"PS_{c}"] = ps_match.iloc[0][c]

    merged_rows.append(row)

merged_df = pd.DataFrame(merged_rows)
merged_path = f"{ROOT}\\atherton_merged_properties.xlsx"
merged_df.to_excel(merged_path, index=False)
print(f"Wrote {merged_path} ({len(merged_df)} rows, {len(merged_df.columns)} cols)")

# ── Flattened / cleaned master: one value per logical field ─────────────

def pick(row, *candidates):
    for c in candidates:
        v = row.get(c)
        if v is not None and str(v).strip() and str(v).strip().lower() != "nan":
            return v
    return None


def pick_bool(row, *candidates):
    """Same as pick(), but normalizes mixed source formats (PropStream's
    "Yes"/"No" text vs PropertyRadar's 1.0/0.0 floats) to one consistent
    "Yes"/"No" instead of leaking whichever raw format happened to win."""
    v = pick(row, *candidates)
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("1", "1.0", "yes", "y", "true"):
        return "Yes"
    if s in ("0", "0.0", "no", "n", "false"):
        return "No"
    return v

cleaned_rows = []
for _, row in merged_df.iterrows():
    address = pick(row, "LV_SITE_ADDR", "PS_Address", "PR_Address")
    apn = pick(row, "LV_APN", "PS_APN")
    c = {
        "Address": address,
        "City": pick(row, "LV_SITE_CITY", "PS_City", "PR_City") or "Atherton",
        "Zip": pick(row, "LV_SITE_ZIP", "PS_Zip", "PR_ZIP"),
        "APN": apn,
        "Owner": pick(row, "PS_Owner 1 Last Name", "LV_OWNER_NAME_1", "PR_Owner"),
        # Real municipal zoning code (e.g. "C10000") — kept separate from
        # PropStream's generic land-use category below, which is NOT a
        # zoning code and would be misleading if conflated with one.
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

# Join Google Maps business data by normalized street address
gm_by_addr = {}
for _, r in gm.iterrows():
    gm_by_addr.setdefault(r["norm_addr"], []).append(r)

def business_fields(norm_addr):
    matches = gm_by_addr.get(norm_addr, [])
    if not matches:
        return pd.Series({"Business Name": None, "Business Category": None,
                           "Business City": None, "Business Reviews": None,
                           "Business Rating": None, "Business Closed": None})
    m = matches[0]
    return pd.Series({
        "Business Name": m["title"], "Business Category": m["categoryName"],
        "Business City": m["city"], "Business Reviews": m["reviewsCount"],
        "Business Rating": m["totalScore"],
        "Business Closed": bool(m["permanentlyClosed"]) or bool(m["temporarilyClosed"]),
    })

cleaned_df = pd.concat([cleaned_df, cleaned_df["norm_addr"].apply(business_fields)], axis=1)

cleaned_path = f"{ROOT}\\atherton_commercial_properties_cleaned.xlsx"
cleaned_df.to_excel(cleaned_path, index=False)
print(f"Wrote {cleaned_path} ({len(cleaned_df)} rows, {len(cleaned_df.columns)} cols)")

matched_biz = cleaned_df["Business Name"].notna().sum()
print(f"Google Maps business matches: {matched_biz}/{len(cleaned_df)}")
print()
print(cleaned_df[["Address", "APN", "Owner", "Zoning", "source_count", "Business Name"]].to_string())
