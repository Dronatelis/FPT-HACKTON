"""
geocode_demo.py — Geocode every delivery address via Google Maps and write
accurate coordinates back into demo.html and driver-app.html.

Run ONCE after setting your API key:

    cd delivery_app
    GOOGLE_MAPS_API_KEY=AIza...yourkey... python3 geocode_demo.py

On Windows (Command Prompt):
    set GOOGLE_MAPS_API_KEY=AIza...yourkey...
    python geocode_demo.py

On Windows (PowerShell):
    $env:GOOGLE_MAPS_API_KEY="AIza...yourkey..."
    python geocode_demo.py

Requirements:  pip install geopy

What it does:
  1. Reads all package addresses from demo.html's embedded DATA block.
  2. Geocodes each unique address via Google Maps Geocoding API.
     - Results are cached in geocode_cache.json so you can safely interrupt
       and re-run without burning API quota.
     - Slovak "X/Y" house numbers are normalised to the orientačné číslo
       (the street-visible number) before the query.
  3. Writes accurate lat/lon back into demo.html AND driver-app.html.
  4. Prints a summary: how many succeeded, how many fell back to the
     district centroid (only if Google returns nothing for that address).

Approximate cost: ~315 unique addresses × $0.005 per 1000 = < $0.002
"""

from __future__ import annotations
import json, os, re, sys, time
from pathlib import Path

# ── locate files ──────────────────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).resolve().parent
DEMO_HTML    = SCRIPT_DIR / "demo.html"
DRIVER_HTML  = SCRIPT_DIR / "driver-app.html"
CACHE_FILE   = SCRIPT_DIR / "geocode_cache.json"

# ── Košice bounding box ───────────────────────────────────────────────────────
BBOX = dict(lat_min=48.55, lat_max=48.83, lon_min=21.10, lon_max=21.45)

# ── district centroid fallbacks (used only when Google returns no result) ─────
DISTRICT_CENTROIDS = {
    "Staré Mesto":         (48.7197, 21.2588),
    "Sever":               (48.7370, 21.2530),
    "Juh":                 (48.6980, 21.2610),
    "Západ":               (48.7168, 21.2295),
    "Sídlisko KVP":        (48.7195, 21.2155),
    "Sídlisko Ťahanovce":  (48.7462, 21.2685),
    "Dargovských hrdinov": (48.7325, 21.2795),
    "Nad jazerom":         (48.6862, 21.2810),
    "Vyšné Opátske":       (48.6920, 21.2940),
    "Krásna":              (48.6788, 21.3095),
    "Barca":               (48.6665, 21.2560),
    "Šaca":                (48.6428, 21.1768),
    "Poľov":               (48.6638, 21.1705),
    "Pereš":               (48.6895, 21.1885),
    "Lorinčík":            (48.6975, 21.2008),
    "Myslava":             (48.7162, 21.1978),
    "Kavečany":            (48.7785, 21.2265),
    "Košická Nová Ves":    (48.7462, 21.3005),
}


def _in_kosice(lat: float, lon: float) -> bool:
    return (BBOX["lat_min"] <= lat <= BBOX["lat_max"] and
            BBOX["lon_min"] <= lon <= BBOX["lon_max"])


def _clean_address(address: str) -> str:
    """Normalise Slovak X/Y house numbers → keep orientačné číslo (after /)."""
    return re.sub(r"\b(\d+)/(\d+)\b", r"\2", address)


def _extract_data(html: str):
    """Return (data_dict, start_char, end_char) for the DATA = {...} block."""
    m = re.search(r"(?:const|let)\s+DATA\s*=\s*(\{)", html)
    if not m:
        raise ValueError("Could not find DATA = { ... } in HTML")
    start = m.start(1)
    depth = 0
    for i in range(start, len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    return json.loads(html[start:end]), start, end


def geocode_all(packages: list[dict], api_key: str) -> dict[str, tuple[float, float]]:
    """Return {address: (lat, lon)} for every unique address in packages."""
    from geopy.geocoders import GoogleV3

    geo = GoogleV3(api_key=api_key)

    # Load existing cache
    cache: dict[str, tuple] = {}
    if CACHE_FILE.exists():
        try:
            cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            print(f"  Loaded {len(cache)} cached results from {CACHE_FILE.name}")
        except Exception:
            pass

    unique_addresses = {p["address"] for p in packages if p.get("address")}
    todo = [a for a in sorted(unique_addresses) if a not in cache]
    print(f"  {len(unique_addresses)} unique addresses — {len(cache)} already cached — {len(todo)} to geocode")

    ok = fail = 0
    for idx, addr in enumerate(todo, 1):
        clean = _clean_address(addr)
        # Append ", Košice, Slovakia" if not already present
        if "košice" not in clean.lower():
            query = f"{clean}, Košice, Slovakia"
        else:
            query = clean

        try:
            time.sleep(0.12)          # stay well within Google's rate limit
            loc = geo.geocode(query, timeout=10)
            if loc and _in_kosice(loc.latitude, loc.longitude):
                cache[addr] = (round(loc.latitude, 6), round(loc.longitude, 6))
                ok += 1
                print(f"  [{idx:3}/{len(todo)}] OK    {addr[:60]:60s} → ({loc.latitude:.5f},{loc.longitude:.5f})")
            else:
                cache[addr] = None   # mark as failed so we don't retry
                fail += 1
                print(f"  [{idx:3}/{len(todo)}] FAIL  {addr[:60]:60s} (no result in Košice bbox)")
        except Exception as e:
            print(f"  [{idx:3}/{len(todo)}] ERROR {addr[:55]:55s}: {e}")
            # Don't cache errors — allow retry next run

        # Save cache after every request so interrupts lose nothing
        CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n  Geocoding done — OK: {ok}, failed: {fail}, cached: {len(cache)-ok-fail}")
    return cache


def apply_coordinates(html_path: Path, cache: dict, fallback_district: bool = True) -> int:
    """Update lat/lon in an HTML file. Returns number of packages updated."""
    html = html_path.read_text(encoding="utf-8")
    data, start, end = _extract_data(html)
    packages = data["packages"]

    updated = fallbacks = missing = 0
    for pkg in packages:
        addr = pkg.get("address", "")
        result = cache.get(addr)

        if result:                        # Google gave us a real coordinate
            pkg["lat"], pkg["lon"] = result
            updated += 1
        elif fallback_district:           # Fall back to district centroid
            district = pkg.get("city_district", "")
            centroid = DISTRICT_CENTROIDS.get(district)
            if centroid:
                pkg["lat"], pkg["lon"] = centroid
                fallbacks += 1
            else:
                missing += 1
        else:
            missing += 1

    new_data = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    new_html = html[:start] + new_data + html[end:]
    html_path.write_text(new_html, encoding="utf-8")

    print(f"  {html_path.name}: {updated} Google coords, {fallbacks} centroid fallbacks, {missing} missing")
    return updated


def main():
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not api_key:
        print("ERROR: GOOGLE_MAPS_API_KEY environment variable is not set.")
        print("       Set it and re-run this script.")
        sys.exit(1)

    if not DEMO_HTML.exists():
        print(f"ERROR: {DEMO_HTML} not found. Run this script from the delivery_app folder.")
        sys.exit(1)

    print("=== KE-Delivery geocoder ===\n")

    # Read packages from demo.html (single source of truth)
    html = DEMO_HTML.read_text(encoding="utf-8")
    data, _, _ = _extract_data(html)
    packages = data["packages"]
    print(f"Step 1: Geocoding {len(packages)} packages via Google Maps …\n")
    cache = geocode_all(packages, api_key)

    print("\nStep 2: Writing coordinates into HTML files …")
    apply_coordinates(DEMO_HTML,   cache)
    if DRIVER_HTML.exists():
        apply_coordinates(DRIVER_HTML, cache)

    print("\n=== Done! Reload demo.html in your browser. ===")
    print(f"(Cache saved to {CACHE_FILE} — re-running is free for already-geocoded addresses)")


if __name__ == "__main__":
    main()
