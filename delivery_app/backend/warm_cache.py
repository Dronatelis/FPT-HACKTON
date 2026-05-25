"""Pre-warm the Google Maps geocoding cache.

Run once on a machine with internet access BEFORE starting the FastAPI
server. Walks data/packages.csv, queries the Google Maps Geocoding API
sequentially with a 0.1-second pause between requests (Google's rate
limits are far more generous than Nominatim's), and persists every result
into the SQLite cache so subsequent server starts are instant.

Requires the GOOGLE_MAPS_API_KEY environment variable to be set.

For ~1050 addresses this takes ~2 minutes the first time. After that the
cache survives reboots / git pulls / etc.

Usage:
    cd backend
    GOOGLE_MAPS_API_KEY=your_key_here python warm_cache.py
"""

from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import os
from geopy.geocoders import GoogleV3

from optimizer import (
    geocode_address, _init_cache, is_kosice_address,
    split_address, lookup_district,
)

CSV = Path(__file__).resolve().parent.parent / "data" / "packages.csv"


def rewrite_address(row):
    """Apply the same address rewrite as load_packages so the cache key
    matches what FastAPI will look up at runtime."""
    street, house, _ = split_address(row["address"])
    in_k, _ = is_kosice_address(row["address"])
    if in_k:
        district, _ = lookup_district(street, row.get("city_district",""))
        return f"{street} {house}, Košice - {district}".strip()
    return row["address"]


def main():
    if not CSV.exists():
        print(f"ERR: {CSV} not found"); sys.exit(1)
    conn = _init_cache()
    geo = GoogleV3(api_key=os.environ.get("GOOGLE_MAPS_API_KEY"))
    df = pd.read_csv(CSV, encoding="utf-8-sig")
    print(f"Warming Google Maps geocode cache for {len(df)} addresses...")
    print("(0.1 s pause between requests — ETA: " + f"{len(df)*0.1/60:.1f} minutes)")
    print()
    ok = fail = 0
    for i, row in df.iterrows():
        # Skip explicitly non-Košice addresses — no point querying
        in_k, reason = is_kosice_address(row["address"])
        if not in_k:
            print(f"  [{i+1:4}/{len(df)}] SKIP  {row['barcode']}  {row['address'][:55]:55s}  {reason}")
            fail += 1; continue

        rewritten = rewrite_address(row)
        # request_delay defaults to 0.1 s inside geocode_address
        r = geocode_address(conn, rewritten, row.get("city_district", ""), geolocator=geo)
        if r["valid"]:
            ok += 1
            print(f"  [{i+1:4}/{len(df)}] OK    {row['barcode']}  {rewritten[:55]:55s}  ({r['lat']:.5f}, {r['lon']:.5f})")
        else:
            fail += 1
            print(f"  [{i+1:4}/{len(df)}] FAIL  {row['barcode']}  {rewritten[:55]:55s}  {r['raw'].get('reason','?')}")
    print()
    print(f"=== Done. valid={ok}, failed/skipped={fail} ===")


if __name__ == "__main__":
    main()
