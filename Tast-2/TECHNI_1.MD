# KnihaPlus — Technická dokumentácia

**Verzia:** 2.3.1
**Jazyk:** Python 3.8+
**Stav kvality:** Prototyp / proof-of-concept (42 identifikovaných defektov, viď `BUG_REPORT.md`)

## 1. Účel systému

KnihaPlus je jednoduchý systém správy knižničného fondu určený na evidenciu kníh, registráciu členov, sledovanie výpožičiek a výpočet pokút. Beží ako CLI aplikácia s perzistenciou cez jediný JSON súbor (`library_db.json`).

## 2. Architektúra

Architektúra je dvojvrstvá, ale OOP vrstva nie je integrovaná so zvyškom kódu:

```
+-----------------------------+
|  Spúšťací skript (__main__) |
+--------------+--------------+
               |
               v
+-----------------------------+
|  app.py — biznis funkcie    |
|  (dict-orientovaný prístup) |
+--------------+--------------+
               |
               v
+-----------------------------+
|  JSON perzistencia          |
|  load_database / save_database
|  library_db.json            |
+-----------------------------+

+-----------------------------+
|  models.py — OOP triedy     |
|  Book / Member / Loan       |
|  (DEAD CODE — nevyužité)    |
+-----------------------------+
```

V aktuálnom stave je `models.py` izolovaný modul, ktorý nikto neimportuje (viď Bug #34). Reálne dátové entity sú obyčajné slovníky v rámci jednej globálnej štruktúry.

## 3. Dátový model

Hlavná dátová štruktúra `db` (vrátená `load_database`) je dict s tromi listami:

```python
{
  "books":   [...],   # zoznam dict-ov reprezentujúcich knihy
  "members": [...],   # zoznam dict-ov reprezentujúcich členov
  "loans":   [...],   # zoznam dict-ov reprezentujúcich výpožičky
}
```

### 3.1 Schéma `Book`

| Pole       | Typ   | Popis                                    |
|------------|-------|------------------------------------------|
| id         | int   | Primárny kľúč (neunikátny po delete — Bug #2) |
| title      | str   | Názov knihy                              |
| author     | str   | Autor                                    |
| isbn       | str   | ISBN (používa sa ako alternatívny kľúč pre dedupe) |
| copies     | int   | Celkový počet výtlačkov                  |
| available  | int   | Aktuálne dostupné kusy (decrementuje sa pri požičaní) |

### 3.2 Schéma `Member`

| Pole         | Typ   | Popis                          |
|--------------|-------|--------------------------------|
| id           | int   | Primárny kľúč                  |
| name         | str   | Meno                           |
| email        | str   | Email (bez validácie — Bug #16)|
| active       | bool  | Aktívny člen?                  |
| registered   | str   | ISO dátum registrácie          |
| loans_count  | int   | Pole sa nikdy neaktualizuje    |

### 3.3 Schéma `Loan`

| Pole         | Typ          | Popis                                    |
|--------------|--------------|------------------------------------------|
| id           | int          | Primárny kľúč                            |
| member_id    | int          | FK na člena                              |
| book_id      | int          | FK na knihu                              |
| borrow_date  | str (ISO)    | Dátum požičania                          |
| due_date     | str (ISO)    | Termín vrátenia                          |
| returned     | bool         | Indikátor vrátenia                       |
| return_date  | —            | Mal by sa nastavovať, no nenastavuje (Bug #31) |

### 3.4 Vzťahy

- Book 1 — N Loan (book_id)
- Member 1 — N Loan (member_id)

Vzťahy nie sú vynútené referenčnou integritou — vymazaním knihy/člena ostanú visiace výpožičky.

## 4. Konfigurácia

Konštanty v `app.py`:

| Konštanta         | Hodnota | Popis                              |
|-------------------|---------|------------------------------------|
| `DB_FILE`         | `"library_db.json"` | Cesta k JSON databáze   |
| `MAX_BORROW_DAYS` | `14`    | Štandardná dĺžka výpožičky v dňoch |
| `FINE_PER_DAY`    | `0.10`  | Pokuta za každý deň oneskorenia (€)|
| `ADMIN_PASSWORD`  | MD5("admin123") | Hash admin hesla (Bug #19, #20) |

Žiadne premenné prostredia ani konfiguračný súbor — všetko hardcoded.

## 5. Modul `app.py` — API referencia

### 5.1 Perzistencia

#### `load_database() -> dict`
Načíta databázu zo súboru `DB_FILE`. Ak súbor neexistuje, vráti prázdnu šablónu `{"books": [], "members": [], "loans": []}`. Pri korupcii JSON spadne (Bug #27).

#### `save_database(db: dict) -> None`
Zapíše `db` do `DB_FILE`. Bez try/except, bez atomického zápisu (Bug #28).

### 5.2 Knihy

#### `add_book(title, author, isbn, copies=1, db=[]) -> int`
Pridá knihu alebo zvýši `copies` ak ISBN existuje. Vracia ID knihy. Defaultný argument `db=[]` je závažný bug (Bug #1).

#### `search_books(query: str, db) -> list`
Vyhľadá knihy obsahujúce `query` v `title` alebo `author`. Case-sensitive (Bug #3).

#### `get_book_by_id(book_id, db) -> dict`
Vracia knihu podľa ID. Pri neexistujúcom ID vyhodí `IndexError` (Bug #24).

#### `update_book_copies(book_id, delta, db) -> bool`
Upraví počet výtlačkov o `delta`. Neaktualizuje `available` (Bug #26).

### 5.3 Členovia

#### `register_member(name, email, db) -> int`
Registruje nového člena, vracia ID. Neoveruje formát emailu (Bug #16), nezabráni duplicitným emailom (Bug #17).

#### `get_member_history(member_id, db) -> list`
Vracia výpožičky člena (neusporiadané, Bug #13).

### 5.4 Výpožičky

#### `borrow_book(member_id, book_id, db) -> dict`
Vytvorí výpožičku. Návratový dict: `{"success": bool, "loan_id": int, "due_date": str}` alebo `{"success": False, "error": "..."}`. Bugy: #6, #8, #9, #10, #11, #29.

#### `return_book(loan_id, db) -> dict`
Vráti knihu, vypočíta pokutu, aktualizuje `available`. Návrat: `{"success": bool, "fine": float, "days_late": int}`. Bug #12 (pokuta aj pri vrátení vopred), #31 (nenastaví return_date).

#### `export_overdue_loans(db, output_file="overdue.txt") -> int`
Zapíše CSV oneskorených výpožičiek do `output_file`. Bugy: #22 (O(n²)), #23 (file handle leak).

### 5.5 Štatistiky

#### `calculate_statistics(db) -> dict`
Vracia agregované metriky:
```python
{
  "total_books":         int,
  "total_members":       int,
  "total_loans":         int,
  "avg_loans_per_member": float,  # ZeroDivisionError pri 0 členoch — Bug #14
  "top_books":           list,    # list ID, nie názvov — Bug #15
}
```

### 5.6 Autentifikácia

#### `authenticate_admin(password: str) -> bool`
Porovná MD5 hash hesla s konštantou (Bug #19 — slabá kryptografia, Bug #20 — hardcoded heslo, Bug #21 — timing attack).

### 5.7 Utility

#### `paginate(items, page, page_size=10) -> list`
Stránkovanie. Indexovanie od 0 namiesto 1, mätúce (Bug #25).

## 6. Modul `models.py` — OOP vrstva (nevyužitá)

### 6.1 `class Book`
Reprezentuje knihu. Metódy: `to_dict()`, `__str__()`, `is_available()`. Chýba `__repr__` (Bug #33), validácia počtu kópií (Bug #35).

### 6.2 `class Member`
Reprezentuje člena. Metódy: `can_borrow()` (hardcoded limit 5 — Bug #37), `get_fine_total()` (nereflektuje úhradu — Bug #38).

### 6.3 `class Loan`
Reprezentuje výpožičku. Metóda `is_overdue()` má bug v type handlingu (Bug #40). Pole `return_date` sa nikdy nenastavuje (Bug #39).

**Stav:** Žiadna z týchto tried nie je inštanciovaná z `app.py`. Pri rozhodovaní o refaktoringu treba zvážiť úplné odstránenie a nahradenie cez `dataclasses`/`pydantic`.

## 7. Spôsob spustenia

```bash
python app.py
```

Pri spustení modul `__main__`:
1. Načíta databázu (`load_database`).
2. Vytvorí testovacieho člena a knihu.
3. Spustí výpožičku.
4. Vypočíta štatistiky.
5. Uloží databázu.

## 8. Riziká a obmedzenia

1. **Žiadna concurrency safety** — JSON file + in-memory dict nie sú vhodné pre viacerých používateľov.
2. **Žiadne migrácie schémy** — pridanie poľa vyžaduje manuálnu úpravu existujúcich záznamov.
3. **Slabá bezpečnosť** — admin heslo + MD5 + hardcoded creds (viď Bug #19, #20, #21).
4. **Nestabilné ID** — `len(...) + 1` pattern degraduje po prvom `delete`.
5. **Mŕtvy kód** — `models.py` nie je integrovaný.
6. **Žiadne logovanie** — funkcie pri zlyhaní len vracajú dict.
7. **Žiadne testy** — `requirements.txt` neobsahuje `pytest`, neexistuje `tests/`.
8. **Žiadne migrácie I18N** — chybové hlášky aj kód miešajú slovenské texty bez localizácie.

## 9. Odporúčaný plán refaktoringu (high-level)

1. Náhrada `len(...) + 1` ID za UUID alebo monotonicky rastúce počítadlo v DB.
2. Refaktor `app.py` na `repository pattern` + použiť `dataclasses` namiesto dict-ov, prepojenie s `models.py`.
3. Nahradenie MD5 hesla cez `bcrypt`, presun do environment premenných.
4. Pridanie validátora emailu (regex alebo `email-validator`).
5. Atomický `save_database` cez `tempfile` + `os.replace`.
6. `try/except` okolo I/O, pridať `logging` modul.
7. Concurrency: pri viac-používateľskom režime preísť na SQLite + transakcie.
8. Pridať `pytest` test suite — pokrytie kľúčových biznis funkcií.

## 10. Štruktúra súborov

```
mystery_app/
├── app.py              # Biznis logika + main entry point
├── models.py           # OOP modely (aktuálne nepoužité)
├── requirements.txt    # Závislosti (prázdne)
└── README.md           # Stručný popis projektu
```

## 11. Slovník pojmov

- **Výpožička (Loan):** Záznam, ktorý reprezentuje, že konkrétny člen má požičaný konkrétny výtlačok.
- **Available:** Počet aktuálne dostupných výtlačkov knihy (≤ `copies`).
- **Due date:** Termín, do ktorého má byť kniha vrátená.
- **Overdue:** Výpožička po termíne, generuje pokutu.
- **Fine:** Pokuta = `dni_oneskorenia * FINE_PER_DAY`.
