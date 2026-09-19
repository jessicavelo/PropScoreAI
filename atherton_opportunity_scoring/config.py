"""
config.py (Atherton)
---------------------
Same scoring engine as business_opportunity_scoring/ (Santa Clara), pointed
at the merged Atherton dataset instead. Two real differences from Santa
Clara's config:

1. PROPERTIES_FILE is the already-merged, already-business-joined master
   (atherton_commercial_properties_cleaned.xlsx, built by
   build_atherton_master.py) — so BUSINESS_FILES isn't used here, business
   data is read directly per-row in load_properties().
2. ZONE_TOWNHOME_FIT / ZONE_INFO are empty — Atherton's LandVision zoning
   codes (e.g. "C10000", "CH00C2") are a different vocabulary than Santa
   Clara's, and there's no real density-classification research for them
   yet. zone_code is never set for Atherton properties, so this just means
   the Buildability layer skips that one sub-signal gracefully (same
   missing-data handling as every other optional signal) rather than
   guessing. Worth noting separately: Atherton is a famously single-family
   estate town that has historically resisted multi-family development —
   even with real zoning data, it's unlikely to be a strong townhome-site
   market compared to Santa Clara.
"""

# ── Input files ──────────────────────────────────────────────────────
PROPERTIES_FILE = r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\atherton_commercial_properties_cleaned.xlsx"
BUSINESS_FILES = []  # not used — business data is already joined per-row
ZONING_GEOJSON_FILE = r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\atherton_opportunity_scoring\__no_zoning_file__.geojson"

LOGO_FAVICON_FILE = r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\atherton_opportunity_scoring\logo_favicon.png"
LOGO_HEADER_FILE = r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\atherton_opportunity_scoring\logo_header.png"

ZONE_TOWNHOME_FIT = {}
ZONE_INFO = {}

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

FAR_LOW = 0.15
FAR_MODERATE = 0.30
LAND_VALUE_RATIO_HIGH = 0.75
LAND_VALUE_RATIO_MODERATE = 0.60
OLD_BUILDING_YEAR = 1970
AGING_BUILDING_YEAR = 1990

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
# gemma-4-26b-a4b-it worked well for the full Santa Clara backfill (906/906
# eventually completed on it) — same account/quota pool, reused here.
GEMINI_MODEL = "gemma-4-26b-a4b-it"
GEMINI_REQUEST_TIMEOUT = 45
GEMINI_MAX_RETRIES = 4
GEMINI_CONCURRENCY = 3

BUSINESS_MATCH_MAX_METERS = 60

# ── Scoring weights (same as Santa Clara) ────────────────────────────
WEIGHT_VISUAL = 0.35
WEIGHT_PROPERTY = 0.30
WEIGHT_BUILDABILITY = 0.20
WEIGHT_BUSINESS = 0.15

CONFIDENCE_FLOOR = 0.70
CONFIDENCE_RANGE = 0.30

# ── Output ────────────────────────────────────────────────────────────
OUTPUT_DIR = r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\atherton_opportunity_scoring"
IMAGES_DIR = r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\atherton_opportunity_scoring\images"
HTML_REPORT_FILE = "atherton_opportunity_report.html"
CSV_REPORT_FILE = "atherton_opportunity_scores.csv"

VISION_CACHE_FILE = r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\atherton_opportunity_scoring\vision_cache.json"
