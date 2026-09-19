"""
find_lot_assemblage.py
-----------------------
Finds clusters of adjacent small parcels (each < 1 acre individually) that
would clear a meaningful townhome-development size threshold if assembled
together. Uses real parcel-boundary GeoJSON files (downloaded from each
city's/county's public GIS) for true adjacency detection, not a lat/long
proximity guess.

Deliberately NOT folded into build_report.py's per-property scoring:
assemblage is speculative and multi-party (often different owners), so it's
surfaced as a separate report rather than silently boosting a property's own
opportunity score. See combined_opportunity_scoring/build_report.py for the
main per-property scoring pipeline this complements.

Output: lot_assemblage_candidates.csv - one row per cluster, listing member
APNs/addresses/combined acreage, and whether any member is already in our
scored commercial dataset (combined_opportunity_scores.csv).
"""

import re
import pandas as pd
import geopandas as gpd

SMALL_PARCEL_ACRES = 1.0       # only cluster parcels individually below this
MIN_COMBINED_ACRES = 0.75      # only report clusters whose combined size clears this
MAX_CLUSTER_MEMBERS = 6        # cap cluster size — beyond this it's an entire subdivision
                                # (dozens of separately-owned parcels), not a realistically
                                # assemblable near-term opportunity
BUFFER_METERS = 1.0            # small buffer to bridge survey-tolerance gaps between true neighbors

CITY_PARCEL_FILES = {
    "Atherton": "atherton_parcels.geojson",
    "Palo Alto": "palo_alto_parcels.geojson",
    "Los Altos": "los_altos_parcels.geojson",
    "Menlo Park": "menlo_park_parcels.geojson",
}

SQM_PER_ACRE = 4046.8564224


def normalize_apn(v):
    return re.sub(r"[^0-9A-Za-z]", "", str(v or "")).upper()


class UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def find_clusters_for_city(city_name, path, scored_apns):
    gdf = gpd.read_file(path)
    gdf["APN_NORM"] = gdf["APN"].apply(normalize_apn)

    utm_crs = gdf.estimate_utm_crs()
    proj = gdf.to_crs(utm_crs)
    proj["acres"] = proj.geometry.area / SQM_PER_ACRE

    small = proj[proj["acres"] < SMALL_PARCEL_ACRES].reset_index(drop=True)
    if small.empty:
        return []

    buffered = small.copy()
    buffered["geometry"] = buffered.geometry.buffer(BUFFER_METERS)

    joined = gpd.sjoin(buffered, buffered, predicate="intersects", how="inner")
    joined = joined[joined.index != joined["index_right"]]

    uf = UnionFind(len(small))
    for i, j in zip(joined.index, joined["index_right"]):
        uf.union(i, j)

    groups = {}
    for i in range(len(small)):
        groups.setdefault(uf.find(i), []).append(i)

    clusters = []
    for members in groups.values():
        if len(members) < 2 or len(members) > MAX_CLUSTER_MEMBERS:
            continue
        sub = small.iloc[members]
        combined_acres = sub["acres"].sum()
        if combined_acres < MIN_COMBINED_ACRES:
            continue
        member_apns = sub["APN"].tolist()
        member_apns_norm = sub["APN_NORM"].tolist()
        has_scored = any(a in scored_apns for a in member_apns_norm)
        clusters.append({
            "city": city_name,
            "member_count": len(members),
            "combined_acres": round(combined_acres, 3),
            "member_apns": "; ".join(member_apns),
            "individual_acres": "; ".join(f"{a:.2f}" for a in sub["acres"]),
            "has_scored_property": has_scored,
        })
    return clusters


def main():
    scored = pd.read_csv(
        r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\combined_opportunity_scoring\combined_opportunity_scores.csv"
    )
    scored["apn_norm"] = scored["apn"].dropna().apply(normalize_apn)
    scored_by_apn = {
        row["apn_norm"]: row for _, row in scored.dropna(subset=["apn"]).iterrows()
    }
    scored_apns = set(scored_by_apn.keys())

    all_clusters = []
    for city_name, filename in CITY_PARCEL_FILES.items():
        path = rf"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\{filename}"
        clusters = find_clusters_for_city(city_name, path, scored_apns)
        print(f"[{city_name}] {len(clusters)} total small-parcel cluster(s) "
              f"(combined >= {MIN_COMBINED_ACRES} ac, from parcels individually < {SMALL_PARCEL_ACRES} ac)")
        all_clusters.extend(clusters)

    # Only clusters that actually touch one of our already-scored commercial
    # properties are actionable here — a generic block of small adjacent
    # residential lots isn't a lead unless it's anchored to a property we're
    # already tracking. Attach that property's real address/score for context.
    relevant = [c for c in all_clusters if c["has_scored_property"]]
    for c in relevant:
        matches = [
            scored_by_apn[a] for a in
            [normalize_apn(x) for x in c["member_apns"].split("; ")]
            if a in scored_by_apn
        ]
        c["anchor_address"] = "; ".join(m["address"] for m in matches)
        c["anchor_opportunity_score"] = "; ".join(f"{m['opportunity_score']:.1f}" for m in matches)
        del c["has_scored_property"]

    df = pd.DataFrame(relevant).sort_values("combined_acres", ascending=False)
    out_path = r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\lot_assemblage_candidates.csv"
    df.to_csv(out_path, index=False)

    print()
    print(f"Total small-parcel clusters city-wide: {len(all_clusters)}")
    print(f"Clusters anchored to an already-scored commercial property: {len(df)}")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
