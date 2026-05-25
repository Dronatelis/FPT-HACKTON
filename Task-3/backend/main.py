"""KE-Delivery FastAPI server — REST API + WebSocket bus."""

from __future__ import annotations
import asyncio, io, json, os, time, uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from optimizer import (
    DISTRICT_ADJACENCY, KOSICE_DEPOT_LAT, KOSICE_DEPOT_LON,
    Route, Stop, assign_packages, clock_from_start, detect_cross_zone_requests,
    fleet_summary, geocode_address, load_drivers, load_packages,
    load_streets_index, lookup_district, normalise_address, parse_dimensions,
    recompute_etas, reorder_stops, routes_to_dict, split_address, _init_cache,
)

app = FastAPI(title="KE-Delivery API", version="1.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

DATA_DIR = Path(os.environ.get("KE_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
PACKAGES_PATH = DATA_DIR / "packages.csv"
DRIVERS_PATH = DATA_DIR / "drivers.csv"
DISPATCH_QUEUE_PATH = DATA_DIR / "dispatch_queue.json"


class AppState:
    def __init__(self):
        self.packages: pd.DataFrame | None = None
        self.drivers: pd.DataFrame | None = None
        self.routes: dict[str, Route] = {}
        self.cross_zone: dict[str, Any] = {}
        self.fuel_logs: list[dict] = []
        self.optimization_run_at: str | None = None
        self.driver_state: dict[str, dict] = {}
        self.package_events: dict[int, list[dict]] = {}
        self.event_feed: list[dict] = []
        self.driver_availability: dict[str, list[dict]] = {}
        self.vehicle_service: dict[str, dict] = {}

    def ensure_loaded(self):
        if self.packages is None and PACKAGES_PATH.exists():
            # Use Nominatim by default — relies on the SQLite cache being warm.
            self.packages = load_packages(str(PACKAGES_PATH), use_nominatim=True)
        if self.drivers is None and DRIVERS_PATH.exists():
            self.drivers = load_drivers(str(DRIVERS_PATH))

    def push_event(self, kind, payload):
        evt = {"kind": kind, "ts": datetime.utcnow().isoformat(), "payload": payload}
        self.event_feed.append(evt)
        if len(self.event_feed) > 500: self.event_feed = self.event_feed[-500:]
        return evt


STATE = AppState()


class EventBus:
    def __init__(self):
        self.clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
    async def connect(self, ws):
        await ws.accept()
        async with self._lock: self.clients.add(ws)
    async def disconnect(self, ws):
        async with self._lock: self.clients.discard(ws)
    async def broadcast(self, msg):
        dead = []
        for c in list(self.clients):
            try: await c.send_json(msg)
            except Exception: dead.append(c)
        for d in dead: await self.disconnect(d)


BUS = EventBus()
MAIN_LOOP: asyncio.AbstractEventLoop | None = None


def emit(kind, payload):
    evt = STATE.push_event(kind, payload)
    if MAIN_LOOP is None: return
    try: asyncio.run_coroutine_threadsafe(BUS.broadcast(evt), MAIN_LOOP)
    except RuntimeError: pass


# ---- Pydantic models ----

class AddressUpdate(BaseModel): address: str
class StreetLookupPayload(BaseModel): address: str
class ReorderPayload(BaseModel): package_ids: list[int]
class EventPayload(BaseModel):
    event: str
    package_id: int | None = None
class LocationPayload(BaseModel):
    lat: float; lon: float
    current_stop_index: int = 0
    status: str = "EN_ROUTE"
class StopCompletePayload(BaseModel):
    package_id: int
    actual_lat: float | None = None
    actual_lon: float | None = None
class LoginPayload(BaseModel):
    driver_id: str; pin: str
class FuelLogPayload(BaseModel):
    driver_id: str; vehicle_id: str
    liters: float; cost_eur: float
    minutes_spent: int = 15; notes: str = ""
class DriverPayload(BaseModel):
    driver_id: str | None = None
    first_name: str; last_name: str; phone: str
    vehicle_id: str; vehicle_make_model: str; vehicle_type: str
    license_plate: str
    max_weight_kg: float; max_volume_m3: float; max_packages_count: int
    zone_mestska_cast: str
    allowed_zones: list[str] = Field(default_factory=list)
    years_experience: int = 0
    shift_start: str = "07:00"; shift_end: str = "16:00"
    notes: str = ""
class VehiclePayload(BaseModel):
    vehicle_id: str | None = None
    license_plate: str; vehicle_make_model: str; vehicle_type: str
    max_weight_kg: float; max_volume_m3: float; max_packages_count: int
class DriverAvailabilityPayload(BaseModel):
    date_from: str; date_to: str
    reason: str = "dovolenka"; notes: str = ""
class VehicleServicePayload(BaseModel):
    stk_until: str | None = None
    ek_until: str | None = None
    insurance_until: str | None = None
    last_service_date: str | None = None
    last_service_km: int | None = None
    current_km: int | None = None
    tires_until: str | None = None
    notes: str = ""
class ServiceLogPayload(BaseModel):
    kind: str; date: str
    km: int | None = None
    cost_eur: float | None = None
    notes: str = ""
class DispatchResolvePayload(BaseModel): address: str


# ---- Lifecycle ----

@app.on_event("startup")
async def _startup():
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_running_loop()
    STATE.ensure_loaded()
    if STATE.packages is not None and STATE.drivers is not None and not STATE.routes:
        _run_optimization()


def _persist_dispatch_queue():
    if STATE.packages is None: return
    flagged = STATE.packages[STATE.packages["status"].isin(["MIMO_KOSICE", "INVALID_ADDRESS"])]
    rows = []
    for _, row in flagged.iterrows():
        rows.append({
            "id": int(row["id"]), "barcode": str(row["barcode"]),
            "recipient_name": str(row["recipient_name"]),
            "address": str(row["address"]),
            "street": str(row.get("street","")), "house_number": str(row.get("house_number","")),
            "declared_city_district": str(row.get("declared_city_district","")),
            "status": str(row["status"]),
            "reason": str(row.get("kosice_check_reason","")),
        })
    DISPATCH_QUEUE_PATH.write_text(
        json.dumps({"updated_at": datetime.utcnow().isoformat(), "items": rows},
                   ensure_ascii=False, indent=2), encoding="utf-8")


def _run_optimization():
    assert STATE.packages is not None and STATE.drivers is not None
    STATE.routes = assign_packages(STATE.drivers, STATE.packages)
    cz = detect_cross_zone_requests(STATE.drivers, STATE.packages, STATE.routes)
    STATE.cross_zone = {r.request_id: r for r in cz}
    STATE.optimization_run_at = datetime.utcnow().isoformat()
    try: _persist_dispatch_queue()
    except Exception: pass


# ---- Health + optimize + upload ----

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "depot": {"lat": KOSICE_DEPOT_LAT, "lon": KOSICE_DEPOT_LON},
        "packages_loaded": 0 if STATE.packages is None else int(len(STATE.packages)),
        "drivers_loaded": 0 if STATE.drivers is None else int(len(STATE.drivers)),
        "last_run": STATE.optimization_run_at,
        "ws_clients": len(BUS.clients),
    }


@app.post("/api/optimize")
def optimize():
    STATE.ensure_loaded()
    if STATE.packages is None or STATE.drivers is None:
        raise HTTPException(400, "Nahrajte najprv packages.csv a drivers.csv")
    _run_optimization()
    emit("optimization.completed", fleet_summary(STATE.drivers, STATE.packages, STATE.routes))
    return {"ok": True, "ran_at": STATE.optimization_run_at,
            **fleet_summary(STATE.drivers, STATE.packages, STATE.routes)}


@app.post("/api/upload")
async def upload(packages: UploadFile = File(...), drivers: UploadFile = File(...)):
    PACKAGES_PATH.write_bytes(await packages.read())
    DRIVERS_PATH.write_bytes(await drivers.read())
    STATE.packages = load_packages(str(PACKAGES_PATH), use_nominatim=True)
    STATE.drivers = load_drivers(str(DRIVERS_PATH))
    _run_optimization()
    return {"ok": True, **fleet_summary(STATE.drivers, STATE.packages, STATE.routes)}


@app.get("/api/summary")
def summary():
    STATE.ensure_loaded()
    if STATE.packages is None or STATE.drivers is None:
        return {"total_packages":0,"assigned":0,"unassigned":0,"invalid":0,
                "outside_kosice":0,"drivers_active":0,"drivers_total":0,
                "fleet_utilization_pct":0.0,"total_distance_km":0.0}
    return fleet_summary(STATE.drivers, STATE.packages, STATE.routes)


@app.get("/api/routes")
def list_routes(district: str | None = None):
    STATE.ensure_loaded()
    out = routes_to_dict(STATE.routes)
    if district:
        out = {k: r for k, r in out.items()
               if r["zone_mestska_cast"] == district
               or any(s["city_district"] == district for s in r["stops"])}
    return {"routes": out, "depot": {"lat": KOSICE_DEPOT_LAT, "lon": KOSICE_DEPOT_LON}}


@app.get("/api/routes/{driver_id}")
def get_route(driver_id: str):
    if driver_id not in STATE.routes:
        raise HTTPException(404, f"Vodič {driver_id} nemá pridelenú trasu")
    return asdict(STATE.routes[driver_id])


# ---- Drivers / Vehicles ----

@app.get("/api/drivers")
def list_drivers():
    STATE.ensure_loaded()
    if STATE.drivers is None: return {"drivers": []}
    df = STATE.drivers.copy()
    df["allowed_zones"] = df["allowed_zones"].apply(lambda z: z if isinstance(z, list) else [z])
    return {"drivers": df.to_dict(orient="records")}


@app.post("/api/drivers")
def create_driver(payload: DriverPayload):
    STATE.ensure_loaded()
    if STATE.drivers is None: STATE.drivers = pd.DataFrame()
    next_id = payload.driver_id or f"DRV{len(STATE.drivers)+1:03d}"
    row = payload.dict()
    row["driver_id"] = next_id
    row["allowed_zones"] = payload.allowed_zones or [payload.zone_mestska_cast]
    STATE.drivers = pd.concat([STATE.drivers, pd.DataFrame([row])], ignore_index=True)
    return {"ok": True, "driver_id": next_id}


@app.put("/api/drivers/{driver_id}")
def update_driver(driver_id: str, payload: DriverPayload):
    if STATE.drivers is None or driver_id not in STATE.drivers["driver_id"].values:
        raise HTTPException(404, "Vodič nenájdený")
    idx = STATE.drivers.index[STATE.drivers["driver_id"] == driver_id][0]
    for k, v in payload.dict().items():
        if v is None: continue
        STATE.drivers.at[idx, k] = v
    return {"ok": True}


@app.delete("/api/drivers/{driver_id}")
def delete_driver(driver_id: str):
    if STATE.drivers is None or driver_id not in STATE.drivers["driver_id"].values:
        raise HTTPException(404, "Vodič nenájdený")
    STATE.drivers = STATE.drivers[STATE.drivers["driver_id"] != driver_id].reset_index(drop=True)
    STATE.routes.pop(driver_id, None)
    return {"ok": True}


@app.get("/api/vehicles")
def list_vehicles():
    STATE.ensure_loaded()
    if STATE.drivers is None: return {"vehicles": []}
    cols = ["vehicle_id","license_plate","vehicle_make_model","vehicle_type",
            "max_weight_kg","max_volume_m3","max_packages_count","driver_id"]
    avail = [c for c in cols if c in STATE.drivers.columns]
    return {"vehicles": STATE.drivers[avail].to_dict(orient="records")}


# ---- Packages + dispatch queue ----

@app.get("/api/packages")
def list_packages(status: str | None = None, district: str | None = None, limit: int = 500):
    STATE.ensure_loaded()
    if STATE.packages is None: return {"packages": [], "total": 0}
    df = STATE.packages.copy()
    if status: df = df[df["status"] == status]
    if district: df = df[df["city_district"] == district]
    total = int(len(df))
    df = df.head(limit).where(pd.notnull(df.head(limit)), None)
    return {"packages": df.to_dict(orient="records"), "total": total}


@app.get("/api/dispatch-queue")
def list_dispatch_queue():
    STATE.ensure_loaded()
    if STATE.packages is None: return {"items": [], "total": 0}
    fl = STATE.packages[STATE.packages["status"].isin(["MIMO_KOSICE","INVALID_ADDRESS"])].copy()
    fl = fl.where(pd.notnull(fl), None)
    return {"items": fl[["id","barcode","recipient_name","address","street","house_number",
                         "declared_city_district","status","kosice_check_reason"]]
            .rename(columns={"kosice_check_reason":"reason"}).to_dict(orient="records"),
            "total": int(len(fl)),
            "persisted_to": str(DISPATCH_QUEUE_PATH)}


@app.put("/api/dispatch-queue/{pkg_id}")
def resolve_dispatch_item(pkg_id: int, payload: DispatchResolvePayload):
    from optimizer import is_kosice_address, normalise_address
    if STATE.packages is None or pkg_id not in STATE.packages["id"].values:
        raise HTTPException(404, "Balík nenájdený")
    idx = STATE.packages.index[STATE.packages["id"] == pkg_id][0]
    info = normalise_address(payload.address, STATE.packages.at[idx, "declared_city_district"])
    in_k, reason = is_kosice_address(payload.address)
    STATE.packages.at[idx, "address"] = payload.address
    STATE.packages.at[idx, "street"] = info["street"]
    STATE.packages.at[idx, "house_number"] = info["house_number"]
    STATE.packages.at[idx, "city_district"] = info["district"]
    STATE.packages.at[idx, "district_confidence"] = info["confidence"]
    STATE.packages.at[idx, "in_kosice"] = in_k
    STATE.packages.at[idx, "kosice_check_reason"] = reason
    if not in_k:
        STATE.packages.at[idx, "status"] = "MIMO_KOSICE"
        _persist_dispatch_queue()
        return {"ok": True, "valid": False, "reason": reason}
    conn = _init_cache()
    result = geocode_address(conn, payload.address, info["district"], geolocator=None)
    conn.close()
    if not result["valid"]:
        STATE.packages.at[idx, "status"] = "INVALID_ADDRESS"
        _persist_dispatch_queue()
        return {"ok": True, "valid": False, "reason": "Nepodarilo sa geokódovať"}
    STATE.packages.at[idx, "lat"] = result["lat"]
    STATE.packages.at[idx, "lon"] = result["lon"]
    STATE.packages.at[idx, "status"] = "Čaká na doručenie"
    _run_optimization()
    emit("dispatch.resolved", {"package_id": pkg_id, "new_district": info["district"]})
    return {"ok": True, "valid": True, "district": info["district"]}


# ---- Streets registry + lookup ----

@app.get("/api/streets")
def list_streets(q: str | None = None, district: str | None = None, limit: int = 200):
    idx = load_streets_index(); out = []
    for name, districts in idx.items():
        for d in districts:
            if q and q.lower() not in name.lower(): continue
            if district and d != district: continue
            out.append({"street_name": name, "city_district": d})
            if len(out) >= limit: break
        if len(out) >= limit: break
    return {"streets": out, "total": sum(len(v) for v in idx.values())}


@app.post("/api/streets/lookup")
def streets_lookup(payload: StreetLookupPayload):
    return normalise_address(payload.address)


# ---- Cross-zone ----

@app.get("/api/cross-zone-requests")
def list_cross_zone():
    return {"requests": [asdict(r) for r in STATE.cross_zone.values()]}


@app.post("/api/cross-zone-requests/{rid}/approve")
def approve_cz(rid: str):
    if rid not in STATE.cross_zone: raise HTTPException(404, "Požiadavka nenájdená")
    req = STATE.cross_zone[rid]; req.status = "APPROVED"
    route = STATE.routes.get(req.driver_id)
    if route is None or STATE.packages is None: raise HTTPException(400, "Stav nie je načítaný")
    for pid in req.package_ids:
        if pid not in STATE.packages["id"].values: continue
        pkg = STATE.packages[STATE.packages["id"] == pid].iloc[0]
        if pd.isna(pkg.get("lat")): continue
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
            service_minutes=2.5)
        route.stops.append(stop)
        route.used_weight_kg += stop.weight_kg
        route.used_volume_m3 += stop.volume_m3
        route.used_packages += 1
    recompute_etas(route)
    emit("crosszone.approved", {"request_id": rid, "driver_id": req.driver_id})
    return {"ok": True}


@app.post("/api/cross-zone-requests/{rid}/reject")
def reject_cz(rid: str):
    if rid not in STATE.cross_zone: raise HTTPException(404, "Požiadavka nenájdená")
    STATE.cross_zone[rid].status = "REJECTED"
    return {"ok": True}


# ---- Manual reorder + events ----

@app.post("/api/routes/{driver_id}/reorder")
def manual_reorder(driver_id: str, payload: ReorderPayload):
    if driver_id not in STATE.routes: raise HTTPException(404, "Vodič nemá trasu")
    reorder_stops(STATE.routes[driver_id], payload.package_ids)
    emit("route.reordered", {"driver_id": driver_id})
    return {"ok": True, "route": asdict(STATE.routes[driver_id])}


@app.post("/api/routes/{driver_id}/event")
def driver_event(driver_id: str, payload: EventPayload):
    if driver_id not in STATE.routes: raise HTTPException(404, "Vodič nemá trasu")
    route = STATE.routes[driver_id]; delta = 0.0
    if payload.event == "pauza":
        delta = 30.0
        route.breaks.append({"at_min": route.total_minutes, "duration_min": 30, "reason": "Manuálna pauza"})
        emit("driver.pauza", {"driver_id": driver_id, "driver_name": route.driver_name})
    elif payload.event == "tankovanie":
        delta = 15.0
        STATE.fuel_logs.append({"id": str(uuid.uuid4()), "driver_id": driver_id,
                                "vehicle_id": route.vehicle_id, "minutes_spent": 15,
                                "liters": 0.0, "cost_eur": 0.0,
                                "notes": "Vodič nahlásil tankovanie",
                                "timestamp": datetime.utcnow().isoformat()})
        emit("driver.tankovanie", {"driver_id": driver_id, "driver_name": route.driver_name})
    elif payload.event == "skip":
        if payload.package_id is None: raise HTTPException(400, "Pre 'skip' chýba package_id")
        before = len(route.stops)
        skipped = next((s for s in route.stops if s.package_id == payload.package_id), None)
        route.stops = [s for s in route.stops if s.package_id != payload.package_id]
        if len(route.stops) == before: raise HTTPException(404, "Zastávka nenájdená")
        recompute_etas(route, 0, 0.0)
        if skipped is not None:
            STATE.package_events.setdefault(skipped.package_id, []).append(
                {"kind": "skipped", "ts": datetime.utcnow().isoformat(),
                 "reason": "Zákazník nezastihnuteľný"})
            emit("package.skipped", {"package_id": skipped.package_id,
                                     "barcode": skipped.barcode, "driver_id": driver_id,
                                     "driver_name": route.driver_name,
                                     "recipient_name": skipped.recipient_name})
        return {"ok": True, "route": asdict(route)}
    else:
        raise HTTPException(400, f"Neznáma udalosť: {payload.event}")
    for s in route.stops:
        s.eta_minutes_from_shift_start += delta
        s.eta_clock = clock_from_start(route.shift_start, s.eta_minutes_from_shift_start)
    route.total_minutes += delta
    return {"ok": True, "route": asdict(route)}


# ---- Driver location push + stop complete ----

@app.post("/api/routes/{driver_id}/location")
def push_location(driver_id: str, payload: LocationPayload):
    if driver_id not in STATE.routes: raise HTTPException(404, "Vodič nemá trasu")
    route = STATE.routes[driver_id]
    state = {"lat": payload.lat, "lon": payload.lon, "ts": datetime.utcnow().isoformat(),
             "current_stop_index": payload.current_stop_index, "status": payload.status,
             "driver_name": route.driver_name, "license_plate": route.license_plate,
             "zone_mestska_cast": route.zone_mestska_cast}
    STATE.driver_state[driver_id] = state
    emit("driver.location", {"driver_id": driver_id, **state})
    return {"ok": True}


@app.post("/api/routes/{driver_id}/stop-complete")
def stop_complete(driver_id: str, payload: StopCompletePayload):
    if driver_id not in STATE.routes: raise HTTPException(404, "Vodič nemá trasu")
    route = STATE.routes[driver_id]
    stop = next((s for s in route.stops if s.package_id == payload.package_id), None)
    if stop is None: raise HTTPException(404, "Zastávka nenájdená")
    STATE.package_events.setdefault(stop.package_id, []).append(
        {"kind": "delivered", "ts": datetime.utcnow().isoformat(),
         "actual_lat": payload.actual_lat, "actual_lon": payload.actual_lon})
    emit("package.delivered", {"package_id": stop.package_id, "barcode": stop.barcode,
                               "driver_id": driver_id, "driver_name": route.driver_name,
                               "recipient_name": stop.recipient_name, "address": stop.address})
    return {"ok": True}


@app.get("/api/driver-state")
def list_driver_state():
    return {"drivers": STATE.driver_state, "events": STATE.event_feed[-50:]}


# ---- Customer tracking ----

@app.get("/api/track/{barcode}")
def track_package(barcode: str):
    STATE.ensure_loaded()
    if STATE.packages is None: raise HTTPException(404, "Zásielka nenájdená")
    df = STATE.packages[STATE.packages["barcode"].str.upper() == barcode.upper()]
    if df.empty: raise HTTPException(404, "Zásielka nenájdená")
    row = df.iloc[0]
    pkg = {
        "id": int(row["id"]), "barcode": str(row["barcode"]),
        "recipient_name": str(row["recipient_name"]),
        "address": str(row["address"]), "city_district": str(row["city_district"]),
        "package_type": str(row.get("package_type","")), "priority": str(row.get("priority","")),
        "status": str(row["status"]),
        "lat": (None if pd.isna(row.get("lat")) else float(row["lat"])),
        "lon": (None if pd.isna(row.get("lon")) else float(row["lon"])),
        "order_date": str(row.get("order_date","")),
    }
    driver_info = None; eta_clock = None
    for did, route in STATE.routes.items():
        for idx, s in enumerate(route.stops):
            if s.package_id == pkg["id"]:
                driver_info = {"driver_id": did, "driver_name": route.driver_name,
                               "license_plate": route.license_plate,
                               "vehicle_make_model": route.vehicle_make_model,
                               "shift_start": route.shift_start, "shift_end": route.shift_end,
                               "total_stops": len(route.stops), "stop_index": idx+1}
                eta_clock = s.eta_clock; break
        if driver_info is not None: break
    history = STATE.package_events.get(pkg["id"], [])
    timeline = [{"label": "Prijatá objednávka", "ts": pkg["order_date"], "state": "done"}]
    if driver_info is not None:
        timeline.append({"label": "Pripravená vo sklade", "ts": "", "state": "done"})
        live = STATE.driver_state.get(driver_info["driver_id"])
        delivered = next((h for h in history if h["kind"] == "delivered"), None)
        skipped = next((h for h in history if h["kind"] == "skipped"), None)
        if delivered:
            timeline.append({"label": "Vyzdvihnuté na rozvoz", "ts": "", "state": "done"})
            timeline.append({"label": "Doručené príjemcovi", "ts": delivered["ts"], "state": "done"})
            pkg["status"] = "Doručené"
        elif skipped:
            timeline.append({"label": "Vyzdvihnuté na rozvoz", "ts": "", "state": "done"})
            timeline.append({"label": "Zákazník nezastihnuteľný — opätovný pokus", "ts": skipped["ts"], "state": "warn"})
            pkg["status"] = "Vrátené do depa"
        elif live and live.get("status") in ("EN_ROUTE","AT_STOP"):
            timeline.append({"label": "Vyzdvihnuté na rozvoz", "ts": live["ts"], "state": "done"})
            timeline.append({"label": "Na ceste k príjemcovi", "ts": "", "state": "active"})
            pkg["status"] = "Na ceste"
        else:
            timeline.append({"label": "Naložené v aute, čaká na štart trasy", "ts": "", "state": "active"})
            pkg["status"] = "V triedení"
    return {"package": pkg, "driver": driver_info, "eta_clock": eta_clock,
            "live": STATE.driver_state.get(driver_info["driver_id"]) if driver_info else None,
            "timeline": timeline,
            "depot": {"lat": KOSICE_DEPOT_LAT, "lon": KOSICE_DEPOT_LON},
            "history": history}


# ---- Fuel, traffic, districts, login ----

@app.get("/api/fuel-logs")
def list_fuel(): return {"logs": STATE.fuel_logs}


@app.post("/api/fuel-logs")
def add_fuel(payload: FuelLogPayload):
    entry = payload.dict(); entry["id"] = str(uuid.uuid4())
    entry["timestamp"] = datetime.utcnow().isoformat()
    STATE.fuel_logs.append(entry)
    return {"ok": True, "log": entry}


@app.get("/api/traffic-alerts")
def traffic():
    return {"alerts": [
        {"level": "warn", "message": "Zdržanie 15 min na Triede SNP — stavebné práce."},
        {"level": "info", "message": "Most VSS uzavretý jedným pruhom."},
    ]}


@app.get("/api/districts")
def districts():
    return {"districts": sorted(DISTRICT_ADJACENCY.keys()), "adjacency": DISTRICT_ADJACENCY}


@app.post("/api/driver/login")
def driver_login(payload: LoginPayload):
    STATE.ensure_loaded()
    if STATE.drivers is None: raise HTTPException(400, "Roster nie je načítaný")
    df = STATE.drivers[STATE.drivers["driver_id"] == payload.driver_id]
    if df.empty: raise HTTPException(401, "Vodič nebol nájdený")
    phone_digits = "".join(ch for ch in str(df.iloc[0]["phone"]) if ch.isdigit())[-4:]
    if payload.pin != phone_digits:
        raise HTTPException(401, "Nesprávny PIN (posledné 4 čísla telefónu)")
    return {"ok": True, "driver_id": payload.driver_id,
            "driver_name": f"{df.iloc[0]['first_name']} {df.iloc[0]['last_name']}"}


# ---- Driver availability + vehicle service ----

@app.get("/api/drivers/{driver_id}/availability")
def get_avail(driver_id: str):
    return {"availability": STATE.driver_availability.get(driver_id, [])}


@app.post("/api/drivers/{driver_id}/availability")
def add_avail(driver_id: str, payload: DriverAvailabilityPayload):
    if STATE.drivers is None or driver_id not in STATE.drivers["driver_id"].values:
        raise HTTPException(404, "Vodič nenájdený")
    rec = payload.dict(); rec["id"] = str(uuid.uuid4())
    rec["created_at"] = datetime.utcnow().isoformat()
    STATE.driver_availability.setdefault(driver_id, []).append(rec)
    return {"ok": True, "record": rec}


def _days_until(date_str):
    if not date_str: return None
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        return (d - datetime.utcnow().date()).days
    except (ValueError, TypeError): return None


def _service_status(days):
    if days is None: return "neznámy"
    if days < 0: return "po termíne"
    if days <= 14: return "kritické"
    if days <= 45: return "blížiace sa"
    return "ok"


@app.get("/api/vehicles/service-overview")
def service_overview():
    STATE.ensure_loaded()
    if STATE.drivers is None: return {"vehicles": []}
    out = []
    for _, drv in STATE.drivers.iterrows():
        vid = drv.get("vehicle_id")
        if not vid: continue
        rec = STATE.vehicle_service.get(vid, {})
        veh = {"vehicle_id": vid, "license_plate": drv["license_plate"],
               "vehicle_make_model": drv["vehicle_make_model"],
               "vehicle_type": drv["vehicle_type"], "driver_id": drv["driver_id"],
               "driver_name": f"{drv['first_name']} {drv['last_name']}", **rec}
        for k in ("stk_until","ek_until","insurance_until","tires_until"):
            d = _days_until(rec.get(k))
            veh[f"{k}_days"] = d
            veh[f"{k}_status"] = _service_status(d)
        out.append(veh)
    return {"vehicles": out}


@app.put("/api/vehicles/{vehicle_id}/service")
def update_service(vehicle_id: str, payload: VehicleServicePayload):
    rec = STATE.vehicle_service.setdefault(vehicle_id, {"logs": []})
    for k, v in payload.dict().items():
        if v is None: continue
        rec[k] = v
    rec["updated_at"] = datetime.utcnow().isoformat()
    return {"ok": True, "service": rec}


# ---- WebSocket ----

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await BUS.connect(ws)
    try:
        try:
            await ws.send_json({"kind": "hello", "ts": datetime.utcnow().isoformat(),
                                "payload": {"recent": STATE.event_feed[-50:]}})
        except Exception: pass
        while True: await ws.receive_text()
    except WebSocketDisconnect: await BUS.disconnect(ws)
    except Exception: await BUS.disconnect(ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
