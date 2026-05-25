"""
KE-Delivery Vehicle Routing Problem (VRP) Optimizer

Production pipeline for Košice fleet operations:
  1. Parse + clean CSV inputs (UTF-8-BOM aware).
  2. Split each address into (street, house_number, declared_district).
  3. Validate street against the authoritative `streets.csv` registry
     (exact, then fuzzy word-overlap ignoring stopwords ulica/cesta/trieda).
  4. Override city_district with the registry value; apply district aliases
     (Ťahanovce → Sídlisko Ťahanovce). Rewrite displayed address so its
     suffix matches the authoritative district; keep raw_address for audit.
  5. Geocode EXCLUSIVELY via Nominatim, with sequential 1.1s throttle and a
     SQLite cache. No jitter / no fake fallback. Failed lookups become
     INVALID_ADDRESS and flow to the dispatcher queue.
  6. VRP heuristic with STRICT zone matching
     (package.city_district == driver.zone_mestska_cast).
  7. Capacity: weight, 85% volume, package count. Priority tiers
     (Overnight/Meltable → Express → Standard). Mandatory 30-min break per
     4h work. Multi-trip restocking (1h reload).
  8. LIFO loading order.
  9. Cross-zone scan for idle drivers.
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

try:
    from geopy.geocoders import GoogleV3
    from geopy.exc import GeocoderTimedOut, GeocoderServiceError
    GEOPY_AVAILABLE = True
except Exception:
    GEOPY_AVAILABLE = False


# Constants — Košice geography
KOSICE_DEPOT_LAT = 48.7194
KOSICE_DEPOT_LON = 21.2581
KOSICE_BBOX = {"min_lat": 48.62, "max_lat": 48.82, "min_lon": 21.10, "max_lon": 21.42}

AVERAGE_SPEED_KMH = 28.0
STOP_SERVICE_MIN = 2.0
STOP_SERVICE_MAX = 3.0
BREAK_DURATION_MIN = 30
WORK_BEFORE_BREAK_MIN = 4 * 60
RESTOCK_DURATION_MIN = 60
VOLUME_USABLE_RATIO = 0.85

DISTRICT_ADJACENCY: dict[str, list[str]] = {
    "Staré Mesto": ["Sever", "Juh", "Západ", "Dargovských hrdinov"],
    "Sever": ["Staré Mesto", "Kavečany", "Sídlisko Ťahanovce", "Dargovských hrdinov"],
    "Juh": ["Staré Mesto", "Nad jazerom", "Barca", "Západ"],
    "Západ": ["Staré Mesto", "Sídlisko KVP", "Juh", "Myslava", "Pereš"],
    "Sídlisko KVP": ["Západ", "Myslava", "Pereš", "Lorinčík"],
    "Sídlisko Ťahanovce": ["Sever", "Dargovských hrdinov", "Kavečany"],
    "Dargovských hrdinov": ["Sever", "Sídlisko Ťahanovce", "Staré Mesto", "Košická Nová Ves"],
    "Nad jazerom": ["Juh", "Krásna", "Barca", "Vyšné Opátske"],
    "Vyšné Opátske": ["Nad jazerom", "Krásna", "Juh"],
    "Krásna": ["Nad jazerom", "Vyšné Opátske", "Barca"],
    "Barca": ["Juh", "Nad jazerom", "Krásna", "Šaca"],
    "Šaca": ["Barca", "Poľov", "Lorinčík"],
    "Poľov": ["Šaca", "Lorinčík", "Pereš"],
    "Pereš": ["Lorinčík", "Poľov", "Sídlisko KVP", "Západ"],
    "Lorinčík": ["Sídlisko KVP", "Pereš", "Poľov", "Šaca", "Myslava"],
    "Myslava": ["Západ", "Sídlisko KVP", "Lorinčík", "Kavečany"],
    "Kavečany": ["Sever", "Sídlisko Ťahanovce", "Myslava"],
    "Košická Nová Ves": ["Dargovských hrdinov", "Sídlisko Ťahanovce"],
}

DISTRICT_CENTROIDS: dict[str, tuple[float, float]] = {
    "Staré Mesto": (48.7203, 21.2580),
    "Sever": (48.7385, 21.2520),
    "Juh": (48.6985, 21.2620),
    "Západ": (48.7170, 21.2280),
    "Sídlisko KVP": (48.7195, 21.2150),
    "Sídlisko Ťahanovce": (48.7470, 21.2680),
    "Dargovských hrdinov": (48.7320, 21.2810),
    "Nad jazerom": (48.6850, 21.2820),
    "Vyšné Opátske": (48.6920, 21.2950),
    "Krásna": (48.6770, 21.3100),
    "Barca": (48.6660, 21.2570),
    "Šaca": (48.6420, 21.1750),
    "Poľov": (48.6630, 21.1700),
    "Pereš": (48.6890, 21.1880),
    "Lorinčík": (48.6960, 21.2010),
    "Myslava": (48.7180, 21.1980),
    "Kavečany": (48.7790, 21.2270),
    "Košická Nová Ves": (48.7470, 21.3000),
}

CACHE_DIR = Path(os.environ.get("KE_CACHE_DIR", Path(__file__).resolve().parent / ".cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)
SQLITE_CACHE = CACHE_DIR / "geocode_cache.sqlite"


@dataclass
class Stop:
    package_id: int
    barcode: str
    recipient_name: str
    address: str
    city_district: str
    lat: float
    lon: float
    weight_kg: float
    volume_m3: float
    fragile: bool
    priority: str
    package_type: str
    payment_method: str
    cod_amount_eur: float | None
    special_instructions: str
    eta_minutes_from_shift_start: float = 0.0
    eta_clock: str = ""
    service_minutes: float = 0.0


@dataclass
class Route:
    driver_id: str
    driver_name: str
    vehicle_id: str
    license_plate: str
    vehicle_make_model: str
    zone_mestska_cast: str
    max_weight_kg: float
    max_volume_m3: float
    max_packages_count: int
    shift_start: str
    shift_end: str
    stops: list[Stop] = field(default_factory=list)
    loading_order: list[Stop] = field(default_factory=list)
    used_weight_kg: float = 0.0
    used_volume_m3: float = 0.0
    used_packages: int = 0
    total_distance_km: float = 0.0
    total_minutes: float = 0.0
    breaks: list[dict] = field(default_factory=list)
    restocks: list[dict] = field(default_factory=list)
    trip_count: int = 1


@dataclass
class CrossZoneRequest:
    request_id: str
    driver_id: str
    driver_name: str
    home_zone: str
    target_zone: str
    package_ids: list[int]
    extra_packages: int
    extra_weight_kg: float
    extra_volume_m3: float
    status: str = "PENDING"


# ---- Geocoding cache (SQLite, survives restarts) ----

def _init_cache() -> sqlite3.Connection:
    conn = sqlite3.connect(SQLITE_CACHE)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS geocode (
                address TEXT PRIMARY KEY,
                lat REAL, lon REAL, suburb TEXT, valid INTEGER,
                raw TEXT, fetched_at TEXT)"""
    )
    conn.commit()
    return conn


def _cache_get(conn, address):
    row = conn.execute("SELECT lat, lon, suburb, valid FROM geocode WHERE address = ?", (address,)).fetchone()
    if not row: return None
    return {"lat": row[0], "lon": row[1], "suburb": row[2], "valid": bool(row[3])}


def _cache_put(conn, address, entry):
    conn.execute(
        "INSERT OR REPLACE INTO geocode VALUES (?, ?, ?, ?, ?, ?, ?)",
        (address, entry.get("lat"), entry.get("lon"), entry.get("suburb"),
         int(bool(entry.get("valid"))), json.dumps(entry.get("raw", {}), ensure_ascii=False),
         datetime.utcnow().isoformat()))
    conn.commit()


def _within_kosice(lat, lon):
    return (KOSICE_BBOX["min_lat"] <= lat <= KOSICE_BBOX["max_lat"]
            and KOSICE_BBOX["min_lon"] <= lon <= KOSICE_BBOX["max_lon"])


def split_house_number(house: str) -> tuple[str, str]:
    """Slovak house numbers come as 'súpisné/orientačné' (e.g. 115/1).

    Nominatim does best with the *orientation* number (the part after the
    slash) because that's the street-facing house number. The descriptive
    number (before the slash) is a building registry number unique to the
    municipality and Nominatim often does not index it.

    Returns (orient, sup). If the number is a single value, it is returned
    as `orient` and `sup` is empty.
    """
    if not isinstance(house, str) or not house.strip():
        return "", ""
    parts = [p.strip() for p in house.split("/", 1)]
    if len(parts) == 2:
        return parts[1], parts[0]
    return parts[0], ""


def geocode_address(conn, address, original_district, geolocator=None, request_delay=0.1):
    """Geocode `address` via the Google Maps Geocoding API.

    Google handles full Slovak address strings natively — including slashed
    house numbers (130/7) — so no multi-step fallback ladder is needed.
    The cache contract (_cache_get / _cache_put) and the returned dict shape
    are identical to the old Nominatim implementation so the rest of the VRP
    pipeline requires zero changes.
    """
    cached = _cache_get(conn, address)
    if cached is not None:
        return cached

    if not (GEOPY_AVAILABLE and geolocator is not None):
        return {
            "valid": False, "lat": None, "lon": None, "suburb": None,
            "match_level": "none",
            "raw": {"reason": "google_geocoder_not_configured"},
        }

    # Append city context only when not already present in the string.
    addr_low = address.lower()
    if "košice" in addr_low or "kosice" in addr_low:
        query = address
    else:
        query = f"{address}, Košice, Slovakia"

    try:
        time.sleep(request_delay)
        location = geolocator.geocode(query, timeout=10)

        if not location or not _within_kosice(location.latitude, location.longitude):
            entry = {
                "valid": False, "lat": None, "lon": None, "suburb": None,
                "match_level": "failed",
                "raw": {"reason": "no_result_within_kosice", "query": query},
            }
            _cache_put(conn, address, entry)
            return entry

        raw = location.raw or {}

        # Pull the sub-locality from Google's address_components when available.
        suburb = original_district
        for component in raw.get("address_components", []):
            if any(t in component.get("types", [])
                   for t in ("sublocality", "sublocality_level_1", "neighborhood")):
                suburb = component.get("long_name", original_district)
                break

        entry = {
            "valid": True,
            "lat": location.latitude,
            "lon": location.longitude,
            "suburb": suburb,
            "match_level": "google_maps",
            "raw": raw,
        }
        _cache_put(conn, address, entry)
        return entry

    except Exception as e:
        entry = {
            "valid": False, "lat": None, "lon": None, "suburb": None,
            "match_level": "failed",
            "raw": {"reason": "google_geocoder_error",
                    "query": query, "error": str(e)[:200]},
        }
        _cache_put(conn, address, entry)
        return entry


def parse_dimensions(s):
    if not isinstance(s, str) or "x" not in s.lower(): return 0.0
    try:
        parts = [float(p.strip()) for p in s.lower().replace("×","x").split("x")]
        if len(parts) != 3: return 0.0
        return (parts[0]*parts[1]*parts[2]) / 1_000_000.0
    except ValueError:
        return 0.0


# ---- Streets registry + address parsing ----

STREETS_CSV = Path(os.environ.get("KE_STREETS_CSV",
                                  Path(__file__).resolve().parent.parent / "data" / "streets.csv"))

DISTRICT_ALIASES: dict[str, str] = {
    "Ťahanovce": "Sídlisko Ťahanovce",
}


def _canonical_district(d):
    return DISTRICT_ALIASES.get(d, d) if d else d


_STREETS_INDEX = None
def load_streets_index():
    global _STREETS_INDEX
    if _STREETS_INDEX is not None: return _STREETS_INDEX
    idx = {}
    if not STREETS_CSV.exists():
        _STREETS_INDEX = {}; return _STREETS_INDEX
    df = pd.read_csv(STREETS_CSV, encoding="utf-8")
    for _, row in df.iterrows():
        s = str(row["street_name"]).strip()
        d = str(row["city_district"]).strip()
        if not s or not d: continue
        key = s.lower()
        if d not in idx.setdefault(key, []):
            idx[key].append(d)
    _STREETS_INDEX = idx
    return idx


_HOUSE_NUMBER_RE = re.compile(r"^\d+[A-Za-zá-žÁ-Ž]?(/\d+[A-Za-zá-žÁ-Ž]?)?$")

def split_address(address):
    if not isinstance(address, str): return "", "", ""
    parts = [p.strip() for p in address.split(",", 1)]
    street_part = parts[0] if parts else ""
    suffix = parts[1] if len(parts) > 1 else ""
    tokens = street_part.split()
    number_tokens = []
    while tokens and _HOUSE_NUMBER_RE.match(tokens[-1]):
        number_tokens.insert(0, tokens.pop())
    street_name = " ".join(tokens)
    house_number = " ".join(number_tokens)
    declared = suffix
    for prefix in ("Košice -", "Košice –", "Kosice -", "Košice"):
        if declared.startswith(prefix):
            declared = declared[len(prefix):].lstrip(" -–")
            break
    return street_name.strip(), house_number.strip(), declared.strip()


_STREET_STOPWORDS = {"ulica","cesta","trieda","námestie","namestie","nábrežie",
                     "nabrezie","park","alej","sady","promenáda","promenada"}

def _street_tokens(name):
    return {t for t in name.lower().split() if t and t not in _STREET_STOPWORDS}

def _street_matches(query, registry_name):
    qt = _street_tokens(query); rt = _street_tokens(registry_name)
    if not qt or not rt: return False
    return bool(qt & rt)


def lookup_district(street_name, fallback=""):
    """Returns (district, confidence). Confidence ∈
       authoritative / authoritative_fuzzy / authoritative_disambiguated /
       authoritative_first / fallback / unknown."""
    idx = load_streets_index()
    canon_fb = _canonical_district(fallback) if fallback else ""
    if not street_name:
        return (canon_fb, "fallback" if fallback else "unknown")
    q = street_name.lower().strip()
    if q in idx:
        candidates = list({_canonical_district(c) for c in idx[q]})
        if len(candidates) == 1: return (candidates[0], "authoritative")
        if canon_fb and canon_fb in candidates:
            return (canon_fb, "authoritative_disambiguated")
        return (candidates[0], "authoritative_first")
    matched_districts = []
    for name, districts in idx.items():
        if _street_matches(q, name):
            for d in districts:
                cd = _canonical_district(d)
                if cd not in matched_districts: matched_districts.append(cd)
    if matched_districts:
        if len(matched_districts) == 1: return (matched_districts[0], "authoritative_fuzzy")
        if canon_fb and canon_fb in matched_districts:
            return (canon_fb, "authoritative_disambiguated")
        return (matched_districts[0], "authoritative_first")
    return (canon_fb, "fallback" if fallback else "unknown")


def normalise_address(address, declared_district=""):
    street, house, declared = split_address(address)
    eff = declared or declared_district
    district, confidence = lookup_district(street, eff)
    return {"street": street, "house_number": house,
            "declared_district": eff, "district": district, "confidence": confidence}


NON_KOSICE_CITIES = {
    "bratislava","žilina","zilina","nitra","trnava","trenčín","trencin",
    "banská bystrica","banska bystrica","prešov","presov","poprad","martin",
    "humenné","humenne","michalovce","spišská nová ves","spisska nova ves",
    "rimavská sobota","rimavska sobota","lučenec","lucenec","komárno","komarno",
    "topoľčany","topolcany","dunajská streda","dunajska streda","bardejov",
    "snina","vranov","stropkov","rožňava","roznava","levoča","levoca","kežmarok",
    "kezmarok","stará ľubovňa","stara lubovna","sabinov","moldava nad bodvou",
    "sečovce","secovce","brno","praha",
}


def is_kosice_address(address):
    """Strict: street must exist in the registry (exact OR fuzzy word-overlap).
    Just mentioning 'Košice' is not enough — fake streets like 'Galvaniho' fail."""
    if not isinstance(address, str) or not address.strip():
        return False, "Prázdna adresa"
    addr_low = address.lower()
    for city in NON_KOSICE_CITIES:
        if city in addr_low and re.search(rf"\b{re.escape(city)}\b", addr_low):
            return False, f"Adresa sa nachádza v meste {city.title()}"
    street, _, _ = split_address(address)
    if street:
        _, conf = lookup_district(street)
        if conf in ("authoritative","authoritative_fuzzy",
                    "authoritative_disambiguated","authoritative_first"):
            return True, "Ulica je v registri Košice"
        return False, f"Ulica '{street}' nie je v košickom registri ulíc"
    return False, "Adresu sa nepodarilo rozparsovať"


# ---- Loaders ----

def load_packages(path, use_nominatim=True):
    """Load + validate packages.csv. Adds: street, house_number, lat, lon,
    volume_m3, status, city_district (authoritative override),
    raw_address (original), district_confidence, kosice_check_reason."""
    df = pd.read_csv(path, encoding="utf-8-sig")
    df["volume_m3"] = df["dimensions_cm"].apply(parse_dimensions)
    df["fragile_bool"] = df["fragile"].astype(str).str.lower().eq("áno")
    df["priority"] = df["priority"].fillna("Štandard")

    streets, houses, auth_d, conf, in_k, reasons = [], [], [], [], [], []
    for _, row in df.iterrows():
        info = normalise_address(row.get("address",""), row.get("city_district",""))
        streets.append(info["street"]); houses.append(info["house_number"])
        auth_d.append(info["district"]); conf.append(info["confidence"])
        ok, reason = is_kosice_address(row.get("address",""))
        in_k.append(ok); reasons.append(reason)
    df["street"] = streets
    df["house_number"] = houses
    df["declared_city_district"] = df["city_district"]
    df["city_district"] = auth_d
    df["district_confidence"] = conf
    df["in_kosice"] = in_k
    df["kosice_check_reason"] = reasons

    # Rewrite the displayed address so the suffix matches the AUTHORITATIVE district
    df["raw_address"] = df["address"]
    def _rewrite(row):
        head = f"{row['street']} {row['house_number']}".strip()
        if row["in_kosice"] and row["city_district"]:
            return f"{head}, Košice - {row['city_district']}"
        return row["address"]
    df["address"] = df.apply(_rewrite, axis=1)

    # Geocoding (Nominatim only — no jitter)
    conn = _init_cache()
    geolocator = None
    if use_nominatim and GEOPY_AVAILABLE:
        geolocator = GoogleV3(api_key=os.environ.get("GOOGLE_MAPS_API_KEY"))

    lats, lons, suburbs, statuses = [], [], [], []
    for _, row in df.iterrows():
        if not row["in_kosice"]:
            lats.append(None); lons.append(None)
            suburbs.append(row.get("city_district","")); statuses.append("MIMO_KOSICE")
            continue
        r = geocode_address(conn, row["address"], row.get("city_district",""), geolocator=geolocator)
        if r["valid"]:
            lats.append(r["lat"]); lons.append(r["lon"])
            suburbs.append(row["city_district"])
            statuses.append(row.get("status","Čaká na doručenie"))
        else:
            lats.append(None); lons.append(None)
            suburbs.append(row.get("city_district",""))
            statuses.append("INVALID_ADDRESS")
    df["lat"] = lats; df["lon"] = lons
    df["city_district"] = suburbs; df["status"] = statuses
    conn.close()
    return df


def load_drivers(path):
    df = pd.read_csv(path, encoding="utf-8-sig")
    df["allowed_zones"] = df["zone_mestska_cast"].apply(lambda z: [z])
    return df


# ---- Helpers ----

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2-lat1); dlmb = math.radians(lon2-lon1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dlmb/2)**2
    return 2*R*math.asin(math.sqrt(a))


def travel_minutes(d_km):
    return (d_km / AVERAGE_SPEED_KMH) * 60.0


def shift_minutes(s, e):
    a = datetime.strptime(s,"%H:%M"); b = datetime.strptime(e,"%H:%M")
    return int((b-a).total_seconds()//60)


def priority_tier(priority, package_type=""):
    p = (priority or "").lower(); pt = (package_type or "").lower()
    if "overnight" in p or "meltable" in p or "potraviny" in pt or "krehk" in pt: return 0
    if "expres" in p: return 1
    return 2


def clock_from_start(shift_start, elapsed):
    base = datetime.strptime(shift_start, "%H:%M")
    return (base + timedelta(minutes=elapsed)).strftime("%H:%M")


# ---- VRP heuristic ----

def _candidate_stop_for(driver_row, candidates, current_lat, current_lon):
    if candidates.empty: return None
    df = candidates.copy()
    df["_dist"] = df.apply(lambda r: haversine_km(current_lat, current_lon, r["lat"], r["lon"]), axis=1)
    df["_tier"] = df.apply(lambda r: priority_tier(r["priority"], r.get("package_type","")), axis=1)
    df["_score"] = df["_tier"]*1000.0 + df["_dist"]
    df = df.sort_values("_score")
    return int(df.iloc[0]["id"])


def _build_route_for_driver(driver_row, packages):
    shift_start = driver_row["shift_start"]; shift_end = driver_row["shift_end"]
    total_shift = shift_minutes(shift_start, shift_end)
    max_w = float(driver_row["max_weight_kg"])
    max_v = float(driver_row["max_volume_m3"]) * VOLUME_USABLE_RATIO
    max_n = int(driver_row["max_packages_count"])
    route = Route(
        driver_id=driver_row["driver_id"],
        driver_name=f"{driver_row['first_name']} {driver_row['last_name']}",
        vehicle_id=driver_row["vehicle_id"],
        license_plate=driver_row["license_plate"],
        vehicle_make_model=driver_row["vehicle_make_model"],
        zone_mestska_cast=driver_row["zone_mestska_cast"],
        max_weight_kg=float(driver_row["max_weight_kg"]),
        max_volume_m3=float(driver_row["max_volume_m3"]),
        max_packages_count=int(driver_row["max_packages_count"]),
        shift_start=shift_start, shift_end=shift_end,
    )
    cur_lat, cur_lon = KOSICE_DEPOT_LAT, KOSICE_DEPOT_LON
    elapsed = 0.0; work_since_break = 0.0

    # STRICT zone matching
    primary_zone = driver_row["zone_mestska_cast"]
    candidates = packages[
        (packages["status"] == "Čaká na doručenie")
        & (packages["city_district"] == primary_zone)
        & (packages["lat"].notna())
    ].copy()

    rnd = random.Random(int(driver_row["driver_id"][3:]) if driver_row["driver_id"][3:].isdigit() else 42)

    while True:
        if candidates.empty: break
        cap_w = max_w - route.used_weight_kg
        cap_v = max_v - route.used_volume_m3
        cap_n = max_n - route.used_packages
        fit = candidates[(candidates["weight_kg"] <= cap_w) & (candidates["volume_m3"] <= cap_v)]
        if cap_n <= 0 or fit.empty:
            depot_back = travel_minutes(haversine_km(cur_lat, cur_lon, KOSICE_DEPOT_LAT, KOSICE_DEPOT_LON))
            extra = depot_back + RESTOCK_DURATION_MIN
            if elapsed + extra >= total_shift - 30: break
            if candidates.empty: break
            elapsed += extra; work_since_break += extra
            cur_lat, cur_lon = KOSICE_DEPOT_LAT, KOSICE_DEPOT_LON
            route.restocks.append({"at_min": elapsed, "clock": clock_from_start(shift_start, elapsed),
                                   "duration_min": RESTOCK_DURATION_MIN})
            route.used_weight_kg = 0.0; route.used_volume_m3 = 0.0
            route.used_packages = 0; route.trip_count += 1
            continue
        chosen_id = _candidate_stop_for(driver_row, fit, cur_lat, cur_lon)
        if chosen_id is None: break
        pkg = candidates[candidates["id"] == chosen_id].iloc[0]
        d_km = haversine_km(cur_lat, cur_lon, pkg["lat"], pkg["lon"])
        t_min = travel_minutes(d_km); svc = rnd.uniform(STOP_SERVICE_MIN, STOP_SERVICE_MAX)
        d_back = travel_minutes(haversine_km(pkg["lat"], pkg["lon"], KOSICE_DEPOT_LAT, KOSICE_DEPOT_LON))
        added = t_min + svc
        if work_since_break + added > WORK_BEFORE_BREAK_MIN:
            if elapsed + BREAK_DURATION_MIN + added + d_back > total_shift: break
            route.breaks.append({"at_min": elapsed, "clock": clock_from_start(shift_start, elapsed),
                                 "duration_min": BREAK_DURATION_MIN, "reason": "Povinná pauza (4h pravidlo)"})
            elapsed += BREAK_DURATION_MIN; work_since_break = 0.0
        if elapsed + added + d_back > total_shift: break
        elapsed += added; work_since_break += added
        route.total_distance_km += d_km
        cur_lat, cur_lon = pkg["lat"], pkg["lon"]
        route.used_weight_kg += float(pkg["weight_kg"])
        route.used_volume_m3 += float(pkg["volume_m3"])
        route.used_packages += 1
        stop = Stop(
            package_id=int(pkg["id"]), barcode=str(pkg["barcode"]),
            recipient_name=str(pkg["recipient_name"]), address=str(pkg["address"]),
            city_district=str(pkg["city_district"]),
            lat=float(pkg["lat"]), lon=float(pkg["lon"]),
            weight_kg=float(pkg["weight_kg"]), volume_m3=float(pkg["volume_m3"]),
            fragile=bool(pkg["fragile_bool"]), priority=str(pkg["priority"]),
            package_type=str(pkg.get("package_type","")),
            payment_method=str(pkg.get("payment_method","")),
            cod_amount_eur=(None if pd.isna(pkg.get("cod_amount_eur")) else float(pkg["cod_amount_eur"])),
            special_instructions=("" if pd.isna(pkg.get("special_instructions")) else str(pkg["special_instructions"])),
            eta_minutes_from_shift_start=elapsed,
            eta_clock=clock_from_start(shift_start, elapsed),
            service_minutes=svc,
        )
        route.stops.append(stop)
        candidates = candidates[candidates["id"] != chosen_id]
    if route.stops:
        elapsed += travel_minutes(haversine_km(cur_lat, cur_lon, KOSICE_DEPOT_LAT, KOSICE_DEPOT_LON))
    route.total_minutes = elapsed
    route.loading_order = list(reversed(route.stops))  # LIFO
    return route


def assign_packages(drivers, packages):
    routes = {}
    work = packages.copy()
    work["status"] = work["status"].where(work["status"].notna(), "Čaká na doručenie")
    drivers_sorted = drivers.sort_values(["max_weight_kg","max_volume_m3"], ascending=False)
    for _, drv in drivers_sorted.iterrows():
        r = _build_route_for_driver(drv, work)
        routes[drv["driver_id"]] = r
        if r.stops:
            ids = {s.package_id for s in r.stops}
            work = work[~work["id"].isin(ids)]
    return routes


def detect_cross_zone_requests(drivers, packages, routes):
    assigned_ids = {s.package_id for r in routes.values() for s in r.stops}
    unassigned = packages[(~packages["id"].isin(assigned_ids))
                          & (packages["status"] == "Čaká na doručenie")
                          & (packages["lat"].notna())]
    if unassigned.empty: return []
    out = []
    for _, drv in drivers.iterrows():
        route = routes.get(drv["driver_id"])
        if route is None: continue
        total = shift_minutes(drv["shift_start"], drv["shift_end"])
        if (total - route.total_minutes) < 90: continue
        free_w = route.max_weight_kg - route.used_weight_kg
        free_v = route.max_volume_m3 * VOLUME_USABLE_RATIO - route.used_volume_m3
        if free_w <= 1 or free_v <= 0.05: continue
        for nb in DISTRICT_ADJACENCY.get(drv["zone_mestska_cast"], []):
            slice_ = unassigned[unassigned["city_district"] == nb]
            if slice_.empty: continue
            chosen = []; w, v = 0.0, 0.0
            for _, p in slice_.sort_values("priority").iterrows():
                if w + p["weight_kg"] > free_w or v + p["volume_m3"] > free_v: continue
                chosen.append(int(p["id"])); w += float(p["weight_kg"]); v += float(p["volume_m3"])
                if len(chosen) >= 5: break
            if chosen:
                out.append(CrossZoneRequest(
                    request_id=f"CZ-{drv['driver_id']}-{nb.replace(' ','_')}",
                    driver_id=drv["driver_id"],
                    driver_name=f"{drv['first_name']} {drv['last_name']}",
                    home_zone=drv["zone_mestska_cast"], target_zone=nb,
                    package_ids=chosen, extra_packages=len(chosen),
                    extra_weight_kg=round(w,2), extra_volume_m3=round(v,3)))
                unassigned = unassigned[~unassigned["id"].isin(chosen)]
                break
    return out


def fleet_summary(drivers, packages, routes):
    total = int(len(packages))
    invalid = int((packages["status"] == "INVALID_ADDRESS").sum())
    mimo = int((packages["status"] == "MIMO_KOSICE").sum())
    assigned_ids = {s.package_id for r in routes.values() for s in r.stops}
    assigned = len(assigned_ids)
    unassigned = total - assigned - invalid - mimo
    cap_w = drivers["max_weight_kg"].sum()
    cap_v = drivers["max_volume_m3"].sum() * VOLUME_USABLE_RATIO
    used_w = sum(r.used_weight_kg for r in routes.values())
    used_v = sum(r.used_volume_m3 for r in routes.values())
    util = (used_w/cap_w + used_v/cap_v) / 2 * 100 if cap_w > 0 and cap_v > 0 else 0
    return {"total_packages": total, "assigned": assigned, "unassigned": unassigned,
            "invalid": invalid, "outside_kosice": mimo,
            "drivers_active": sum(1 for r in routes.values() if r.stops),
            "drivers_total": int(len(drivers)),
            "fleet_utilization_pct": round(float(util),1),
            "total_distance_km": round(sum(r.total_distance_km for r in routes.values()),1)}


def route_to_dict(route): return asdict(route)
def routes_to_dict(routes): return {k: route_to_dict(v) for k, v in routes.items()}


def recompute_etas(route, from_index=0, offset_minutes=0.0):
    elapsed = offset_minutes
    cur_lat, cur_lon = KOSICE_DEPOT_LAT, KOSICE_DEPOT_LON
    if from_index > 0 and route.stops:
        prev = route.stops[from_index-1]
        elapsed = max(elapsed, prev.eta_minutes_from_shift_start)
        cur_lat, cur_lon = prev.lat, prev.lon
    for i in range(from_index, len(route.stops)):
        s = route.stops[i]
        d_km = haversine_km(cur_lat, cur_lon, s.lat, s.lon)
        elapsed += travel_minutes(d_km) + s.service_minutes
        s.eta_minutes_from_shift_start = elapsed
        s.eta_clock = clock_from_start(route.shift_start, elapsed)
        cur_lat, cur_lon = s.lat, s.lon
    route.total_minutes = elapsed
    route.loading_order = list(reversed(route.stops))
    return route


def reorder_stops(route, new_order_ids):
    by_id = {s.package_id: s for s in route.stops}
    new_stops = [by_id[i] for i in new_order_ids if i in by_id]
    seen = {s.package_id for s in new_stops}
    for s in route.stops:
        if s.package_id not in seen: new_stops.append(s)
    route.stops = new_stops
    return recompute_etas(route, 0, 0.0)


__all__ = [
    "Stop","Route","CrossZoneRequest",
    "load_packages","load_drivers","assign_packages",
    "detect_cross_zone_requests","fleet_summary","routes_to_dict",
    "recompute_etas","reorder_stops","DISTRICT_ADJACENCY",
    "KOSICE_DEPOT_LAT","KOSICE_DEPOT_LON",
    "split_address","lookup_district","normalise_address",
    "load_streets_index","clock_from_start","shift_minutes",
    "is_kosice_address","NON_KOSICE_CITIES","DISTRICT_ALIASES",
    "geocode_address","_init_cache","parse_dimensions",
    "split_house_number",
]
