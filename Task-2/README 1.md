# KnihaPlus

**Systém správy knižničného fondu** — jednoduchá CLI aplikácia v Pythone na evidenciu kníh, registráciu členov a sledovanie výpožičiek.

> **Status:** Prototyp / Proof of Concept (v2.3.1).
> Pre známe defekty si pozrite [`BUG_REPORT.md`](BUG_REPORT.md) — 42 identifikovaných bugov, neodporúčame nasadenie do produkcie bez opráv.

## Obsah

1. [Funkcie](#funkcie)
2. [Požiadavky](#požiadavky)
3. [Inštalácia](#inštalácia)
4. [Spustenie](#spustenie)
5. [Príklad použitia](#príklad-použitia)
6. [Štruktúra projektu](#štruktúra-projektu)
7. [Konfigurácia](#konfigurácia)
8. [Dokumentácia](#dokumentácia)
9. [Vývoj a prispievanie](#vývoj-a-prispievanie)
10. [Bezpečnostné upozornenia](#bezpečnostné-upozornenia)
11. [Licencia](#licencia)

## Funkcie

- Správa katalógu kníh (pridanie, vyhľadanie, počty výtlačkov).
- Registrácia členov knižnice.
- Vytváranie a vracanie výpožičiek vrátane výpočtu pokút za oneskorenie.
- Export oneskorených výpožičiek do CSV.
- Štatistický prehľad (počet kníh, členov, výpožičiek, najvypožičanejšie tituly).
- Jednoduchá admin autentifikácia (vyžaduje vylepšenie — viď Bezpečnostné upozornenia).

## Požiadavky

- **Python 3.8** alebo novší
- Operačný systém: Windows, macOS, Linux (akýkoľvek systém s podporou Pythonu)
- Žiadne externé knižnice pre základný beh — používa iba štandardnú knižnicu (`json`, `datetime`, `hashlib`, `os`).

Pre odporúčané opravy bezpečnosti budú potrebné dodatočné závislosti — viď [Vývoj](#vývoj-a-prispievanie).

## Inštalácia

```bash
git clone <repo-url> knihaplus
cd knihaplus
python -m venv .venv
source .venv/bin/activate    # Linux/macOS
# .venv\Scripts\activate     # Windows
pip install -r requirements.txt
```

Pre čerstvé prostredie sa databáza `library_db.json` vytvorí automaticky pri prvom uložení.

## Spustenie

```bash
python app.py
```

Štandardné spustenie inicializuje testovací scenár (zaregistruje člena, pridá knihu, vytvorí výpožičku, vypíše štatistiky a uloží stav). Pre použitie ako modul z vlastného skriptu:

```python
from app import (
    load_database, save_database,
    add_book, register_member,
    borrow_book, return_book,
    calculate_statistics,
)

db = load_database()
member_id = register_member("Ján Novák", "jan.novak@example.com", db)
book_id = add_book("Osudy dobrého vojaka Švejka", "Jaroslav Hašek", "978-80-7049-123-4", 3, db)
result = borrow_book(member_id, book_id, db)
print(result)
save_database(db)
```

## Príklad použitia

```python
# Pridanie viacerých kópií tej istej knihy podľa ISBN
add_book("1984", "George Orwell", "978-0-452-28423-4", copies=5, db=db)

# Vyhľadanie kníh
results = search_books("Orwell", db)

# Vrátenie knihy s výpočtom pokuty
outcome = return_book(loan_id=1, db=db)
# {"success": True, "fine": 0.30, "days_late": 3}

# Export oneskorených výpožičiek do CSV
overdue_count = export_overdue_loans(db, output_file="overdue.csv")
```

## Štruktúra projektu

```
mystery_app/
├── app.py              # Biznis logika + spúšťací skript
├── models.py           # OOP modely (aktuálne nevyužité — kandidát na refaktor)
├── requirements.txt    # Python závislosti
├── library_db.json     # Perzistentný úložný súbor (autogenerovaný)
└── README.md           # Tento súbor
```

## Konfigurácia

Konštanty v hornej časti `app.py`:

| Konštanta         | Default                | Popis                                    |
|-------------------|------------------------|------------------------------------------|
| `DB_FILE`         | `"library_db.json"`    | Cesta k databáze                         |
| `MAX_BORROW_DAYS` | `14`                   | Štandardná dĺžka výpožičky               |
| `FINE_PER_DAY`    | `0.10`                 | Pokuta za deň oneskorenia (€)            |
| `ADMIN_PASSWORD`  | MD5(`"admin123"`)      | Hash admin hesla (**zmeňte pred použitím**) |

Pre nasadenie sa odporúča presunúť `ADMIN_PASSWORD` do environment premennej a používať bezpečnejší hash (`bcrypt`, `argon2`).

## Dokumentácia

| Dokument                            | Účel                                                       |
|-------------------------------------|------------------------------------------------------------|
| `README.md`                         | Tento prehľad                                              |
| `TECHNICAL_DOCUMENTATION.md`        | Technický popis architektúry, dátového modelu a API        |
| `BUG_REPORT.md`                     | Identifikované defekty s návrhmi opráv                     |

## Vývoj a prispievanie

### Odporúčané dev závislosti

Po vyriešení bezpečnostných odporúčaní v `BUG_REPORT.md`:

```
bcrypt>=4.0
email-validator>=2.0
pytest>=7.4
pytest-cov>=4.1
ruff>=0.4
```

### Testovanie

V aktuálnej verzii neexistuje test suite. Plánujeme `pytest` s pokrytím kľúčových biznis funkcií:

```bash
pytest tests/ -v --cov=app --cov=models
```

### Štýl kódu

Cieľová zhoda s PEP 8 (`ruff check .`). Pred PR spustite linter:

```bash
ruff check app.py models.py
```

### Workflow

1. Forkujte repozitár a vytvorte feature branch.
2. Implementujte zmenu, doplňte testy.
3. Skontrolujte, že linter prejde bez chýb.
4. Otvorte pull request s popisom zmeny a referenciou na bug číslo (ak existuje).

## Bezpečnostné upozornenia

Aktuálna verzia obsahuje viacero známych bezpečnostných problémov dokumentovaných v `BUG_REPORT.md`. Najvážnejšie:

- **Hardcoded admin heslo** v zdrojovom kóde (Bug #20).
- **Slabý MD5 hash** namiesto `bcrypt`/`argon2` (Bug #19).
- **Timing-attack** v porovnaní hesla (Bug #21).
- **Chýbajúca validácia emailov** (Bug #16).
- **Race conditions** v zápise výpožičiek (Bug #8).

**Neodporúčame nasadenie do produkcie** bez aplikovania opráv. Pre interný/demo režim postačí, ale databáza nesmie obsahovať reálne osobné údaje.

## Známe obmedzenia

- Jeden JSON súbor → nevhodné pre súbežných používateľov.
- Žiadne migrácie schémy → manuálny zásah pri zmene polí.
- Žiadne logovanie ani audit trail.
- `models.py` nie je integrovaný do `app.py`.

## Licencia

Internal use only. Pre verejné použitie konzultujte s majiteľom projektu.

## Kontakt

Otázky a hlásenia chýb adresujte na: `library-team@example.com` (placeholder).
