# KnihaPlus v2.3.1 — Bug Report

**Projekt:** KnihaPlus (Systém správy knižničného fondu)
**Analyzované súbory:** `app.py`, `models.py`
**Dátum analýzy:** 2026-05-21
**Celkový počet bugov:** 42

---

## Súhrn podľa závažnosti

| Severity   | Počet |
|------------|-------|
| Critical   | 6     |
| High       | 10    |
| Medium     | 14    |
| Low        | 12    |

## Súhrn podľa typu (vrátane kombinovaných)

| Typ (primárny)     | Počet |
|--------------------|-------|
| Logic              | 21    |
| Python Antipattern | 7     |
| Security           | 5     |
| Error Handling     | 5     |
| Null Pointer       | 5     |
| Data Integrity     | 4     |
| Performance        | 2     |

---

## Bug #1: Mutable default argument
**Súbor:** app.py
**Riadok:** 30
**Severity:** Critical
**Typ:** Python Antipattern
**Popis:** Funkcia `add_book(..., db=[])` má mutable default argument. Default `[]` je vytvorený raz pri definícii funkcie a zdieľaný medzi všetkými volaniami. Navyše je to prázdny `list`, ale funkcia ho používa ako `dict` (`db["books"]`), takže pri volaní bez argumentu dôjde k `TypeError: list indices must be integers`.
**Reprodukcia:** `add_book("Title", "Author", "ISBN")` bez parametra `db` -> TypeError.
**Navrhovaná oprava:**
```python
def add_book(title, author, isbn, copies=1, db=None):
    if db is None:
        raise ValueError("db is required")
    ...
```

---

## Bug #2: Neunikátne ID kníh
**Súbor:** app.py
**Riadok:** 38
**Severity:** High
**Typ:** Logic / Data Integrity
**Popis:** `new_id = len(db["books"]) + 1`. Ak sa kniha vymaže, nasledujúce pridanie vygeneruje duplicitné ID a poškodí referencie vo výpožičkách.
**Reprodukcia:** Pridať 3 knihy (id 1,2,3), zmazať knihu č. 2, pridať novú -> nové ID = 3 (duplicita).
**Navrhovaná oprava:**
```python
import uuid
new_id = str(uuid.uuid4())
# alebo:
new_id = max((b["id"] for b in db["books"]), default=0) + 1
```

---

## Bug #3: Case-sensitive vyhľadávanie
**Súbor:** app.py
**Riadok:** 56
**Severity:** Medium
**Typ:** Logic
**Popis:** `if query in book["title"] or query in book["author"]` rozlišuje veľké/malé písmená. Hľadanie "hašek" nenájde "Hašek".
**Reprodukcia:** Pridať knihu autora "Jaroslav Hašek" a hľadať "hašek" -> 0 výsledkov.
**Navrhovaná oprava:**
```python
q = (query or "").lower()
for book in db["books"]:
    if q in book["title"].lower() or q in book["author"].lower():
        results.append(book)
```

---

## Bug #4: Chýbajúca kontrola typu/None pri vyhľadávacom dotaze
**Súbor:** app.py
**Riadok:** 56
**Severity:** Low
**Typ:** Null Pointer / Error Handling
**Popis:** Ak je `query` `None` alebo nie je string, `query in book["title"]` vyhodí `TypeError`.
**Reprodukcia:** `search_books(None, db)` -> TypeError.
**Navrhovaná oprava:**
```python
if not isinstance(query, str) or not query.strip():
    return []
```

---

## Bug #5: Chýbajúci `break` pri hľadaní člena
**Súbor:** app.py
**Riadok:** 64-66
**Severity:** Low
**Typ:** Performance
**Popis:** Cyklus `for m in db["members"]` neukončí iteráciu po nájdení člena, prechádza celý zoznam aj keď je výsledok známy.
**Reprodukcia:** Pri 10 000 členoch sa zbytočne prejde celý list.
**Navrhovaná oprava:**
```python
member = next((m for m in db["members"] if m["id"] == member_id), None)
```

---

## Bug #6: Chýbajúca None kontrola člena (AttributeError/TypeError)
**Súbor:** app.py
**Riadok:** 68-69
**Severity:** Critical
**Typ:** Null Pointer
**Popis:** Ak člen s daným ID neexistuje, `member` ostane `None` a `member["active"]` vyhodí `TypeError: 'NoneType' object is not subscriptable`.
**Reprodukcia:** `borrow_book(999, 1, db)` keď člen 999 neexistuje -> TypeError.
**Navrhovaná oprava:**
```python
if member is None:
    return {"success": False, "error": "Člen nenájdený"}
if not member["active"]:
    return {"success": False, "error": "Člen nie je aktívny"}
```

---

## Bug #7: Antipattern porovnanie s `== False`
**Súbor:** app.py
**Riadok:** 69
**Severity:** Low
**Typ:** Python Antipattern
**Popis:** `if member["active"] == False:` je v rozpore s PEP 8. Správna forma je `if not member["active"]:`.
**Reprodukcia:** Code review/linter (pyflakes, pylint E712) vyhlási chybu.
**Navrhovaná oprava:**
```python
if not member["active"]:
    return {"success": False, "error": "Člen nie je aktívny"}
```

---

## Bug #8: Race condition pri rezervácii výtlačku
**Súbor:** app.py
**Riadok:** 82-85
**Severity:** High
**Typ:** Logic / Concurrency
**Popis:** Kontrola `if book["available"] <= 0` a následný dekrement `book["available"] -= 1` nie sú atomické. Pri súbežných volaniach môžu dvaja členovia "získať" posledný kus.
**Reprodukcia:** Dva súbežné `borrow_book` volania pri `available == 1` -> `available` skončí na `-1`.
**Navrhovaná oprava:**
```python
import threading
db_lock = threading.Lock()

with db_lock:
    if book["available"] <= 0:
        return {"success": False, "error": "Žiadny dostupný výtlačok"}
    book["available"] -= 1
```

---

## Bug #9: `timedelta(MAX_BORROW_DAYS)` bez kľúčového slova
**Súbor:** app.py
**Riadok:** 89
**Severity:** Low
**Typ:** Python Antipattern / Readability
**Popis:** `datetime.timedelta(14)` je technicky správne (prvý argument je `days`), ale je nečitateľné a fragilné — ak by sa `MAX_BORROW_DAYS` premenil na sekundy alebo float, výsledok by bol iný.
**Reprodukcia:** Refaktorácia konštanty rozbije logiku, lebo pozičný argument prijíma akúkoľvek jednotku.
**Navrhovaná oprava:**
```python
due_date = today + datetime.timedelta(days=MAX_BORROW_DAYS)
```

---

## Bug #10: Neunikátne ID výpožičky
**Súbor:** app.py
**Riadok:** 92
**Severity:** High
**Typ:** Logic / Data Integrity
**Popis:** `len(db["loans"]) + 1` — rovnaký problém ako pri knihách. Pri odstránení starých výpožičiek vzniknú duplicity.
**Reprodukcia:** Vytvoriť 2 výpožičky (1,2), zmazať č. 1, vytvoriť novú -> nová má ID 2 (duplicita).
**Navrhovaná oprava:**
```python
new_loan_id = max((l["id"] for l in db["loans"]), default=0) + 1
```

---

## Bug #11: `borrow_book` nezavolá `save_database`
**Súbor:** app.py
**Riadok:** 99-100
**Severity:** High
**Typ:** Data Integrity
**Popis:** Funkcia upraví in-memory dict (zníži `available`, pridá výpožičku), ale nezapíše do súboru. Pri páde aplikácie sa stratia zmeny.
**Reprodukcia:** Po `borrow_book` reštartovať proces — výpožička v databáze nie je.
**Navrhovaná oprava:**
```python
db["loans"].append(loan)
save_database(db)
return {"success": True, ...}
```

---

## Bug #12: Pokuta `abs(days_late)` aj pri vrátení vopred
**Súbor:** app.py
**Riadok:** 116
**Severity:** Critical
**Typ:** Logic
**Popis:** `fine = abs(days_late) * FINE_PER_DAY` — ak člen vráti knihu skôr, `days_late` je záporné, `abs()` urobí kladné a strhne pokutu. Člen platí za včasné vrátenie.
**Reprodukcia:** Vrátiť knihu deň pred `due_date` -> `days_late = -1`, fine = 0.10 € (nesprávne).
**Navrhovaná oprava:**
```python
days_late = max(0, (return_date - due_date).days)
fine = days_late * FINE_PER_DAY
```

---

## Bug #13: `get_member_history` nezoradené podľa dátumu
**Súbor:** app.py
**Riadok:** 131-136
**Severity:** Low
**Typ:** Logic
**Popis:** História sa vracia v poradí vloženia do `db["loans"]`, nie chronologicky. Užívateľ očakáva chronologické zoradenie.
**Reprodukcia:** Manuálne pridať výpožičky v zmiešanom poradí dátumov -> história nie je zoradená.
**Navrhovaná oprava:**
```python
history.sort(key=lambda l: l["borrow_date"], reverse=True)
return history
```

---

## Bug #14: Delenie nulou pri `calculate_statistics`
**Súbor:** app.py
**Riadok:** 145
**Severity:** Critical
**Typ:** Logic / Null Pointer
**Popis:** `avg_loans = len(db["loans"]) / total_members` — ak `total_members == 0`, vyhodí `ZeroDivisionError`.
**Reprodukcia:** Volať `calculate_statistics(db)` na čerstvo inicializovanej DB -> ZeroDivisionError.
**Navrhovaná oprava:**
```python
avg_loans = len(db["loans"]) / total_members if total_members else 0
```

---

## Bug #15: `top_books` vracia ID namiesto názvov
**Súbor:** app.py
**Riadok:** 156
**Severity:** Medium
**Typ:** Logic / UX
**Popis:** `sorted(most_borrowed, key=...)` na dict vracia list kľúčov (book_id), ale rozhranie/štatistika je pre používateľa — mal by vrátiť názvy alebo `(title, count)`.
**Reprodukcia:** `stats["top_books"]` vracia `[1, 5, 3]` namiesto pochopiteľných údajov.
**Navrhovaná oprava:**
```python
top = sorted(most_borrowed.items(), key=lambda x: x[1], reverse=True)[:5]
id_to_title = {b["id"]: b["title"] for b in db["books"]}
top_books = [{"title": id_to_title.get(bid, "?"), "count": cnt} for bid, cnt in top]
```

---

## Bug #16: Chýba validácia formátu emailu
**Súbor:** app.py
**Riadok:** 167-172
**Severity:** Medium
**Typ:** Security / Logic
**Popis:** `register_member` prijme čokoľvek ako email. Skripty môžu uložiť XSS payload, neplatné adresy, prázdny string.
**Reprodukcia:** `register_member("Ján", "not-an-email", db)` -> uloží sa.
**Navrhovaná oprava:**
```python
import re
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
if not EMAIL_RE.match(email or ""):
    return {"success": False, "error": "Neplatný email"}
```

---

## Bug #17: Duplicitný email povolený (`pass`)
**Súbor:** app.py
**Riadok:** 171-173
**Severity:** High
**Typ:** Logic / Data Integrity
**Popis:** Cyklus detekuje existujúci email, ale namiesto návratu chyby volá `pass`. Vzniknú viacerí členovia s rovnakým emailom.
**Reprodukcia:** Dvakrát zavolať `register_member("X", "a@b.sk", db)` -> dvaja členovia, rovnaký email.
**Navrhovaná oprava:**
```python
for m in db["members"]:
    if m["email"].lower() == email.lower():
        return {"success": False, "error": "Email už existuje"}
```

---

## Bug #18: Neunikátne ID člena
**Súbor:** app.py
**Riadok:** 176
**Severity:** High
**Typ:** Logic / Data Integrity
**Popis:** Rovnaký problém ako pri knihách a výpožičkách — `len(db["members"]) + 1` po zmazaní spôsobí duplicity.
**Reprodukcia:** Registrovať 3 členov, vymazať 2., registrovať ďalšieho -> ID kolízia.
**Navrhovaná oprava:**
```python
new_id = max((m["id"] for m in db["members"]), default=0) + 1
```

---

## Bug #19: MD5 hash pre heslo
**Súbor:** app.py
**Riadok:** 15, 190
**Severity:** Critical
**Typ:** Security
**Popis:** MD5 je kryptograficky zlomený. Pre heslá sa nepoužíva ani SHA-256 — patrí sa použiť `bcrypt`, `argon2` alebo `scrypt` s saltom.
**Reprodukcia:** MD5(`admin123`) = `0192023a7bbd73250516f069df18b500` — okamžite v rainbow tabuľkách.
**Navrhovaná oprava:**
```python
import bcrypt
ADMIN_PASSWORD_HASH = bcrypt.hashpw(b"admin123", bcrypt.gensalt())

def authenticate_admin(password):
    return bcrypt.checkpw(password.encode(), ADMIN_PASSWORD_HASH)
```

---

## Bug #20: Hardcoded heslo "admin123" v zdrojovom kóde
**Súbor:** app.py
**Riadok:** 15
**Severity:** Critical
**Typ:** Security
**Popis:** Heslo je viditeľné v repozitári (Git history, OSS skenery, leaks). Heslá patria do environment premenných alebo secret managera.
**Reprodukcia:** `grep -i password app.py` odhalí heslo.
**Navrhovaná oprava:**
```python
import os
ADMIN_PASSWORD_HASH = os.environ.get("KNIHAPLUS_ADMIN_HASH")
if not ADMIN_PASSWORD_HASH:
    raise RuntimeError("KNIHAPLUS_ADMIN_HASH not configured")
```

---

## Bug #21: Timing attack v autentifikácii
**Súbor:** app.py
**Riadok:** 191
**Severity:** Medium
**Typ:** Security
**Popis:** `==` na hashoch beží v čase úmernom dĺžke zhody prefixu. Útočník vie odvodiť hash znak po znaku.
**Reprodukcia:** Mikro-benchmark porovnaní s rôznymi predponami.
**Navrhovaná oprava:**
```python
import hmac
return hmac.compare_digest(hashed, ADMIN_PASSWORD)
```

---

## Bug #22: O(n²) v `export_overdue_loans`
**Súbor:** app.py
**Riadok:** 199-208
**Severity:** Medium
**Typ:** Performance
**Popis:** Pre každú oneskorenú výpožičku sa lineárne hľadá člen — pri N výpožičkách a M členoch je to O(N·M).
**Reprodukcia:** 10 000 výpožičiek × 10 000 členov -> 100 000 000 porovnaní.
**Navrhovaná oprava:**
```python
members_by_id = {m["id"]: m for m in db["members"]}
for loan in db["loans"]:
    member = members_by_id.get(loan["member_id"])
    ...
```

---

## Bug #23: `open()` bez kontextového manažéra a `try/except`
**Súbor:** app.py
**Riadok:** 218-221
**Severity:** Medium
**Typ:** Error Handling
**Popis:** Ak `f.write` vyhodí výnimku, súbor sa nikdy nezavrie (resource leak). Tiež chýba `try/except` pre IOError/PermissionError.
**Reprodukcia:** Spustiť na read-only filesystéme alebo bez práv -> únik file descriptora.
**Navrhovaná oprava:**
```python
try:
    with open(output_file, "w", encoding="utf-8") as f:
        for item in overdue:
            f.write(f"{item['loan_id']},{item['member']},{item['fine']}\n")
except OSError as e:
    logger.error("Export failed: %s", e)
    raise
```

---

## Bug #24: `get_book_by_id` IndexError keď kniha neexistuje
**Súbor:** app.py
**Riadok:** 228
**Severity:** High
**Typ:** Null Pointer / Error Handling
**Popis:** `[b for b in db["books"] if b["id"] == book_id][0]` — list comprehension generuje prázdny list ak kniha neexistuje, indexácia `[0]` vyhodí `IndexError`.
**Reprodukcia:** `get_book_by_id(999, db)` -> IndexError.
**Navrhovaná oprava:**
```python
def get_book_by_id(book_id, db):
    return next((b for b in db["books"] if b["id"] == book_id), None)
```

---

## Bug #25: Off-by-one v stránkovaní
**Súbor:** app.py
**Riadok:** 231-236
**Severity:** Medium
**Typ:** Logic
**Popis:** Pre `page=1` vráti `items[10:20]` namiesto `items[0:10]`. Konvencia 1-indexovaných stránok porušená; `page=0` vracia prvú stránku, čo je mätúce.
**Reprodukcia:** `paginate([0..29], 1)` -> `[10..19]` namiesto `[0..9]`.
**Navrhovaná oprava:**
```python
def paginate(items, page, page_size=10):
    if page < 1:
        page = 1
    start = (page - 1) * page_size
    return items[start:start + page_size]
```

---

## Bug #26: `update_book_copies` neaktualizuje `available`
**Súbor:** app.py
**Riadok:** 239-246
**Severity:** High
**Typ:** Logic / Data Integrity
**Popis:** Pri zvýšení/znížení `copies` sa nemení `available`. Knižničník pridá 5 výtlačkov, ale `available` ostane rovnaké — nové kópie sa nedajú požičať.
**Reprodukcia:** `update_book_copies(1, +5, db)` -> `copies=8`, `available=3` (stratených 5 nových výtlačkov).
**Navrhovaná oprava:**
```python
for book in db["books"]:
    if book["id"] == book_id:
        new_copies = book["copies"] + delta
        if new_copies < (book["copies"] - book["available"]):
            return False  # nemožno znížiť pod aktuálne požičané
        book["copies"] = new_copies
        book["available"] = max(0, book["available"] + delta)
        return True
```

---

## Bug #27: `load_database` bez `try/except`
**Súbor:** app.py
**Riadok:** 18-22
**Severity:** Medium
**Typ:** Error Handling
**Popis:** Ak je `library_db.json` korupný (neplatný JSON), `json.load` vyhodí `JSONDecodeError` a aplikácia spadne pri štarte bez možnosti recoveru.
**Reprodukcia:** Pridať náhodný znak do `library_db.json` -> aplikácia neštartuje.
**Navrhovaná oprava:**
```python
def load_database():
    if not os.path.exists(DB_FILE):
        return {"books": [], "members": [], "loans": []}
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("DB load failed: %s; using empty DB", e)
        return {"books": [], "members": [], "loans": []}
```

---

## Bug #28: `save_database` nie je atomické
**Súbor:** app.py
**Riadok:** 25-27
**Severity:** High
**Typ:** Data Integrity / Error Handling
**Popis:** Ak proces padne počas `json.dump`, súbor zostane čiastočne zapísaný a databáza sa stratí. Treba write-to-temp + rename.
**Reprodukcia:** `kill -9` v strede zápisu -> poškodený `library_db.json`.
**Navrhovaná oprava:**
```python
import tempfile, os
def save_database(db):
    dir_ = os.path.dirname(os.path.abspath(DB_FILE)) or "."
    with tempfile.NamedTemporaryFile("w", delete=False, dir=dir_, encoding="utf-8") as tmp:
        json.dump(db, tmp, ensure_ascii=False, indent=2)
        tmp_path = tmp.name
    os.replace(tmp_path, DB_FILE)
```

---

## Bug #29: Chýba kontrola maximálneho počtu výpožičiek v `borrow_book`
**Súbor:** app.py
**Riadok:** 61-100
**Severity:** Medium
**Typ:** Logic
**Popis:** Model `Member.can_borrow()` definuje limit 5, ale `borrow_book` ho nikdy nevolá ani nekontroluje. Člen môže mať neobmedzene veľa výpožičiek.
**Reprodukcia:** Cyklicky volať `borrow_book` -> žiadny strop.
**Navrhovaná oprava:**
```python
active_loans = sum(1 for l in db["loans"]
                   if l["member_id"] == member_id and not l["returned"])
if active_loans >= MAX_ACTIVE_LOANS:
    return {"success": False, "error": "Prekročený limit výpožičiek"}
```

---

## Bug #30: `available` môže klesnúť do záporu (chýba dolná hranica)
**Súbor:** app.py
**Riadok:** 85
**Severity:** Medium
**Typ:** Data Integrity
**Popis:** Aj po kontrole `available <= 0` (Bug #8 race condition) nie je žiadna spodná hranica. V kombinácii s race condition / nesynchronizovaným prístupom môže ísť `available` pod 0.
**Reprodukcia:** Súbežné požičanie pri `available == 1` (viď Bug #8).
**Navrhovaná oprava:**
```python
if book["available"] <= 0:
    return {"success": False, "error": "Žiadny dostupný výtlačok"}
book["available"] = max(0, book["available"] - 1)
```

---

## Bug #31: Vrátenie už vrátenej výpožičky vracia chybu, ale podmienka by mala mať aj kontrolu existencie
**Súbor:** app.py
**Riadok:** 103-126
**Severity:** Low
**Typ:** Error Handling
**Popis:** Pri vrátení knihy sa neukladá `return_date` (len `returned=True`), takže neskôr nie je možné určiť, kedy bola kniha vrátená. Komentár k `models.Loan` to potvrdzuje.
**Reprodukcia:** Pozrieť `loan` po `return_book` — chýba `return_date`.
**Navrhovaná oprava:**
```python
loan["returned"] = True
loan["return_date"] = str(return_date)
```

---

## Bug #32: `models.py` — `id` shadowing builtin
**Súbor:** models.py
**Riadok:** 6, 33, 55
**Severity:** Low
**Typ:** Python Antipattern
**Popis:** Parameter `id` prekrýva vstavanú funkciu `id()`. PEP 8 / linter (`pylint W0622`).
**Reprodukcia:** Linter hlásenie.
**Navrhovaná oprava:**
```python
def __init__(self, book_id, title, author, isbn, copies):
    self.id = book_id
```

---

## Bug #33: `Book` — chýba `__repr__`
**Súbor:** models.py
**Riadok:** 24-26
**Severity:** Low
**Typ:** Python Antipattern
**Popis:** Iba `__str__`. Pri debuggingu `print(books)` vypíše `[<Book object at 0x...>, ...]`.
**Reprodukcia:** `repr([Book(...)])` -> nečitateľný výstup.
**Navrhovaná oprava:**
```python
def __repr__(self):
    return f"Book(id={self.id!r}, title={self.title!r}, author={self.author!r})"
```

---

## Bug #34: `Book.is_available()` mŕtvy kód (nikdy nevolaný)
**Súbor:** models.py
**Riadok:** 28-29
**Severity:** Low
**Typ:** Logic / Maintainability
**Popis:** `borrow_book` v `app.py` pracuje so slovníkmi, OOP modely v `models.py` sa nepoužívajú. `is_available()` ani ostatné metódy nikdy nebežia.
**Reprodukcia:** Hľadať použitie `Book(`, `Member(`, `Loan(` v `app.py` -> 0 výskytov.
**Navrhovaná oprava:** Refaktorovať `app.py` na použitie tried, alebo odstrániť `models.py` ak je redundantný.

---

## Bug #35: `Book` — neexistuje validácia `copies >= 0`
**Súbor:** models.py
**Riadok:** 11-12
**Severity:** Medium
**Typ:** Logic / Data Integrity
**Popis:** `Book(copies=-5)` sa konštruktorom akceptuje. Žiadna invariant kontrola.
**Reprodukcia:** `Book(1, "T", "A", "ISBN", -5)` -> vytvorené.
**Navrhovaná oprava:**
```python
if copies < 0:
    raise ValueError("copies must be >= 0")
self.copies = copies
self.available = copies
```

---

## Bug #36: `Member` — chýba validácia emailu
**Súbor:** models.py
**Riadok:** 36
**Severity:** Medium
**Typ:** Security / Logic
**Popis:** Žiadna regex/sanitizácia emailu pri inštanciácii.
**Reprodukcia:** `Member(1, "X", "")` -> akceptované.
**Navrhovaná oprava:**
```python
import re
if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email or ""):
    raise ValueError("Invalid email")
```

---

## Bug #37: `Member.can_borrow` — hardcoded limit
**Súbor:** models.py
**Riadok:** 41-43
**Severity:** Low
**Typ:** Python Antipattern / Maintainability
**Popis:** Magic number `5` nie je konštanta ani konfigurácia.
**Reprodukcia:** Zmena limitu vyžaduje úpravu kódu.
**Navrhovaná oprava:**
```python
MAX_ACTIVE_LOANS = 5  # alebo z konfigurácie
def can_borrow(self):
    active = [l for l in self.loans if not l.get("returned", False)]
    return len(active) < MAX_ACTIVE_LOANS
```

---

## Bug #38: `Member.get_fine_total` sčíta aj zaplatené pokuty
**Súbor:** models.py
**Riadok:** 45-51
**Severity:** Medium
**Typ:** Logic
**Popis:** Nesleduje sa stav úhrady (`paid` flag). Vráti súčet všetkých pokút, aj tých, ktoré už člen zaplatil.
**Reprodukcia:** Zaplatiť pokutu -> `get_fine_total` ju stále počíta.
**Navrhovaná oprava:**
```python
for loan in self.loans:
    if "fine" in loan and not loan.get("fine_paid", False):
        total += loan["fine"]
```

---

## Bug #39: `Loan.return_date` sa nikdy nenastaví
**Súbor:** models.py
**Riadok:** 62
**Severity:** Medium
**Typ:** Logic / Data Integrity
**Popis:** Trieda `Loan` má `self.return_date = None`, ale neexistuje žiadna metóda `mark_returned()`. Vrátenie knihy v `app.py` to taktiež neukladá (viď Bug #31).
**Reprodukcia:** Po `return_book` zostáva `return_date` `None`.
**Navrhovaná oprava:**
```python
def mark_returned(self, on_date=None):
    self.returned = True
    self.return_date = on_date or datetime.date.today()
```

---

## Bug #40: `Loan.is_overdue` porovnáva `date` so `str`
**Súbor:** models.py
**Riadok:** 64-68
**Severity:** High
**Typ:** Logic / Type Mismatch
**Popis:** `due_date` je v konštruktore prijímaný ako-je (môže byť `str`). `today > self.due_date` vyhodí `TypeError: '>' not supported between instances of 'datetime.date' and 'str'` ak je `due_date` string.
**Reprodukcia:** `Loan(1, 1, 1, "2026-01-01", "2026-01-15").is_overdue()` -> TypeError.
**Navrhovaná oprava:**
```python
def __init__(self, id, member_id, book_id, borrow_date, due_date):
    ...
    self.due_date = due_date if isinstance(due_date, datetime.date) \
                              else datetime.date.fromisoformat(due_date)
```

---

## Bug #41: `models.py` — `import datetime` až vnútri metódy
**Súbor:** models.py
**Riadok:** 65
**Severity:** Low
**Typ:** Python Antipattern
**Popis:** `import datetime` je vnútri `is_overdue` namiesto na vrchu modulu. Drobný performance penalty + nečitateľné.
**Reprodukcia:** Lint (`PEP8 E402` na inom importe vyzerá podobne).
**Navrhovaná oprava:**
```python
# na vrchu súboru:
import datetime
```

---

## Bug #42: `requirements.txt` — žiadne reálne závislosti, ale kód MD5/JSON funguje len so štandardnou knižnicou
**Súbor:** requirements.txt
**Riadok:** —
**Severity:** Low
**Typ:** Data Integrity / Dokumentácia
**Popis:** Súbor je v podstate prázdny. Po aplikovaní fixov (bcrypt, regex email validátor cez `email-validator`, prípadne `pytest`) bude treba pripojiť skutočné dependencies. Aktuálne neexistuje žiadna garancia voči nasadeniu.
**Reprodukcia:** `pip install -r requirements.txt` neurobí nič.
**Navrhovaná oprava:**
```
# requirements.txt
bcrypt>=4.0
email-validator>=2.0
# dev:
pytest>=7.4
pytest-cov>=4.1
```

---

## Záver

Z 42 zistených bugov je 8 kritických (najvážnejšie: hardcoded heslo + MD5 hash, mutable default argument s nesprávnym typom, race condition na výtlačkoch, delenie nulou v štatistikách, abs() pri výpočte pokuty, žiadna None kontrola v `borrow_book`, vrátenie `IndexError` v `get_book_by_id`).

Najsystémovejší problém: **`models.py` je úplne nevyužitý** (Bug #34). Aplikácia pracuje výlučne so slovníkmi v `app.py` a OOP modely sú duplicitný/mŕtvy kód — odporúčam refaktoring na repository pattern + dataclasses.
