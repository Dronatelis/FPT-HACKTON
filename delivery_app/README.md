# KE-Delivery — Fleet Management & Routing pre Košice

## ⚡ Najrýchlejší preview

**Otvorte `demo.html` priamo v prehliadači** (Chrome / Edge / Firefox / Safari).
Žiadny Node.js, žiadny Python, žiadna inštalácia — celá aplikácia (Dispečing,
Živá prevádzka, Zákaznícky portál, Vodičská app, Administrácia) beží lokálne
v jednom HTML súbore. 301 zásielok, 58 vodičov, 956 ulíc.

## Komponenty

```
ke-delivery/
├── demo.html              ← standalone preview (dvojklik)
├── backend/
│   ├── optimizer.py        — VRP heuristika + adresový parser + validátor
│   ├── main.py             — FastAPI server + WebSocket
│   ├── warm_cache.py       — pre-warm Nominatim cache
│   └── requirements.txt
└── data/
    ├── packages.csv        — vstupné balíky
    ├── drivers.csv         — register vodičov + vozidiel
    └── streets.csv         — autoritatívny zoznam ulíc z `ulice kosice.xlsx`
```

## Spustenie backendu

```bash
cd backend
pip install -r requirements.txt

# 1) Najprv pre-warm geocoding cache cez Nominatim
#    (jednorázovo ~20 min pre 1050 adries; cache prežíva reštart)
python warm_cache.py

# 2) Štart FastAPI
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

API beží na `http://localhost:8000`. Endpointy:
- `/api/health`, `/api/summary`, `/api/routes`
- `/api/dispatch-queue` (PUT na opravu adresy)
- `/api/streets/lookup` (POST normalizácia)
- `/api/driver/login`, `/api/routes/{id}/location`, `/api/routes/{id}/stop-complete`
- `/api/track/{barcode}` (verejné sledovanie)
- `/ws` (WebSocket event bus)
- a ďalšie

## Architektúra adries

1. **Split** adresy čiarkou → `street` + `house_number` + `declared_district`
2. **Validate** street proti `streets.csv` (956 ulíc): exact + fuzzy word-overlap
3. **Override** `city_district` autoritatívnou hodnotou
4. **Geocode** výhradne cez Nominatim s `time.sleep(1.1)` + SQLite cache
5. **Assign** balíky vodičom STRICT: `package.city_district == driver.zone_mestska_cast`
6. **LIFO** loading order (posledná zastávka = prvá naložená)

Adresy mimo Košíc alebo s fabrikovaným názvom ulice **NEPÔJDU** vodičovi —
flagujú sa ako MIMO_KOSICE / INVALID_ADDRESS a čakajú v dispatcher queue.

## Pravidlo priraďovania

- Vodič dostáva **iba** balíky kde `package.city_district == driver.zone_mestska_cast`
- Multi-zone výnimky idú cez `CrossZoneRequest` (vyžaduje explicitné schválenie administrátora)
- Vehicle limity (hmotnosť / 85 % objemu / počet) sú vždy rešpektované
- Priority: Overnight > Expres > Štandard

## Slovak UI

Celé UI je výhradne v slovenčine podľa FPT Digital Brand Identity:
- Primary: `#F36F21` (FPT Orange)
- Secondary: `#101820` (Deep Navy)
- Background: `#F3F4F6`
