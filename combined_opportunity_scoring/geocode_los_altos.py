"""One-off backfill: get lat/lng for Los Altos properties missing coordinates.

Uses the Street View Static Metadata endpoint (free, no image download) to
geocode by address. Its metadata response's `location.lat`/`location.lng`
resolves a real-world coordinate near the property even for address-only
queries -- this is what the pipeline already calls per property to check for
a Street View image, but only ever kept the `date` field and discarded the
coordinates. Writing them to a lookup CSV lets build_report.py's
load_properties() apply them as a fallback where LATITUDE/LONGITUDE are
missing from the source data, unlocking the zoning/GP spatial join for
these properties on the next report build.
"""
import sys
import time
import requests

sys.path.insert(0, ".")
import build_report as br

OUT_PATH = r"C:\Users\jessi\Downloads\Autonomation Lab\Property Scoring\la_geocoded_coords.csv"


def main():
    props = br.load_properties()
    la = [p for p in props if p["city"] == "Los Altos"]
    missing = [p for p in la if p.get("lat") is None or p.get("lng") is None]
    print(f"Los Altos: {len(la)} total, {len(missing)} missing coordinates")

    if not br.GOOGLE_MAPS_API_KEY:
        print("ERROR: GOOGLE_MAPS_API_KEY not set")
        return

    session = requests.Session()
    results = []
    ok = 0
    zero = 0
    err = 0

    for i, p in enumerate(missing, 1):
        addr = p["address"].strip()
        if not addr or not any(c.isdigit() for c in addr):
            # No street number at all (e.g. "1ST ST") -- not geocodable
            zero += 1
            continue

        location = f"{p['address']}, {p['city']}, CA {p['zip']}".strip()
        try:
            meta = session.get(
                "https://maps.googleapis.com/maps/api/streetview/metadata",
                params={"location": location, "key": br.GOOGLE_MAPS_API_KEY},
                timeout=20,
            ).json()
        except requests.RequestException as e:
            err += 1
            continue

        if meta.get("status") == "OK" and meta.get("location"):
            apn_norm = br.normalize_apn_digits(p["apn"])
            results.append({
                "apn_norm": apn_norm,
                "address": p["address"],
                "lat": meta["location"]["lat"],
                "lng": meta["location"]["lng"],
            })
            ok += 1
        else:
            zero += 1

        if i % 50 == 0:
            print(f"  {i}/{len(missing)} processed -- {ok} geocoded, {zero} not found, {err} errors")
        time.sleep(0.05)

    print(f"Done. {ok} geocoded, {zero} not found/ungeocodable, {err} errors")

    import csv
    with open(OUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["apn_norm", "address", "lat", "lng"])
        writer.writeheader()
        writer.writerows(results)
    print(f"Wrote {len(results)} rows to {OUT_PATH}")


if __name__ == "__main__":
    main()
