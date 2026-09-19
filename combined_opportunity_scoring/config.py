"""
config.py (Combined: Atherton + Los Altos + Los Altos Hills)
--------------------------------------------------------------
Same scoring engine and adapted loader as atherton_opportunity_scoring/,
pointed at the combined multi-city master (built by build_regional_master.py
from PropertyRadar+LandVision+PropStream+Google Maps per city, then
concatenated). No zoning-density reference data for any of these three
cities yet (Santa Clara is the only one researched so far), so
ZONE_TOWNHOME_FIT stays empty and that one Buildability sub-signal is
skipped gracefully, same missing-data handling as every other optional
signal.
"""

# ── Input files ──────────────────────────────────────────────────────
PROPERTIES_FILE = r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\combined_commercial_properties_cleaned.xlsx"
BUSINESS_FILES = []  # not used — business data is already joined per-row
ZONING_GEOJSON_FILE = r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\combined_opportunity_scoring\__no_zoning_file__.geojson"

LOGO_FAVICON_FILE = r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\combined_opportunity_scoring\logo_favicon.png"
LOGO_HEADER_FILE = r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\combined_opportunity_scoring\logo_header.png"

# Sourced from the City of Los Altos's own Housing Element site inventory
# ("Los Altos Opportunity Site - City based.pdf", Tables B-7A/B-7B) — these
# are the zones the city itself is actively targeting for higher-density
# rezoning. CT/CN/CD/CD-R3/CRS already carry no maximum density per the
# General Plan; OA is a real rezoning target but not yet zoned residential,
# so it gets a smaller, more speculative bonus. Only Los Altos has real
# reference data for this signal so far — every other city's zoning codes
# stay unmatched (None) and this sub-signal is skipped gracefully for them,
# same as any other missing-data case.
ZONE_TOWNHOME_FIT = {
    # Los Altos — matched against the real ZoningDistrict GIS layer (see
    # CITY_ZONING_GEOJSON / zoning_gp_lookup.csv) rather than the raw
    # LandVision "Zoning" string. R3-1 (38 du/ac, the city's highest-density
    # multi-family zone per the General Plan) and R3-1.8 (24.2 du/ac) added
    # now that the real zoning layer surfaces them; CT/CN/CD/CD-R3/CRS/OA
    # carry over from the earlier Los Altos Housing Element research.
    "CT": 25,
    "R3-1": 25,
    "CN": 20,
    "CD": 20,
    "CD/R3": 20,
    "R3-1.8": 18,
    "CRS": 18,
    "CRS/OAD": 18,
    "OA": 12,

    # Palo Alto — matched against the real parcel-level ZONEGIS field
    # (Assessor's Parcels file). RM-20/30/40 point values from the same
    # General Plan density table read earlier this session (20/30/40 du/ac).
    "RM-40": 25,
    "RM-30": 20,
    "RM-20": 15,

    # Menlo Park (matched against a real zone_code from the city's own zoning
    # GIS layer — see CITY_ZONING_GEOJSON below — not the garbled raw
    # "Zoning" string). Point values from the General Plan's density table
    # (Menlo Park Zoning Development, read earlier this session): R-4 tops
    # out at 40 du/ac (highest), R-MU/C-2-B allow 20-30 du/ac mixed-use,
    # R-3-A (Garden Apartment) 15-30 du/ac, R-3 13.1 du/ac (up to 30 near El
    # Camino Real), SP-ECR-D is the El Camino Real specific-plan corridor
    # (a real redevelopment target across all 4 cities, still speculative
    # since it's a corridor overlay, not a density number). R-1/PF/office/
    # commercial-only/open-space codes are deliberately left unscored —
    # single-family or non-residential, not a townhome fit.
    "R4": 25, "R4S": 25, "R4S(AHO)": 25, "R4(X)": 25,
    "R-MU-B": 22, "R-MU": 22,
    "C2B": 20,
    "R3A": 18, "R3A(X)": 18,
    "SP-ECR-D": 18,
    "R3": 15, "R3(X)": 15, "R3C": 15,
    "RLU": 12, "RLU(X)": 12,
    "R2": 10, "R2(X)": 10,
}
ZONE_INFO = {}

# General Plan (future/aspirational) land-use designation fit — a DIFFERENT
# signal from ZONE_TOWNHOME_FIT above. Current zoning says what's buildable
# TODAY with no city approval needed; a favorable General Plan designation
# says the city has already committed to allowing more housing here
# eventually, even if the zoning code hasn't caught up yet (a rezoning is
# needed first). Scored separately in score_buildability() — see
# GP_ONLY_DISCOUNT for how a GP-only match (no favorable current zoning) is
# treated with more caution than a current-zoning match.
GP_TOWNHOME_FIT = {
    # Los Altos (General Plan Land Use Designation GIS layer, LANDUSEDESC field)
    "Medium Density Multi-Family (38 du/net acre)": 25,
    "Thoroughfare Commercial": 20,
    "Downtown Commercial": 18,
    "Low Density Multi-Family (15 du/net acre)": 15,
    "Neighborhood Commercial": 15,
    "Planned Community": 10,

    # Palo Alto (LandUse GIS layer, DESIGNATIO field)
    "Multi-Family Res": 25,
    "Multi-Family Res (w/Hotel Overlay)": 22,
    "Mixed Use": 22,
    "Regional/Community Commercial": 15,
    "Service Commercial": 15,

    # Menlo Park (extracted from the Housing Element PDF's Site Inventory
    # table — see menlo_park_gp_zoning_by_apn.csv; two truncation variants of
    # "Professional and Administrative" appear in the source table)
    "Bayfront Innovation Area": 20,
    "El Camino Real/Downtown": 20,
    "Medium Density Residential": 15,
    "Retail/Commercial": 15,
    "Professional and Administra": 10,
    "Professional and Administrat": 10,
}

# A GP-only match (favorable General Plan designation but no favorable
# current zoning) means a rezoning is still needed. Per user direction, this
# isn't discounted — full credit, same as a current-zoning match. The reason
# string still calls out that entitlement/rezoning would be required, for
# transparency, but the score itself isn't reduced for it.
GP_ONLY_DISCOUNT = 1.0

# Per-city zoning GIS overlays: city name -> (GeoJSON path, zone-designation
# field name in that file). Only cities with a real, verified zoning GIS
# layer are listed — build_report.py's join_zoning() assigns p["zone_code"]
# from a spatial (lat/long-in-polygon) lookup against this file, which is
# far more reliable than a city's raw "Zoning" column when that column
# turns out to be a non-municipal encoding (as Menlo Park's and Atherton's
# are — see the "improve Menlo Park zoning" investigation this session).
# Cities not listed here simply get no zone_code (None), same graceful-skip
# behavior as any other missing signal.
CITY_ZONING_GEOJSON = {
    "Menlo Park": (
        r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\menlo_park_zoning.geojson",
        "Zoning",
    ),
}

# Precomputed current-zoning + General Plan designation per property for Los
# Altos, Palo Alto, and Menlo Park (Atherton skipped — no usable source
# found), built by build_zoning_gp_lookup.py from each city's real GIS/PDF
# data. Keyed by normalized APN; loaded once in build_report.py and used to
# set p["current_zoning_lookup"] / p["gp_designation"] for the two-signal
# Buildability zone-fit logic (ZONE_TOWNHOME_FIT vs GP_TOWNHOME_FIT above).
ZONING_GP_LOOKUP_FILE = r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\zoning_gp_lookup.csv"

# Lat/lng backfilled for properties missing coordinates in the source data
# (captured from the Street View metadata endpoint's free geocoding
# side-effect — see geocode_los_altos.py). Keyed by normalized APN; used as
# a fallback so the zoning/GP spatial join can still run for these rows.
COORD_BACKFILL_FILE = r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\la_geocoded_coords.csv"

# ── Business-type signal (same list as Santa Clara) ──────────────────
DECLINING_BUSINESS_KEYWORDS = [
    "appliance repair", "appliance store", "video rental", "video store",
    "print shop", "printing", "copy shop", "copying", "fax service",
    "film", "photo processing", "one hour photo", "photo developing",
    "dry cleaner", "dry cleaning", "travel agency", "pawn shop", "pawnbroker",
    "check cashing", "currency exchange", "tax preparation", "locksmith",
    "tanning salon", "video game rental", "video game store", "record store",
    "cd store", "book store", "bookstore", "furniture store", "carpet store",
    "flooring store", "electronics repair", "shoe repair", "watch repair",
    "sewing", "vacuum cleaner store", "fabric store", "office supply store",
    "greeting card shop", "party supply store",
    "self service laundry", "laundromat", "vacant", "closed",
    "camera repair", "camera store", "newsstand", "magazine store",
    "stationery store", "typewriter repair", "tv repair", "clock repair",
    "vacuum repair", "upholstery shop", "car stereo", "bank branch",
    "smog check station", "movie theater", "buffet restaurant",
    "mattress store",
]
DECLINING_EXCLUDE_KEYWORDS = []

REVIEW_COUNT_LOW = 10
REVIEW_COUNT_HIGH = 100
RATING_LOW = 3.5
RATING_HIGH = 4.3

FAR_LOW = 0.25
FAR_MODERATE = 0.50
LAND_VALUE_RATIO_HIGH = 0.75
LAND_VALUE_RATIO_MODERATE = 0.60
OLD_BUILDING_YEAR = 1970
AGING_BUILDING_YEAR = 1980

# Lot-size multiplier applied to the whole Buildability score (not just one
# line item) — a distressed/underbuilt property on too small a lot still
# can't support a townhome development, so size suppresses every other
# buildability signal rather than being scored independently. Control
# points (acres -> multiplier), linearly interpolated between them: full
# credit at 1 acre+, still relatively high near 0.75 acre, then the curve
# steepens below 0.5 acre. Set per user direction (townhome development
# feasibility floor around 1 acre).
LOT_SIZE_MULTIPLIER_POINTS = [
    (0.00, 0.00),
    (0.20, 0.10),
    (0.35, 0.35),
    (0.50, 0.70),
    (0.75, 0.88),
    (1.00, 1.00),
]

LAND_USE_HINT_KEYWORDS = [
    "used car dealer", "auto parts store", "auto repair shop", "tire shop",
    "muffler shop", "gas station", "car wash", "storage facility",
    "self storage", "warehouse", "parking lot", "parking garage",
    "parking structure",
]

# ── Street View Static API ──────────────────────────────────────────
STREET_VIEW_IMAGE_SIZE = "640x400"
STREET_VIEW_FOV = 80
STREET_VIEW_PITCH = 0

# ── Gemini vision ────────────────────────────────────────────────────
GEMINI_MODEL = "gemma-4-26b-a4b-it"
GEMINI_REQUEST_TIMEOUT = 45
GEMINI_MAX_RETRIES = 4
GEMINI_CONCURRENCY = 3

BUSINESS_MATCH_MAX_METERS = 60

# ── Scoring weights (same as Santa Clara) ────────────────────────────
WEIGHT_VISUAL = 0.25
WEIGHT_PROPERTY = 0.25
WEIGHT_BUILDABILITY = 0.35
WEIGHT_BUSINESS = 0.15

CONFIDENCE_FLOOR = 0.85
CONFIDENCE_RANGE = 0.15

# ── Output ────────────────────────────────────────────────────────────
OUTPUT_DIR = r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\combined_opportunity_scoring"
IMAGES_DIR = r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\combined_opportunity_scoring\images"
HTML_REPORT_FILE = "combined_opportunity_report.html"
CSV_REPORT_FILE = "combined_opportunity_scores.csv"

VISION_CACHE_FILE = r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\combined_opportunity_scoring\vision_cache.json"

# ── Known entitled/in-pipeline projects ──────────────────────────────
# Properties with a development already approved, under construction, or
# formally in the city's review pipeline -- compiled from each city's own
# project pages (Menlo Park approved/under-review/under-construction lists),
# CEQA filings, Housing Element records, and news coverage (SF YIMBY, Palo
# Alto Online, The Almanac), then cross-referenced into this dataset by
# APN/address (see entitled_pipeline_matches.csv for the full audit trail).
# None of the licensed sources (PropertyRadar/LandVision/PropStream) carry
# entitlement status, so this list is maintained manually -- every entry
# individually verified, never scraped/inferred. It does NOT drop rows --
# it drives the report's "Hide known entitled/pipeline projects" toggle
# and the "entitled" badge, since a hard exclusion would be irreversible.
# Keyed by normalized APN (digits only); multi-APN rows ("; "-joined in
# the master file) are matched per-APN by the split in build_report.py.
# Atherton verified to have no commercial pipeline (corridor study only).
ENTITLED_PIPELINE_APNS = {
    # ── Los Altos ──
    "17003085": "Aron Developers 85-unit 8-story — approved Nov 2025",   # 4898 El Camino Real
    "18915086": "Denardi Wang 12-unit mixed-use — CEQA cleared Nov 2025",  # 996 Loraine Ave
    # ── Menlo Park: approved ──
    "062423020": "68 Willow Rd density-bonus housing — approved",
    "055236120": "SP Menlo 105-unit 8-story — approved 2021",            # 111 Independence Dr
    "071413200": "201 ECR + 612 Cambridge mixed-use — approved",
    "071413370": "201 ECR + 612 Cambridge mixed-use — approved",
    "055170240": "March Capital 112-unit 8-story — approved",            # 3705 Haven Ave
    "055242090": "Menlo Flats 158 units — approved 2022",                # 165 Jefferson Dr
    "062390660": "Parkline (SRI campus redevelopment) — approved 2025",  # 333 Ravenswood Ave
    "062390670": "Parkline (SRI campus redevelopment) — approved 2025",
    "062390730": "Parkline (SRI campus redevelopment) — approved 2025",
    "062390760": "Parkline (SRI campus redevelopment) — approved 2025",
    "062390780": "Parkline (SRI campus redevelopment) — approved 2025",
    # Willow Village (Meta master plan, approved 2022; paused May 2026 but
    # still entitled) -- the ENTIRE former Menlo Science & Technology Park
    # assessor block 055-440-xxx per the project NOP's address ranges
    # (1350-1390 Willow Rd, 925-1098 Hamilton Ave, 1005-1275 Hamilton Ct):
    "055440010": "Willow Village (Meta master plan) — approved, paused",  # 1205 Hamilton Ct
    "055440020": "Willow Village (Meta master plan) — approved, paused",  # 1240 Hamilton Ct
    "055440030": "Willow Village (Meta master plan) — approved, paused",  # 1105 Hamilton Ct
    "055440040": "Willow Village (Meta master plan) — approved, paused",  # Hamilton Ct assemblage
    "055440050": "Willow Village (Meta master plan) — approved, paused",
    "055440090": "Willow Village (Meta master plan) — approved, paused",  # 931 Hamilton Ave
    "055440110": "Willow Village (Meta master plan) — approved, paused",  # 1374 Willow Rd
    "055440190": "Willow Village (Meta master plan) — approved, paused",  # 923-927 Hamilton Ave
    "055440210": "Willow Village (Meta master plan) — approved, paused",  # 1370 Willow Rd
    "055440230": "Willow Village (Meta master plan) — approved, paused",  # 940-960 Hamilton Ct
    "055440260": "Willow Village (Meta master plan) — approved, paused",  # 980 Hamilton Ave
    "055440300": "Willow Village (Meta master plan) — approved, paused",  # 1380 Willow Rd
    "055440310": "Willow Village (Meta master plan) — approved, paused",
    "055440320": "Willow Village (Meta master plan) — approved, paused",
    "055440330": "Willow Village (Meta master plan) — approved, paused",  # 990-998 Hamilton Ave
    "055440340": "Willow Village (Meta master plan) — approved, paused",  # 1360 Willow Rd
    "055440350": "Willow Village (Meta master plan) — approved, paused",  # 1350 Willow Rd
    # ── Menlo Park: under review ──
    "062423040": "Willow Park 80 Willow Rd (builder's remedy) — under review",
    "055242100": "BRP 8-story multifamily — under review",               # 155 Jefferson Dr
    "062390700": "Former USGS campus (Presidio Bay) — under review",     # 345 Middlefield Rd
    "062421070": "Former USGS campus (Presidio Bay) — under review",
    "071091520": "6-story 15-unit + bank conversion — under review",     # 800 Oak Grove Ave
    "071333190": "7-story 41-unit — under review",                       # 888 El Camino Real
    "055421160": "Tarlton R&D (w/ 1005 O'Brien) — under review",         # 1320 Willow Rd
    "055243300": "Commonwealth Bldg 3 (Sobrato office) — under review",  # 162 Jefferson Dr
    "055243310": "Commonwealth Bldg 3 (Sobrato office) — under review",  # 164 Jefferson Dr
    "055433250": "CSBio Phase 3 — under review",                         # 1075 O'Brien Dr
    # ── Menlo Park: under construction ──
    "071433330": "Mixed-use condos — under construction",                # 115 El Camino Real
    "055236140": "Sobrato 432 units — under construction",               # 123 Independence Dr
    "055170350": "Hotel Moxy 163 rooms — under construction",            # 3723 Haven Ave
    "055241050": "Lume/Menlo Uptown 483 units — under construction",     # 181 Constitution Dr
    "071332130": "Middle Plaza (Stanford) — under construction",         # 515 El Camino Real #525
    "071411450": "Middle Plaza (Stanford) — under construction",         # 495 El Camino Real
    "071411170": "Middle Plaza (Stanford) — under construction",
    "071411180": "Middle Plaza (Stanford) — under construction",
    "071412240": "Middle Plaza (Stanford) — under construction",         # 301 El Camino Real
    "071411190": "Middle Plaza (Stanford) — under construction",         # 441 El Camino Real
    "071411200": "Middle Plaza (Stanford) — under construction",         # 425 El Camino Real
    "071411210": "Middle Plaza (Stanford) — under construction",         # 417 El Camino Real
    # ── Palo Alto ──
    "14220054": "Acclaim 386 units (fish-market site) — under construction Aug 2026",  # 3150 ECR
    "00802035": "Strada 145 townhomes (Baylands) — approved",            # 2100 Geng Rd
    "12428045": "Redco 390 units (Mollie Stone's) — architectural review",  # 156 California Ave
    "13708006": "Oxford 231 units (Creekside Inn) — under review",       # 3400 El Camino Real
    "13708072": "Oxford 231 units (Creekside Inn) — under review",
    "13708083": "Oxford 231 units (Creekside Inn) — under review",
    "14809010": "SummerHill 29 townhomes — approved, final map 2026",    # 4335 El Camino Real
    "14809011": "SummerHill 29 townhomes — approved, final map 2026",    # 4345 El Camino Real
    "14705102": "Acclaim/Globe 198 apartments — under review",           # 762 San Antonio Rd
    "12026037": "Minority TV Project 17 units — initial review",         # 135 University Ave
    "13701069": "SB79 filing: 70 units — filed Jul 2026",                # 555 College Ave
    "12025110": "SB79 filing: 24 units — filed Jul 2026",                # 127 Lytton Ave
    "12433008": "SB79 filing: Coronet Motel 76 units — filed Jul 2026",  # 2455 El Camino Real
    "12034002": "Ellis Partners 158 units (T&C) — pre-application",      # 44 Encina Ave
    "12033004": "Ellis Partners 158 units (T&C) — pre-application",      # 63 Encina Ave
    "12034006": "Ellis Partners 158 units (T&C) — pre-application",      # 70 Encina Ave
    "12034007": "Ellis Partners 158 units (T&C) — pre-application",
    "12033003": "Ellis Partners 158 units (T&C) — pre-application",      # 75 Encina Ave
    "12033002": "Ellis Partners 158 units (T&C) — pre-application",      # 81 Encina Ave
    "12033001": "Ellis Partners 158 units (T&C) — pre-application",      # 87 Encina Ave
}

# Fallback for dataset rows with no APN at all (matched by exact
# uppercased address + city instead). Same semantics as the APN dict.
ENTITLED_PIPELINE_ADDRESSES = {
    "MENLO PARK|155 JEFFERSON DR # 100": "BRP 8-story multifamily — under review",
    "MENLO PARK|515 EL CAMINO REAL # 100-160": "Middle Plaza (Stanford) — under construction",
    "MENLO PARK|931-1003 HAMILTON AVE": "Willow Village (Meta master plan) — approved, paused",
}

# Source citations for each distinct project name above (the text before
# " — " in the note strings). Feeds the report's editable "Pipeline project
# notes" panel so every flagged site shows where the information came from.
ENTITLED_PROJECT_SOURCES = {
    "Aron Developers 85-unit 8-story": "SF YIMBY (Nov 2025); Los Altos Planning Commission",
    "Denardi Wang 12-unit mixed-use": "CEQAnet NOE (Nov 2025); SF YIMBY (Jul 2025)",
    "68 Willow Rd density-bonus housing": "menlopark.gov — Approved projects",
    "SP Menlo 105-unit 8-story": "menlopark.gov — Approved projects (PC approval Apr 2021)",
    "201 ECR + 612 Cambridge mixed-use": "menlopark.gov — Approved projects",
    "March Capital 112-unit 8-story": "menlopark.gov — Approved projects",
    "Menlo Flats 158 units": "menlopark.gov — Approved projects; The Almanac (Mar 2022)",
    "Parkline (SRI campus redevelopment)": "menlopark.gov — Approved projects; Council approval fall 2025",
    "Willow Village (Meta master plan)": "menlopark.gov — Approved projects; The Almanac (May 2026 pause)",
    "Willow Park 80 Willow Rd (builder's remedy)": "menlopark.gov — Under review",
    "BRP 8-story multifamily": "menlopark.gov — Under review",
    "Former USGS campus (Presidio Bay)": "menlopark.gov — Under review",
    "6-story 15-unit + bank conversion": "menlopark.gov — Under review",
    "7-story 41-unit": "menlopark.gov — Under review",
    "Tarlton R&D (w/ 1005 O'Brien)": "menlopark.gov — Under review",
    "Commonwealth Bldg 3 (Sobrato office)": "menlopark.gov — Under review",
    "CSBio Phase 3": "menlopark.gov — Under review; CEQAnet NOP",
    "Mixed-use condos": "menlopark.gov — Under construction",
    "Sobrato 432 units": "menlopark.gov — Under construction (PC Aug 2023, Council Sep 2023)",
    "Hotel Moxy 163 rooms": "menlopark.gov — Under construction (3723 Haven Ave project page)",
    "Lume/Menlo Uptown 483 units": "menlopark.gov — Under construction",
    "Middle Plaza (Stanford)": "menlopark.gov — Under construction (300-550 El Camino Real)",
    "Acclaim 386 units (fish-market site)": "Palo Alto Online / RWC Pulse (Sep 2026 — broke ground Aug 2026)",
    "Strada 145 townhomes (Baylands)": "Palo Alto Online (Mar 2026 approval)",
    "Redco 390 units (Mollie Stone's)": "Palo Alto Online / RWC Pulse (Sep 2026)",
    "Oxford 231 units (Creekside Inn)": "Palo Alto Online (Sep 2024, builder's remedy)",
    "SummerHill 29 townhomes": "Palo Alto Online (Apr 2026 final map); CEQAnet",
    "Acclaim/Globe 198 apartments": "Palo Alto Online (Jul 2025 revision)",
    "Minority TV Project 17 units": "Palo Alto Online / RWC Pulse (Sep 2026)",
    "SB79 filing: 70 units": "Palo Alto Online SB 79 coverage (Jul 2026)",
    "SB79 filing: 24 units": "Palo Alto Online SB 79 coverage (Jul 2026)",
    "SB79 filing: Coronet Motel 76 units": "Palo Alto Online SB 79 coverage (Jul 2026)",
    "Ellis Partners 158 units (T&C)": "Palo Alto Online / RWC Pulse (Sep 2026, pre-application)",
}
