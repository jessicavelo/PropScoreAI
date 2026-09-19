# Business Value — data improvement backlog

Notes captured 2026-09-01. Nothing here is implemented; this is a parked idea list.

## Measured baseline (don't re-derive these — they were tested against real data)

| Fact | Value |
|---|---|
| Properties with any business matched | 280 / 2,207 (12.7%) |
| Businesses scraped across all 4 cities | 983 → only 280 matched (**28%**) |
| Business Value weight in overall score | 15% |
| Business labels | 1,927 unknown / 224 neutral / 56 declining |

Per-city Google Maps scrape → match counts: Atherton 10→3, Los Altos 184→62,
Menlo Park 294→71, Palo Alto 495→144.

## Finding 1 — matching is the bottleneck, not scraping (highest ROI)

`build_regional_master.py` `business_fields()` (~line 454) matches businesses to
properties by **exact normalized address only**. There is no lat/lng proximity
fallback, even though both sides carry coordinates.

Tested on the 84-restaurant Los Altos file:
- 30 matched by exact address (current logic)
- **32 more sit within 60m of a dataset property and are discarded**
- 22 genuinely unmatched (mostly other cities — Mountain View, Cupertino)

Real losses being thrown away:

| Business address | Actual property | Distance |
|---|---|---|
| 987 Fremont Ave | 981 FREMONT AVE | 20m |
| 170 State St | 170 STATE ST STE 100 | 24m |
| 266 Main St | 262 MAIN ST | 12m |
| 4546 El Camino Real | 4540 EL CAMINO REAL | 33m |

Cause: suite suffixes and off-by-a-few street numbers. Fixing this roughly doubles
the yield of every scrape already paid for, across all four cities.

**Caution:** 60m can mis-assign on dense downtown corners (e.g. "295 Main St" →
"255 2ND ST" at 14m). Prefer street-name agreement first, distance as fallback.

## Finding 2 — adding restaurants alone is modest

Of 84 Los Altos restaurants: 17 land on properties that have no business today.
Los Altos coverage would go 11.3% → 14.4%. Worth doing, but it is the smaller half
of the win compared to the matching fix.

Note most restaurants are *thriving* (median 4.3★, 282 reviews), so they mostly add
"neutral" scores. That is valuable **negative** information — this site is not a soft
target — rather than a source of new opportunities.

## Finding 3 — fields already in the scrape, currently unused

Only ~5 of ~35 usable fields are read today:

- `temporarilyClosed` — 3 of 84 flagged; direct distress
- `claimThisBusiness` — unclaimed listing = owner never engaged
- `imagesCount` — median 340; under ~20 means invisible/dying
- `price` — a `$` tenant on prime land = underutilized rent
- `website` / `description` / `reserveTableUrl` — all absent = marginal operator
- `openingHours` — open 3 days/week = winding down
- `reviewsDistribution` — 1–5 star breakdown (partially populated in current scrape)

## Best *new* signal: review recency, not review count

Current scoring uses lifetime review count, which never decreases. A restaurant with
900 reviews whose last one was 11 months ago is dying but currently scores as thriving.
Scraping individual reviews with dates gives **review velocity trend** (reviews/month
over ~3 years) — a leading indicator instead of a lagging one.

Runner-up: **popularTimes** busyness histogram. The columns exist in the scrape
(`popularTimesLivePercent`, `popularTimesLiveText`) but came back empty. Real foot
traffic would replace the single-Street-View-snapshot parking heuristic.

## Tier 1 — free, high value

1. Scrape **all** business categories, not just restaurants (restaurants ≈10% of tenants)
2. **Owner portfolio clustering** — owner names are already in the data; a one-property
   owner sells, a 40-property institution does not
3. **"TRUST" / "FAMILY TRUST" in owner name** — regex on existing data; signals estate
   planning and a coming succession event
4. **CA Secretary of State entity status** — property owned by a suspended/forfeited LLC
   is genuine distress; free lookup
5. **Transit distance (SB 79)** — computable from existing lat/lng + GTFS; materially
   changes what is legally buildable

## Tier 2 — public records, real work, high value

6. **LoopNet / Crexi listings** — "For Lease" = vacancy, "For Sale" = owner already
   motivated. The most direct signal that exists for the actual question.
7. **GeoTracker (CA Water Board) contamination** — ⚠️ correctness fix, not an enhancement.
   Gas stations and dry cleaners carry leaking-tank contamination that destroys
   redevelopment economics, and those property types currently rank highly.
8. **Historic designation status** — ⚠️ another false-positive source. Historic buildings
   are near-impossible to redevelop; Los Altos has an active Historic Resource process.
9. **Business license registries** — lapsed license = closed tenant; license count per
   parcel over time = tenant turnover rate = frustrated-landlord signal
10. **County tax delinquency + recorder liens** (mechanics/tax liens, notices of default)
11. **Code enforcement + permit history** — open violations = neglect; zero permits in
    30 years = no reinvestment; large recent permit = owner just invested, won't sell
12. **CA ABC liquor licenses** — surrendered license precedes closure; pending transfer
    means a sale is already in progress
13. **County health inspection scores** — declining scores predict closure
14. **Flood zone / Alquist-Priolo fault zones** — affects the Menlo Park bayfront cluster

## Tier 3 — paid

15. **Placer.ai / SafeGraph** — cell-derived foot traffic; what institutional buyers use
16. **CoStar** — real commercial tenancy and **lease expiry dates**; transformative and
    otherwise nearly unobtainable

## Tier 4 — unconventional but legitimate

17. **Historical Street View time-series** — imagery back to ~2007 on an API already in
    use. Diffing 2011 vs 2024 reveals tenant churn (signage changes), physical decay, or
    a lot empty for a decade. Powerful and nearly free.
18. **Social media dormancy** — stopped posting 14 months ago = in trouble
19. **Delivery-platform delisting** — vanishing from DoorDash/UberEats precedes closure
20. **Domain expiry / Wayback last-update** — dead website, dead business
21. **Job postings** — hiring = growing; silent for years = shrinking
22. **Obituary matching on owner names** — estate sales follow; PropStream's
    `deceased_owner` field is sparse
23. **Sign permit filings** — a new sign means a just-turned-over tenant

## Standing caveat

Business Value is 15% of the score and applies to only 13% of properties. Even excellent
business data has a capped effect until coverage rises — consider whether the weight
should change alongside any coverage improvement.

## If picking three

1. Proximity matching — doubles existing data for free
2. GeoTracker contamination — protects against an expensive false positive
3. Review recency — fixes the signal already being relied on
