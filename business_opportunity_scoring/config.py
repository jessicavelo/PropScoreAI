"""
config.py
---------
Tunable constants for the business-property opportunity scorer.
Edit these to adjust what counts as a "declining" business or how heavily
each signal contributes to the final score.
"""

# ── Input files ──────────────────────────────────────────────────────
PROPERTIES_FILE = r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\santa_clara_commercial_properties_cleaned.xlsx"
BUSINESS_FILES = [
    r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\scraped_batch1.csv",
    r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\scraped_batch2.csv",
    r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\scraped_batch3.csv",
    r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\scraped_batch4.csv",
]
# City of Santa Clara zoning parcels (real geometry). Only properties with
# LATITUDE/LONGITUDE can be spatially joined to a zone; properties without
# coordinates get no zone_code (same missing-data handling as other signals).
ZONING_GEOJSON_FILE = r"C:\Users\jessi\Downloads\City_of_Santa_Clara_Zoning.geojson"

# PropScore AI logo (cropped/transparent PNGs, generated from the source
# artwork) — embedded as data URIs at render time so the report stays a
# single self-contained file.
LOGO_FAVICON_FILE = r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\business_opportunity_scoring\logo_favicon.png"
LOGO_HEADER_FILE = r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\business_opportunity_scoring\logo_header.png"

# Researched against Santa Clara's actual general plan density classifications
# and zoning ordinance (via search-indexed city ordinances, CEQA filings, and
# specific-plan documents — santaclaraca.gov and codepublishing.com both
# block direct automated fetches with a 403, so this wasn't read straight off
# the primary code text). Client goal is a HIGH-DENSITY TOWNHOME community,
# so scores weight both "is residential allowed" and "how much density" —
# a zone allowing only single-family scores low even though it's technically
# residential. Score out of 35 (the zoning-fit share of Buildability,
# additive alongside FAR/lot-size/land-value elsewhere in that layer).
#
# Confirmed figures (source: general plan classifications + specific-plan/
# ordinance documents):
#   Very Low Density Residential (R1 family): <=10 units/acre, single-family
#   Low Density Residential (R2 family): 8-19 units/acre — general plan text
#     explicitly names townhomes/rowhouses as a permitted building type here
#   Medium Density Residential (R3): 20-36 units/acre
#   High Density Residential (R4): 37-50 units/acre
#   MUCC (Community Mixed Use): 19-25 units/acre — confirmed via a real
#     built example (22 townhouse units on a 0.88-acre MUCC site)
#   MURC (Regional Mixed Use): 50-120 units/acre, transit-oriented
#   TN (Transit Neighborhood / Tasman East specific plan): 100-350 units/
#     acre, 60-100 minimum required — very high density, likely stacked
#     flats/podium apartments at that density rather than literal 2-3
#     story townhomes, but still a strong "high-density residential" site
#   DNTW (Downtown): ~40-53+ units/acre per the Downtown Precise Plan
# Inferred (naming convention + comparable zones in the same city, NOT
# independently confirmed — verify before treating as certain): MUCCHT,
# MUNC, CD, VHDM, VHDR (this one's general-plan link oddly points to an
# industrial classification despite its "Very High Density Residential"
# name — worth a manual check), DTHT, PD-MC, and the Patrick Henry
# Drive/Lawrence Station specific-plan zones (HDF, LSAP, PH-R5, UC, UV, VR).
ZONE_TOWNHOME_FIT = {
    # Confirmed high/very-high density residential or mixed-use
    "R4": 35, "TN": 35, "MURC": 35, "DNTW": 33, "MUCC": 32,
    "R3": 30, "MUCCHT": 30, "DTHT": 30,
    # Inferred high density (specific-plan/transit-oriented pattern in this
    # city trends 50-100+ units/acre; not independently confirmed per-zone)
    "VHDM": 32, "VHDR": 25, "LSAP": 25, "CD": 25,
    # Low Density Residential — confirmed to allow townhomes/rowhouses, but
    # at meaningfully lower density (8-19/acre) than the zones above
    "R2": 18, "R2-7L": 18, "MUNC": 18, "PD-MC": 22,
    "R2HT": 15, "HDF": 22, "PH-R5": 20, "UC": 18, "UV": 18, "VR": 18,
    # Commercial-only or Planned-Development zones — residential possible
    # only via a specific approved PD plan or a rezone, not by-right
    "CT": 15, "PD": 15, "CP(PD)": 18, "CC": 12, "CN": 12, "CR": 12, "CP": 10,
    # Already residential-zoned but single-family only (<=10 units/acre) —
    # real zoning, just the wrong density for a townhome project as-is
    "R1-6L": 5, "R1-6LHT": 5, "R1-8L": 5,
    # Not residential-capable (industrial, office/R&D, open space, public)
    "HI": 0, "LI": 0, "ML": 0, "MP": 0, "HO-RD": 0, "LO-RD": 0,
    "B": 0, "OA": 0, "OS": 0, "PQP": 0, "MH": 0,
}

# Human-readable explanation shown in the report for each zone code — same
# research/confidence basis as ZONE_TOWNHOME_FIT above.
ZONE_INFO = {
    "R1-8L": "Very Low Density Residential — single-family only, up to ~10 units/acre. Would need a rezone for a townhome project.",
    "R1-6L": "Very Low Density Residential — single-family only, up to ~10 units/acre. Would need a rezone for a townhome project.",
    "R1-6LHT": "Very Low Density Residential (historic overlay) — single-family only, up to ~10 units/acre.",
    "R2": "Low Density Residential — 8-19 units/acre; general plan explicitly names townhomes/rowhouses as a permitted building type here.",
    "R2-7L": "Low Density Residential — 8-19 units/acre; townhomes/rowhouses explicitly allowed.",
    "R2HT": "Low Density Residential (historic overlay) — 8-19 units/acre.",
    "R3": "Medium Density Residential — 20-36 units/acre. Multi-family/townhomes permitted by right.",
    "R4": "High Density Residential — 37-50 units/acre. Strong fit for a high-density townhome/multi-family project.",
    "VHDM": "Very High Density Mixed combining district — inferred very high density (50+ units/acre) from comparable Santa Clara districts; not independently confirmed, verify with the city.",
    "VHDR": "Named Very High Density Residential, but its general-plan link points to an industrial/R&D classification — a data inconsistency worth verifying with the city directly before relying on it.",
    "TN": "Transit Neighborhood (Tasman East Specific Plan) — 100-350 units/acre, 60-100 minimum required. Extremely high density; likely stacked flats/podium apartments at max density rather than 2-3 story townhomes, but strongly residential-capable.",
    "MUCC": "Community Mixed Use — 19-25 units/acre. Confirmed: real townhome projects have been built under this designation (e.g. 22 units on a 0.88-acre site).",
    "MUCCHT": "Community Mixed Use (historic overlay) — same profile as MUCC, 19-25 units/acre.",
    "MUNC": "Neighborhood Mixed Use — allows residential above/alongside neighborhood-scale commercial, likely lower density than the Community/Regional Mixed Use tiers.",
    "MURC": "Regional Mixed Use — 50-120 units/acre, transit-oriented. Very strong fit for high-density residential.",
    "DNTW": "Downtown — roughly 40-53+ units/acre per the Downtown Precise Plan. High-density mixed-use residential is expected here.",
    "DTHT": "Downtown (historic overlay) — similar density profile to Downtown, with historic-preservation design constraints.",
    "CD": "Station Area classification — likely allows higher-density residential near transit; exact figures not directly confirmed.",
    "CC": "Community Commercial — commercial only by base zoning; residential needs a Planned Development approval or rezone.",
    "CN": "Neighborhood Commercial — commercial only by base zoning; residential needs a Planned Development approval or rezone.",
    "CR": "Commercial Regional — commercial only by base zoning; residential needs a Planned Development approval or rezone.",
    "CT": "Commercial Thoroughfare — primarily commercial; residential possible only via a specific approved plan.",
    "CP": "Commercial Planned Development — residential only if the specific approved plan for that parcel includes it.",
    "CP(PD)": "Commercial Planned Development combining district — residential possible if the approved plan allows it.",
    "PD": "Planned Development — allowed uses depend entirely on the specific approved plan for that parcel; verify the underlying plan before assuming residential is allowed.",
    "PD-MC": "Planned Development, Mixed Use/Commercial — more likely than a plain PD to include a residential component, but still plan-specific.",
    "HO-RD": "Heavy Industrial/Office/R&D combining district — not residential-capable without a rezone.",
    "MP": "Heavy Industrial/Office/R&D combining district — not residential-capable without a rezone.",
    "LI": "Light Industrial — not residential-capable without a rezone.",
    "LO-RD": "Light Industrial/Office/R&D combining district — not residential-capable without a rezone.",
    "ML": "Light Industrial — not residential-capable without a rezone.",
    "HI": "Heavy Industrial — not residential-capable without a rezone.",
    "MH": "Industrial-adjacent classification — not residential-capable without a rezone.",
    "B": "Regional Commercial-adjacent classification — not residential-capable without a rezone.",
    "OA": "Regional Commercial-adjacent classification — not residential-capable without a rezone.",
    "OS": "Parks/Open Space — not developable for housing.",
    "PQP": "Public/Quasi-Public — institutional use only, not residential-capable.",
    "HDF": "Patrick Henry Drive Specific Plan area — likely high density based on comparable Santa Clara transit-oriented specific plans; exact figures not directly confirmed.",
    "LSAP": "Lawrence Station Area Specific Plan — transit-oriented, likely high density similar to Tasman East; exact figures not directly confirmed.",
    "PH-R5": "Patrick Henry Drive Specific Plan, residential sub-area — likely allows attached/multi-family housing; exact figures not directly confirmed.",
    "UC": "Patrick Henry Drive Specific Plan, urban-core sub-area — likely mixed-use/residential; exact figures not directly confirmed.",
    "UV": "Patrick Henry Drive Specific Plan sub-area — likely mixed-use/residential; exact figures not directly confirmed.",
    "VR": "Patrick Henry Drive Specific Plan sub-area — likely mixed-use/residential; exact figures not directly confirmed.",
}

# ── Business-type signal ────────────────────────────────────────────
# Case-insensitive substring match against the business's Google category
# name and title. A hit marks the property as a "declining business"
# opportunity (old-economy retail/service categories that tend to close
# and free up the site for redevelopment or a new tenant).
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

# Auto-related and self-storage/warehouse categories were deliberately
# removed from the list above. They aren't genuinely "declining businesses"
# in the way video rental or film developing are — they were on the list
# because that *type of site* (single-story, large lot, low building
# coverage) is a classic redevelopment target, which is a land-use signal,
# not a business-health signal (e.g. AutoZone is a thriving national chain,
# not a declining business, and shouldn't be flagged as one just because
# its category matched). See LAND_USE_HINT_KEYWORDS below — that's where
# they now live, feeding the Buildability layer instead.

# Categories that should NOT be treated as declining even if they contain a
# keyword above as a substring (avoids false positives, e.g. "print shop"
# inside "custom print shop for apparel"). Leave empty unless you notice
# a specific false positive during review.
DECLINING_EXCLUDE_KEYWORDS = []

# ── Review-based signal (Google reviewsCount / totalScore) ──────────
# A business with very few reviews and a low rating is a stronger
# "struggling" signal than category alone; a busy, well-reviewed business
# is a weaker opportunity even if its category happens to match a
# declining keyword (e.g. a thriving 4.8-star, 500-review auto repair shop
# isn't actually a good target just because "auto repair" is on the list).
REVIEW_COUNT_LOW = 10       # at or below this many reviews counts as "few"
REVIEW_COUNT_HIGH = 100     # at or above this many reviews counts as "many"
RATING_LOW = 3.5            # at or below this rating counts as "low"
RATING_HIGH = 4.3           # at or above this rating counts as "high"

# ── Buildability signal (physical redevelopment upside) ─────────────
# Is the LAND underused relative to what's built on it? Separate from
# whether the current tenant business is healthy or struggling.
FAR_LOW = 0.15          # building/lot ratio at or below this = strongly underbuilt
FAR_MODERATE = 0.30     # at or below this = moderately underbuilt
LAND_VALUE_RATIO_HIGH = 0.75    # land value / total assessed value at/above this = value is mostly in the dirt
LAND_VALUE_RATIO_MODERATE = 0.60
OLD_BUILDING_YEAR = 1970       # built at/before this = old improvement, weak secondary signal
AGING_BUILDING_YEAR = 1990

# Land-use categories that are classic underbuilt/redevelopment-target site
# types (single-story, large lot, low building coverage) — used ONLY as a
# fallback Buildability hint when Building Sqft is missing and FAR can't be
# computed directly. Moved out of the business-value list (see note above)
# since these describe the land use, not whether the business is declining.
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
# Pinned to an explicit (not "-latest") model so it draws from its own
# separate free-tier daily quota bucket. Tried and exhausted so far today:
# gemini-3.5-flash-lite (via "-latest"), gemini-3.1-flash-lite (and its
# "-preview" twin, which shares the same quota). gemini-2.5-flash-lite and
# the 2.0 line are fully retired for this account (404). gemini-3-flash-
# preview is the next untouched option — it's a "thinking" model so calls
# run slower (~3-30s each vs ~1-2s for the lite models), but it works,
# including under concurrency.
GEMINI_MODEL = "gemma-4-26b-a4b-it"
GEMINI_REQUEST_TIMEOUT = 45
GEMINI_MAX_RETRIES = 4
# Cap how many vision calls run at once regardless of --workers (which also
# covers Street View fetch, a much higher-quota API). Lower than usual
# since gemini-3-flash-preview is a slower "thinking" model — the earlier
# thinking model (gemini-flash-latest) hit tight RPM limits under load.
GEMINI_CONCURRENCY = 3

# ── Business/property address join ──────────────────────────────────
# Max distance (meters) allowed for a lat/long fallback match when the
# normalized street address doesn't match exactly.
BUSINESS_MATCH_MAX_METERS = 60

# ── Scoring weights ──────────────────────────────────────────────────
# Four layers now: Visual condition > Property info > Buildability >
# Business value, in that priority order per the user's call. Must sum to
# 1.0. Applied to whichever layers are available for a given property;
# unavailable layers are dropped and the remaining weights are
# renormalized (see combine_scores in build_report.py).
WEIGHT_VISUAL = 0.35
WEIGHT_PROPERTY = 0.30
WEIGHT_BUILDABILITY = 0.20
WEIGHT_BUSINESS = 0.15

# Confidence multiplier range, same shape as the residential scorer this
# project reuses the methodology from (palm_property_intelligence).
CONFIDENCE_FLOOR = 0.70
CONFIDENCE_RANGE = 0.30

# ── Output ────────────────────────────────────────────────────────────
OUTPUT_DIR = r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\business_opportunity_scoring"
IMAGES_DIR = r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\business_opportunity_scoring\images"
HTML_REPORT_FILE = "santa_clara_opportunity_report.html"
CSV_REPORT_FILE = "santa_clara_opportunity_scores.csv"

# Successful Gemini vision reads are cached here, keyed by property id, so
# reruns (e.g. after hitting a daily free-tier quota) only need to backfill
# whatever is still missing instead of re-spending quota on properties that
# already got a read.
VISION_CACHE_FILE = r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\business_opportunity_scoring\vision_cache.json"
