"""
build_zoning_gp_lookup.py
--------------------------
Resolves BOTH current zoning and General Plan (future/aspirational) land-use
designation for every Los Altos, Palo Alto, and Menlo Park property in
combined_commercial_properties_cleaned.xlsx, using the real GIS/PDF sources
downloaded this session (parcel-level fields where available, spatial join
against zoning/GP polygon layers otherwise). Atherton is skipped — no usable
zoning or GP source was found for it.

Output: zoning_gp_lookup.csv, keyed by normalized APN, with columns
apn_norm, city, current_zoning, gp_designation. build_report.py loads this
directly rather than repeating these joins at score time.
"""

import re
import pandas as pd
import geopandas as gpd

ROOT = r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring"


def norm_apn(v):
    return re.sub(r"[^0-9]", "", str(v or ""))


def split_apns(v):
    """Some properties are multi-parcel assemblages (e.g. a shopping center
    spanning several assessor parcels) and their APN field is a "; "-joined
    list, e.g. "055-440-040; 055-440-050; 055-440-310; 055-440-320". Naively
    normalizing that whole string concatenates all the digits into one
    unmatchable blob, silently losing any per-APN lookup (ZONEGIS-by-APN,
    GP-by-APN) for 45 real properties across all 4 cities. Split first, then
    normalize each APN individually, so callers can check every sub-parcel."""
    return [norm_apn(part) for part in re.split(r"[;,]", str(v or "")) if norm_apn(part)]


def first_match(lookup_dict, raw_apn_field):
    for apn in split_apns(raw_apn_field):
        val = lookup_dict.get(apn)
        if val:
            return val
    return None


def spatial_join_field(points_gdf, id_col, poly_path, poly_field):
    """Point-in-polygon join; returns {id: field_value} for matched points."""
    poly = gpd.read_file(poly_path)
    poly = poly[poly[poly_field].notna()][[poly_field, "geometry"]]
    if poly.crs is None:
        poly = poly.set_crs(4326)
    elif poly.crs.to_epsg() != 4326:
        poly = poly.to_crs(4326)
    poly = poly.reset_index(drop=True)
    poly_proj = poly.to_crs(poly.estimate_utm_crs())
    poly_proj["_area"] = poly_proj.geometry.area

    joined = gpd.sjoin(points_gdf, poly, how="left", predicate="within")
    joined = joined.join(poly_proj["_area"], on="index_right")
    joined = joined.sort_values("_area", ascending=False).drop_duplicates(subset=id_col, keep="first")
    return dict(zip(joined[id_col], joined[poly_field]))


def make_points(df, id_col):
    return gpd.GeoDataFrame(
        {id_col: df[id_col]},
        geometry=gpd.points_from_xy(df["LONGITUDE"], df["LATITUDE"]),
        crs="EPSG:4326",
    )


def main():
    props = pd.read_excel(rf"{ROOT}\combined_commercial_properties_cleaned.xlsx")
    props["apn_norm"] = props["APN"].apply(norm_apn)
    props["_row_id"] = range(len(props))

    # Backfill missing LATITUDE/LONGITUDE for Los Altos from the Street View
    # metadata geocoding pass (geocode_los_altos.py) -- keyed by the same
    # apn_norm derivation used here, so it lines up even for the
    # multi-parcel rows above. Only fills rows that don't already have coords.
    try:
        coords = pd.read_csv(rf"{ROOT}\la_geocoded_coords.csv", dtype=str)
        coord_by_apn = {row["apn_norm"]: (float(row["lat"]), float(row["lng"])) for _, row in coords.iterrows()}
        missing_mask = props["LATITUDE"].isna() | props["LONGITUDE"].isna()
        filled = 0
        for idx in props[missing_mask].index:
            backfilled = coord_by_apn.get(props.at[idx, "apn_norm"])
            if backfilled:
                props.at[idx, "LATITUDE"], props.at[idx, "LONGITUDE"] = backfilled
                filled += 1
        print(f"Backfilled coordinates for {filled} properties from la_geocoded_coords.csv")
    except FileNotFoundError:
        pass

    results = {}  # apn_norm -> {city, current_zoning, gp_designation}

    # ── Los Altos: spatial join against real ZoningDistrict + General Plan layers ──
    la = props[(props["City"] == "Los Altos") & props["LATITUDE"].notna() & props["LONGITUDE"].notna()]
    if len(la):
        pts = make_points(la, "_row_id")
        zoning_by_id = spatial_join_field(pts, "_row_id", rf"{ROOT}\los_altos_zoning_district.geojson", "ZONING")
        gp_by_id = spatial_join_field(pts, "_row_id", rf"{ROOT}\los_altos_general_plan.geojson", "LANDUSEDESC")
        for _, row in la.iterrows():
            rid = row["_row_id"]
            results[row["apn_norm"]] = {
                "city": "Los Altos",
                "current_zoning": zoning_by_id.get(rid),
                "gp_designation": gp_by_id.get(rid),
            }
        print(f"[Los Altos] {len(la)} properties | zoning matched: {sum(1 for v in results.values() if v['city']=='Los Altos' and v['current_zoning'])} "
              f"| GP matched: {sum(1 for v in results.values() if v['city']=='Los Altos' and v['gp_designation'])}")

    # ── Palo Alto: current zoning via parcel-level ZONEGIS (APN join, exact),
    #    GP via spatial join against the LandUse polygon layer ──
    pa = props[props["City"] == "Palo Alto"]
    pa_parcels = gpd.read_file(rf"{ROOT}\palo_alto_parcels.geojson")
    pa_parcels["apn_norm"] = pa_parcels["APN"].apply(norm_apn)
    zonegis_by_apn = dict(zip(pa_parcels["apn_norm"], pa_parcels["ZONEGIS"]))

    pa_coords = pa[pa["LATITUDE"].notna() & pa["LONGITUDE"].notna()]
    gp_by_id = {}
    if len(pa_coords):
        pts = make_points(pa_coords, "_row_id")
        gp_by_id = spatial_join_field(pts, "_row_id", rf"{ROOT}\palo_alto_general_plan.geojson", "DESIGNATIO")

    zoning_matched = gp_matched = 0
    for _, row in pa.iterrows():
        zoning = first_match(zonegis_by_apn, row["APN"])
        gp = gp_by_id.get(row["_row_id"])
        if zoning:
            zoning_matched += 1
        if gp:
            gp_matched += 1
        results[row["apn_norm"]] = {"city": "Palo Alto", "current_zoning": zoning, "gp_designation": gp}
    print(f"[Palo Alto] {len(pa)} properties | zoning matched: {zoning_matched} | GP matched: {gp_matched}")

    # ── Menlo Park: current zoning already resolved via the real GIS spatial
    #    join at score time (config.CITY_ZONING_GEOJSON) — this lookup only
    #    needs to supply GP designation, extracted from the Housing Element
    #    PDF's site inventory table earlier this session ──
    mp = props[props["City"] == "Menlo Park"]
    mp_gp = pd.read_csv(rf"{ROOT}\menlo_park_gp_zoning_by_apn.csv", dtype=str)
    gp_by_apn = dict(zip(mp_gp["apn_norm"], mp_gp["gp_designation_current"]))
    gp_matched = 0
    for _, row in mp.iterrows():
        gp = first_match(gp_by_apn, row["APN"])
        if gp:
            gp_matched += 1
        results[row["apn_norm"]] = {"city": "Menlo Park", "current_zoning": None, "gp_designation": gp}
    print(f"[Menlo Park] {len(mp)} properties | GP matched: {gp_matched} (current zoning handled separately via existing spatial join)")

    out = pd.DataFrame([
        {"apn_norm": apn, **v} for apn, v in results.items() if apn
    ])
    out_path = rf"{ROOT}\zoning_gp_lookup.csv"
    out.to_csv(out_path, index=False)
    print(f"\nWrote {out_path} ({len(out)} rows)")


if __name__ == "__main__":
    main()
