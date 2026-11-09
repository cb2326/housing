#!/usr/bin/env python3
"""Builds the data for the Armenia map (site/armenia/data/).

Kept apart from the US build on purpose: if anything here fails, the US map is not affected, and the Armenia map
keeps showing the last data that passed its checks.

Sources (all free and official):
  - State Committee of the Real Estate Cadastre (cadastre.am): monthly market reports (PDF, Armenian). Apartment
    prices per m2 by Yerevan district and regional town, and transaction counts. Read with the pypdf library.
  - Statistical Committee of Armenia (statbank.armstat.am, PxWeb API): poverty, wages, housing space, new housing
    and population by marz; apartments by Yerevan district; inflation.
  - Central Bank of Armenia (cba.am): mortgage loans outstanding, policy rate, AMD/USD exchange rate.

Saved results live in armenia/cache/ (small JSON files kept in the repository), so each PDF is read only once.
Downloads go to .cache/armenia/ (not kept). Anything that needs attention is added to notices.txt, which the GitHub
workflow turns into an email.

Run:  python armenia/build_armenia.py            (normal run)
      python armenia/build_armenia.py --max-reports 200   (read more old reports in one go)
"""
import argparse, datetime as dt, html, io, json, os, re, shutil, sys, time, unicodedata, urllib.error, urllib.parse
import urllib.request, zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent            # armenia/
REPO = ROOT.parent
STATIC = ROOT / "static"
CACHE = ROOT / "cache"
DL = Path(os.environ.get("ARMENIA_DOWNLOADS") or REPO / ".cache" / "armenia")   # PDFs are deleted once read
OUT = REPO / "site" / "armenia" / "data"
NOTICES = REPO / "notices.txt"
PARSER_VERSION = 4
UA = "OpenHousingAtlas/1.0 (+https://openhousingatlas.com; free public housing data map)"
FIRST_MONTH = "2019-01"   # oldest Cadastre monthly report the build tries to read

notices = []
def notice(msg):
    print("NOTICE:", msg)
    notices.append(msg)

# ---------------------------------------------------------------- downloads
def fetch(url, data=None, headers=None, timeout=120, tries=3):
    h = {"User-Agent": UA}
    h.update(headers or {})
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 404) or attempt == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == tries - 1:
                raise
        time.sleep(5 * (attempt + 1))

def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default

def save_json(path, obj, pretty=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(obj, ensure_ascii=False, indent=1 if pretty else None, separators=None if pretty else (",", ":"))
    path.write_text(txt, encoding="utf-8")

def month_add(ym, n):
    y, m = int(ym[:4]), int(ym[5:7])
    m += n
    while m < 1:
        m += 12; y -= 1
    while m > 12:
        m -= 12; y += 1
    return f"{y:04d}-{m:02d}"

# ---------------------------------------------------------------- names
PLACES = json.loads((STATIC / "places.json").read_text(encoding="utf-8"))

def key(s):
    """Compare Armenian (or English) names loosely: no spaces, dashes or case; old/new spelling of և."""
    s = unicodedata.normalize("NFC", s or "").lower().replace("և", "եւ")
    return re.sub(r"[\s\-‐-―_.'՚’()]", "", s)

DISTRICT_BY_KEY = {key(d["hy"]): d["id"] for d in PLACES["districts"]}
DISTRICT_BY_KEY.update({key(d["en"]): d["id"] for d in PLACES["districts"]})
DISTRICT_BY_KEY.update({key("Nor-Norq"): "nor-nork", key("Norq-Marash"): "nork-marash", key("Qanaqer-Zeytun"): "kanaker-zeytun"})
MARZ_BY_KEY = {key(m["hy"]): m["id"] for m in PLACES["marzes"]}
TOWN_BY_KEY = {key(t["hy"]): t["id"] for t in PLACES["towns"]}

def marz_from_english(text):
    t = (text or "").lower()
    if t.strip() in ("ra", "total ra", "total", "republic of armenia", "national average") or "republic" in t:
        return "armenia"
    for prefix, mid in (("yere", "yerevan"), ("arag", "aragatsotn"), ("arar", "ararat"), ("arma", "armavir"),
                        ("gegh", "gegharkunik"), ("lori", "lori"), ("kota", "kotayk"), ("shir", "shirak"),
                        ("syun", "syunik"), ("vayo", "vayots-dzor"), ("tavu", "tavush")):
        if t.strip().startswith(prefix) or f"-{prefix}" in t.replace(" ", "") or f"- {prefix}" in t:
            return mid
    return None

# ---------------------------------------------------------------- Cadastre reports
HY_MONTHS = ["հունվար", "փետրվար", "մարտ", "ապրիլ", "մայիս", "հունիս", "հուլիս", "օգոստոս", "սեպտեմբեր",
             "հոկտեմբեր", "նոյեմբեր", "դեկտեմբեր"]
LISTING_URL = "https://cadastre.am/en/market_analysis"

def cadastre_monthly_reports():
    """Return {"YYYY-MM": pdf_url} for every monthly report on the Cadastre listing page."""
    page = fetch(LISTING_URL).decode("utf-8", "replace")
    found = {}
    for href, title in re.findall(r'<a[^>]+href="([^"]+\.pdf)"[^>]*>(.*?)</a>', page, flags=re.S):
        title = html.unescape(re.sub(r"<[^>]+>", " ", title)).strip().lower()
        if "ամս" not in title:       # "ամսվա" = "of the month"; quarterly/annual reports don't have it
            continue
        m = re.search(r"(20\d\d)\s*թ", title)
        mon = next((i + 1 for i, n in enumerate(HY_MONTHS) if n in title), None)
        if not m or not mon:
            continue
        ym = f"{m.group(1)}-{mon:02d}"
        found.setdefault(ym, urllib.parse.urljoin(LISTING_URL, href))
    return found

NUM = r"(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)"
VAL = rf"(?:{NUM}|N/A|-)"
CHG = rf"(?:-?\d+(?:[.,]\d+)?\s*%|\d+(?:[.,]\d+)?\s*անգամ|N/A|-)"
NAME = r"(?P<name>[^\d%]+?(?:\(\s*Գոտի\s*\d+\s*\))?)"   # a place name, optionally "(Գոտի 3)" = price zone 3
# Most tables: same month last year | change | this month | change | previous month
ROW = re.compile(rf"^\s*{NAME}\s+(?P<a>{VAL})\s+(?P<c1>{CHG})\s+(?P<b>{VAL})\s+(?P<c2>{CHG})\s+(?P<c>{VAL})\s*$")
# Some older tables: this month | change | previous month
ROW3 = re.compile(rf"^\s*{NAME}\s+(?P<b>{VAL})\s+(?P<c2>{CHG})\s+(?P<c>{VAL})\s*$")
CAPTION = re.compile(r"^\s*Աղյուսակ\s*\d+(?:\.\d+)*\s*-\s*\d+\s*$")

def num(s):
    if s is None or s.strip() in ("N/A", "-", ""):
        return None
    return float(s.replace(",", ""))

def pdf_text(path):
    from pypdf import PdfReader   # only the Armenia build needs this library
    reader = PdfReader(str(path))
    return "\n".join((p.extract_text() or "") for p in reader.pages)

def split_tables(text):
    """Cut the report into (caption, text_before, rows) for every 'Աղյուսակ N.N-N' table."""
    lines = text.splitlines()
    caps = [i for i, ln in enumerate(lines) if CAPTION.match(ln)]
    out = []
    for n, i in enumerate(caps):
        end = caps[n + 1] if n + 1 < len(caps) else len(lines)
        rows, after = [], []
        for ln in lines[i + 1:end]:
            m = ROW.match(ln) or ROW3.match(ln)
            if m:
                rows.append({"a": None, "c1": None, **m.groupdict()})
            elif not rows:
                after.append(ln)          # the description can come right after the title
        context = " ".join(lines[max(0, i - 12):i] + after[:12])
        out.append((lines[i].strip(), context, rows))
    return out

def classify_rows(rows):
    """Count how many rows name a district / district zone / marz / town."""
    c = {"district": 0, "zone": 0, "avg": 0, "marz": 0, "town": 0}
    for r in rows:
        name = r["name"]
        base = re.sub(r"\(.*?\)", "", name).replace("միջին", "")
        k = key(base)
        if k in DISTRICT_BY_KEY:
            c["district"] += 1
            if "Գոտի" in name:
                c["zone"] += 1
            if "միջին" in name:
                c["avg"] += 1
        if k in MARZ_BY_KEY:
            c["marz"] += 1
        if k in TOWN_BY_KEY:
            c["town"] += 1
    return c

def parse_report(text):
    """Pull the tables the map needs out of one monthly report. Each value comes as
    {"cur": this month, "prev": previous month, "ly": same month last year}."""
    res = {"district_price": {}, "district_zones": {}, "town_price": {}, "district_deals": {}, "marz_deals": {},
           "district_sales": {}, "marz_sales": {}, "order": {}}
    def keep_order(table, k):
        res["order"].setdefault(table, []).append(k)
    for cap, before, rows in split_tables(text):
        if not rows:
            continue
        c = classify_rows(rows)
        b = before.replace("\n", " ")
        is_apartment = "բազմաբնակարան" in b
        is_price = ("գներ" in b or "գին" in b) and all((num(r["b"]) or 0) == 0 or (num(r["b"]) or 0) > 5000 for r in rows)
        is_sales = "առուվաճառք" in b and not is_price
        is_total = "ընդհանուր գործարքների" in b and not is_price
        if is_price and is_apartment and c["district"] >= 8 and not res["district_price"]:
            for r in rows:
                base = re.sub(r"\(.*?\)", "", r["name"]).replace("միջին", "")
                did = DISTRICT_BY_KEY.get(key(base))
                if not did:
                    continue
                vals = {"ly": num(r["a"]), "cur": num(r["b"]), "prev": num(r["c"])}
                z = re.search(r"Գոտի\s*(\d+)", r["name"])
                if z:
                    res["district_zones"].setdefault(did, {})[z.group(1)] = vals
                    keep_order("district_price", f"{did}#{z.group(1)}")
                else:
                    res["district_price"][did] = vals
                    keep_order("district_price", did)
        elif is_price and is_apartment and c["town"] >= 25 and not res["town_price"]:
            for r in rows:
                tid = TOWN_BY_KEY.get(key(r["name"]))
                if tid:
                    res["town_price"][tid] = {"ly": num(r["a"]), "cur": num(r["b"]), "prev": num(r["c"])}
                    keep_order("town_price", tid)
        elif not is_price and c["district"] >= 10 and c["zone"] == 0:
            target = "district_sales" if is_sales else "district_deals" if is_total else None
            if target and not res[target]:
                for r in rows:
                    did = DISTRICT_BY_KEY.get(key(r["name"]))
                    if did:
                        res[target][did] = {"ly": num(r["a"]), "cur": num(r["b"]), "prev": num(r["c"])}
                        keep_order(target, did)
        elif not is_price and c["marz"] >= 10:
            target = "marz_sales" if is_sales else "marz_deals" if is_total else None
            if target and not res[target]:
                for r in rows:
                    k = key(r["name"])
                    mid = MARZ_BY_KEY.get(k) or ("armenia" if k.startswith(key("Հանրապետություն")) else None)
                    if mid:
                        res[target][mid] = {"ly": num(r["a"]), "cur": num(r["b"]), "prev": num(r["c"])}
                        keep_order(target, mid)
    return res

# Rows of numbers in report order, with or without a readable place name in front. Some reports draw the Armenian
# names as pictures, so only the numbers come out; those are matched to places later (see decode_numbers).
NUMROW = re.compile(rf"^\s*(?:{NAME}\s+)?(?P<a>{VAL})\s+(?P<c1>{CHG})\s+(?P<b>{VAL})\s+(?P<c2>{CHG})\s+(?P<c>{VAL})\s*$")

def number_rows(text):
    out = []
    for ln in text.splitlines():
        m = NUMROW.match(ln)
        if m:
            out.append([num(m.group("a")), num(m.group("b")), num(m.group("c"))])   # last year, this month, last month
    return out

TABLES = ("district_price", "town_price", "district_deals", "marz_deals", "district_sales", "marz_sales")

def table_values(res, table):
    """[(key, {"ly","cur","prev"})] in report order for one table of a decoded report."""
    out = []
    for k in res.get("order", {}).get(table, []):
        if "#" in k:
            did, z = k.split("#")
            v = res["district_zones"].get(did, {}).get(z)
        else:
            v = res[table].get(k)
        if v:
            out.append((k, v))
    return out

def align(target, column):
    """Longest common subsequence of two number lists (None never matches). Returns matched (i, j) pairs."""
    n, m = len(target), len(column)
    L = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        ti, Li, Ln = target[i], L[i], L[i + 1]
        for j in range(m - 1, -1, -1):
            Li[j] = Ln[j + 1] + 1 if ti is not None and ti == column[j] else max(Ln[j], Li[j + 1])
    pairs, i, j = [], 0, 0
    while i < n and j < m:
        if target[i] is not None and target[i] == column[j]:
            pairs.append((i, j)); i += 1; j += 1
        elif L[i + 1][j] >= L[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs

def decode_table(nums, keys, known, month):
    """Find one table inside a report's number rows. keys: the table's rows in order (from a readable report).
    known(key, month) -> value. Lines up this report's 'this month' column with known values for the month (or its
    'last month' / 'last year' column with those months), then fills the rows between matches by position."""
    best = None
    for col, offset in ((1, 0), (2, -1), (0, -12)):
        target = [known(k, month_add(month, offset)) for k in keys]
        if sum(t is not None for t in target) < 5:
            continue
        pairs = align(target, [r[col] for r in nums])
        if best is None or len(pairs) > len(best):
            best = pairs
    if not best or len(best) < max(5, 0.6 * len(keys)):
        return None
    span = best[-1][1] - best[0][1] + 1
    if span > len(keys) * 1.3 + 3:          # matches scattered across the report: not this table
        return None
    assign = dict(best)
    for (t1, u1), (t2, u2) in zip(best, best[1:]):
        if t2 - t1 == u2 - u1:
            for k in range(1, t2 - t1):
                assign[t1 + k] = u1 + k
    return {keys[t]: {"ly": nums[u][0], "cur": nums[u][1], "prev": nums[u][2]} for t, u in assign.items()}

def decode_numbers(reports):
    """Give place names to reports that only have numbers, working outwards from the readable ones."""
    decoded = {ym: r for ym, r in reports.items() if r["status"] == "ok"}
    pending = sorted(ym for ym, r in reports.items() if r["status"] == "numbers" and r.get("nums"))
    known_vals = {}
    def add_known(ym, res):
        for table in TABLES:
            for k, v in table_values(res, table):
                for col, month, prio in (("cur", ym, 3), ("prev", month_add(ym, -1), 2), ("ly", month_add(ym, -12), 1)):
                    if v.get(col) is not None:
                        slot = known_vals.setdefault((table, k), {})
                        if month not in slot or slot[month][0] < prio:
                            slot[month] = (prio, v[col])
    for ym, r in decoded.items():
        add_known(ym, r)
    def templates(ym):
        named = sorted((r for r in reports.values() if r["status"] == "ok"), key=lambda r: abs(int(r["month"][:4]) * 12 + int(r["month"][5:]) - int(ym[:4]) * 12 - int(ym[5:])))
        return {t: next((r["order"][t] for r in named if r.get("order", {}).get(t)), None) for t in TABLES}
    progress = True
    while progress and pending:
        progress = False
        for ym in list(pending):
            nums = reports[ym]["nums"]
            tpl = templates(ym)
            res = {t: {} for t in TABLES}
            res.update({"district_zones": {}, "order": {}})
            for table in TABLES:
                if not tpl[table]:
                    continue
                got = decode_table(nums, tpl[table], lambda k, mo: (known_vals.get((table, k), {}).get(mo) or (None, None))[1], ym)
                if not got:
                    continue
                res["order"][table] = [k for k in tpl[table] if k in got]
                for k, v in got.items():
                    if "#" in k:
                        did, z = k.split("#")
                        res["district_zones"].setdefault(did, {})[z] = v
                    else:
                        res[table][k] = v
            if len(res["district_price"]) >= 11 and not [p for p in check_report(res) if "price" in p and "only" not in p]:
                res.update({"month": ym, "status": "decoded", "source": reports[ym]["source"]})
                decoded[ym] = res
                add_known(ym, res)
                pending.remove(ym)
                progress = True
                print(f"  {ym}: matched by numbers ({len(res['district_price'])} districts, {len(res['town_price'])} towns)")
    return decoded, pending

def check_report(res):
    """Return a list of problems; an empty list means the report passed."""
    problems = []
    dp = res["district_price"]
    if len(dp) < 11:
        problems.append(f"only {len(dp)} Yerevan district prices found")
    for did, v in dp.items():
        if v["cur"] is not None and not (50_000 <= v["cur"] <= 10_000_000):
            problems.append(f"{did} price {v['cur']} looks wrong")
    if len(res["town_price"]) < 30:
        problems.append(f"only {len(res['town_price'])} town prices found")
    for tid, v in res["town_price"].items():
        if v["cur"] is not None and not (5_000 <= v["cur"] <= 5_000_000):
            problems.append(f"{tid} price {v['cur']} looks wrong")
    return problems

CADASTRE_FILE = CACHE / "cadastre.json"     # every report read so far, one entry per month ("YYYY-MM")

def load_cadastre():
    store = load_json(CADASTRE_FILE, {})
    old = CACHE / "cadastre"                 # earlier layout: one file per month
    if old.is_dir():
        for f in old.glob("*.json"):
            r = load_json(f, None)
            if r and r["month"] not in store:
                store[r["month"]] = r
    return store

def save_cadastre(store):
    CADASTRE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # one line per month keeps the file readable and the GitHub changes small
    lines = [f"{json.dumps(ym)}:{json.dumps(store[ym], ensure_ascii=False, separators=(',', ':'))}" for ym in sorted(store)]
    CADASTRE_FILE.write_text("{\n" + ",\n".join(lines) + "\n}\n", encoding="utf-8")

def update_cadastre(max_reports):
    store = load_cadastre()
    try:
        reports = cadastre_monthly_reports()
    except Exception as e:
        notice(f"Armenia: could not read the Cadastre report list ({LISTING_URL}): {e}. The map keeps its saved data.")
        return
    if not reports:
        notice(f"Armenia: no monthly reports found on {LISTING_URL}. The page layout may have changed.")
        return
    print(f"Cadastre: {len(reports)} monthly reports listed, newest {max(reports)}")
    todo = []
    for ym in sorted(reports, reverse=True):     # newest first
        if ym < FIRST_MONTH:
            continue
        saved = store.get(ym)
        if saved and (saved.get("parser", 0) >= PARSER_VERSION or (saved.get("status") == "ok" and saved.get("order"))):
            continue
        todo.append(ym)
    for ym in todo[:max_reports]:
        url = reports[ym]
        pdf = DL / "pdf" / (ym + ".pdf")
        try:
            if not pdf.exists():
                pdf.parent.mkdir(parents=True, exist_ok=True)
                pdf.write_bytes(fetch(url, timeout=300))
                time.sleep(1)
            text = pdf_text(pdf)
        except Exception as e:
            print(f"  {ym}: download/read failed: {e}")
            if ym >= month_add(dt.date.today().strftime("%Y-%m"), -3):
                notice(f"Armenia: the Cadastre report for {ym} could not be downloaded or opened ({url}): {e}")
            continue
        res = parse_report(text)
        problems = check_report(res)
        pdf.unlink(missing_ok=True)
        nums = number_rows(text)
        # ok = names and numbers readable; numbers = names are pictures, numbers are text (matched later);
        # unreadable = the tables are pictures
        status = "ok" if not problems else "numbers" if len(nums) >= 40 else "unreadable"
        entry = {"month": ym, "source": url, "status": status, "problems": problems, "parser": PARSER_VERSION,
                 "text_chars": len(text), **res}
        if status == "numbers":
            entry["nums"] = nums
        store[ym] = entry
        save_cadastre(store)
        print(f"  {ym}: {status} ({len(res['district_price'])} districts, {len(res['town_price'])} towns, {len(nums)} number rows)")
        if status == "unreadable" and ym >= month_add(dt.date.today().strftime("%Y-%m"), -3):
            notice(f"Armenia: the Cadastre report for {ym} did not pass the checks ({'; '.join(problems)}). "
                   f"It may be a scanned PDF or the layout changed. The numbers can be typed into "
                   f"armenia/cache/cadastre.json (the \"{ym}\" line) by hand. Report: {url}")
    save_cadastre(store)
    if (CACHE / "cadastre").is_dir():
        shutil.rmtree(CACHE / "cadastre")    # moved into cadastre.json
    if len(todo) > max_reports:
        print(f"  ({len(todo) - max_reports} older reports left for later runs)")

def combine_cadastre():
    """Merge all saved reports into monthly series. A month's own report wins; otherwise use the
    'previous month' column of the next report, then the 'same month last year' column of a report 12 months on."""
    series = {}   # (group, id) -> {month: (priority, value)}
    zones_latest = {}
    reports = load_cadastre()
    decoded, left = decode_numbers(reports)
    meta = {"months_read": sorted(decoded), "months_unreadable": sorted(m for m in reports if m not in decoded)}
    newest = max(reports) if reports else None
    if newest and newest not in decoded:
        notice(f"Armenia: the newest Cadastre report ({newest}) could not be matched to places, so the map still shows "
               f"{max(decoded) if decoded else 'nothing'}. Report: {reports[newest].get('source')}")
    for ym in sorted(decoded):
        r = decoded[ym]
        for group in ("district_price", "town_price", "district_deals", "marz_deals", "district_sales", "marz_sales"):
            for pid, v in r.get(group, {}).items():
                s = series.setdefault((group, pid), {})
                for col, month, prio in (("cur", ym, 3), ("prev", month_add(ym, -1), 2), ("ly", month_add(ym, -12), 1)):
                    val = v.get(col)
                    if val is None:
                        continue
                    if month not in s or s[month][0] < prio:
                        s[month] = (prio, val)
        for did, zs in r.get("district_zones", {}).items():
            for z, v in zs.items():
                s = series.setdefault(("zone_price", f"{did}#{z}"), {})
                for col, month, prio in (("cur", ym, 3), ("prev", month_add(ym, -1), 2), ("ly", month_add(ym, -12), 1)):
                    if v.get(col) is not None and (month not in s or s[month][0] < prio):
                        s[month] = (prio, v[col])
            zones_latest.setdefault(did, {})
            for z, v in zs.items():
                if v.get("cur") is not None:
                    zones_latest[did][z] = (ym, v["cur"], v.get("ly"))
    out = {}
    for (group, pid), s in series.items():
        out.setdefault(group, {})[pid] = {m: round(v[1]) for m, v in sorted(s.items())}
    zones = {}
    for did, zs in zones_latest.items():
        latest = max(v[0] for v in zs.values())
        zones[did] = {"month": latest, "zones": {z: {"price": v[1], "ly": v[2]} for z, v in sorted(zs.items(), key=lambda x: int(x[0])) if v[0] == latest}}
    return out, zones, meta

# ---------------------------------------------------------------- Armstat (PxWeb API)
PX = "https://statbank.armstat.am/api/v1/en/ArmStatBank/"

def px_meta(path):
    return json.loads(fetch(PX + urllib.parse.quote(path)).decode("utf-8-sig"))

def px_table(path, pick):
    """Download one table. pick(variable_code, value_text) -> True to keep that value.
    Returns a list of dicts {variable_code: value_text, ..., "value": number}."""
    meta = px_meta(path)
    query, names = [], {}
    for v in meta["variables"]:
        keep = [code for code, text in zip(v["values"], v["valueTexts"]) if pick(v["code"], text)]
        if not keep:
            raise ValueError(f"nothing selected for {v['code']} in {path}")
        query.append({"code": v["code"], "selection": {"filter": "item", "values": keep}})
        names[v["code"]] = dict(zip(v["values"], v["valueTexts"]))
    body = json.dumps({"query": query, "response": {"format": "json"}}).encode()
    data = json.loads(fetch(PX + urllib.parse.quote(path), data=body, headers={"Content-Type": "application/json"}).decode("utf-8-sig"))
    dims = [c["code"] for c in data["columns"] if c.get("type") != "c"]
    rows = []
    for d in data["data"]:
        val = d["values"][0]
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        row = {code: names[code].get(k, k) for code, k in zip(dims, d["key"])}
        row["value"] = val
        rows.append(row)
    return rows

def year_of(text):
    m = re.match(r"\s*(\d{4})", text)
    return m.group(1) if m else None

def by_marz_year(rows, marz_code, year_code="years"):
    out = {}
    for r in rows:
        mid = marz_from_english(r[marz_code])
        y = year_of(r[year_code])
        if mid and y:
            out.setdefault(mid, {})[y] = round(r["value"], 2)
    return out

def update_armstat():
    """Yearly tables: refreshed at most once a week (they change once a year)."""
    saved = load_json(CACHE / "armstat.json", {})
    needed = ("poverty", "space_per_person", "new_housing_m2", "wage", "population", "house_sales", "apartment_sales",
              "district_apartments", "inflation")
    # refresh weekly, or at once when a table was added since the last download
    if saved.get("fetched") and (dt.date.today() - dt.date.fromisoformat(saved["fetched"])).days < 7 and all(k in saved for k in needed):
        return saved
    out = {"fetched": dt.date.today().isoformat()}
    P = "2 Population and social processes/"
    jobs = {
        "poverty": (P + "25 Households/PS-hh-11-2022.px",
                    lambda c, t: (c != "indicators" or t.strip() == "Poor population"), "marzes"),
        "space_per_person": (P + "26 Housing/261 Bnakfond/PS-hs-bn02.px",
                             lambda c, t: (c != "indicators" or t.strip() == "Total"), "marzes"),
        "new_housing_m2": ("3 Industry, Construction, trade and services/31 Construction/IC-co-1-2024.px",
                           lambda c, t: (c != "indicators" or t.strip().startswith("Total space of dwellings put in place, (sq. metres")), "marzes"),
        "wage": (P + "23 Employment and unemployment/PS-eu-19-2024.px",
                 lambda c, t: (c != "indicators" or (re.search(r"by RA Marzes and Yerevan\s*-", t) and "Sectors" not in t)), "indicators"),
        "population": (P + "20 Census/PS-pp-1-1-2.px",
                       lambda c, t: (c not in ("settlement", "sex") or t.strip() == "Total"), "marzes"),
        # sales ("trade transactions") registered in the year, by type of property
        "house_sales": (P + "26 Housing/262 Shuka/PS-hs-rem03.px",
                        lambda c, t: (c != "type of transaction" or t.strip().startswith("Residential houses")), "marzes"),
        "apartment_sales": (P + "26 Housing/262 Shuka/PS-hs-rem03.px",
                            lambda c, t: (c != "type of transaction" or t.strip().startswith("Apartments in multi-apartment")), "marzes"),
    }
    for name, (path, pick, marz_code) in jobs.items():
        try:
            rows = px_table(path, pick)
            vals = by_marz_year(rows, marz_code)
            if len(vals) < 10:
                raise ValueError(f"only {len(vals)} marzes found")
            out[name] = vals
            print(f"Armstat {name}: {len(vals)} areas, years {min(min(v) for v in vals.values())}-{max(max(v) for v in vals.values())}")
        except Exception as e:
            notice(f"Armenia: Armstat table '{name}' ({path}) failed: {e}. The saved copy is used.")
            if name in saved:
                out[name] = saved[name]
        time.sleep(0.5)
    # Apartments in apartment buildings by Yerevan district
    try:
        rows = px_table(P + "26 Housing/261 Bnakfond/PS-hs-bn20.px", lambda c, t: True)
        d = {}
        for r in rows:
            did = DISTRICT_BY_KEY.get(key(r["administrative regions"]))
            y = year_of(r["years"])
            if did and y:
                d.setdefault(did, {})[y] = round(r["value"])
        out["district_apartments"] = d
        print(f"Armstat district_apartments: {len(d)} districts")
    except Exception as e:
        notice(f"Armenia: Armstat apartments-by-district table failed: {e}")
        if "district_apartments" in saved:
            out["district_apartments"] = saved["district_apartments"]
    # Inflation: each month vs the same month a year earlier
    try:
        rows = px_table("1 Econnomy and finance/12 Consumer Prices/1.2.1 Monthly indicators/EF-cpi-mi3.px",
                        lambda c, t: (c != "period" or "Each month" in t))
        months = {n: i + 1 for i, n in enumerate(["January", "February", "March", "April", "May", "June", "July",
                                                  "August", "September", "October", "November", "December"])}
        cpi = {}
        for r in rows:
            mo = months.get(r["months"].strip())
            if mo and r["value"] > 0:
                cpi[f"{r['years'].strip()}-{mo:02d}"] = round(r["value"] - 100, 2)   # index (previous year = 100) -> % change
        out["inflation"] = dict(sorted(cpi.items()))
        print(f"Armstat inflation: {len(cpi)} months, latest {max(cpi)}")
    except Exception as e:
        notice(f"Armenia: Armstat inflation table failed: {e}")
        if "inflation" in saved:
            out["inflation"] = saved["inflation"]
    save_json(CACHE / "armstat.json", out, pretty=True)
    return out

# ---------------------------------------------------------------- Central Bank of Armenia
CBA_STAT = "http://old.cba.am/stat/stat_data_eng/"

def xlsx_rows(data, sheet_name=None):
    """Minimal .xlsx reader (standard library only): returns the rows of one sheet as lists of values."""
    z = zipfile.ZipFile(io.BytesIO(data))
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
          "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns):
            shared.append("".join(t.text or "" for t in si.iter("{%s}t" % ns["m"])))
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    rel_target = {r.get("Id"): r.get("Target") for r in rels}
    sheets = [(s.get("name"), rel_target[s.get("{%s}id" % ns["r"])]) for s in wb.find("m:sheets", ns)]
    name, target = next(((n, t) for n, t in sheets if n == sheet_name), sheets[0])
    target = target.lstrip("/")
    target = target if target.startswith("xl/") else "xl/" + target
    rows = []
    for row in ET.fromstring(z.read(target)).iter("{%s}row" % ns["m"]):
        cells = {}
        for c in row.findall("m:c", ns):
            ref = c.get("r")
            col = 0
            for ch in re.match(r"[A-Z]+", ref).group(0):
                col = col * 26 + (ord(ch) - 64)
            v = c.find("m:v", ns)
            t = c.get("t")
            if t == "s" and v is not None:
                val = shared[int(v.text)]
            elif t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter("{%s}t" % ns["m"]))
            elif v is not None:
                try:
                    val = float(v.text)
                except ValueError:
                    val = v.text
            else:
                val = None
            cells[col - 1] = val
        if cells:
            rows.append([cells.get(i) for i in range(max(cells) + 1)])
        else:
            rows.append([])
    return rows

EN_MON = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}

def header_month(v):
    if isinstance(v, float) and 20000 < v < 80000:      # Excel date number
        d = dt.date(1899, 12, 30) + dt.timedelta(days=int(v))
        return d.strftime("%Y-%m")
    if isinstance(v, str):
        m = re.match(r"\s*([A-Za-z]{3})[a-z]*[-\s']*(\d{2,4})", v)
        if m and m.group(1).lower() in EN_MON:
            y = int(m.group(2))
            y = y + 2000 if y < 100 else y
            return f"{y:04d}-{EN_MON[m.group(1).lower()]:02d}"
    return None

# ---- Mortgage interest rates (new mortgage loans to households, % a year) ----
# The CBA's monthly statistical bulletin has a line "Mortgage loans of households" (Armenian: "Ֆիզիկական անձանց
# հիպոտեկային վարկեր") with the rate in drams and in US dollars for the latest 9-13 months. English editions run to
# 2022, Armenian ones after that. There is no separate euro mortgage rate; for euros the closest official figure is
# the rate on new household loans of over 5 years in euros (CBA file 14, from 2018).
ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII"]

def bulletin_urls(y, m):
    r, base = ROMAN[m - 1], "https://www.cba.am/file_manager/"
    return [f"{base}Statistical%20overview/Statistical_overview_{r}.{y}.pdf",
            f"{base}Statistical%20overview/Statistical_Overview_{r}.{y}_eng.pdf",
            f"{base}Statistics/Statistical-overview-en/Statistical-Overview-{r}.{y}-en.pdf"]

def rate_numbers(line):
    """The rates on one table line. PDF text sometimes splits a number ("1 1.3", "9 .6", "7. 8"); those are joined."""
    line = line.replace("․", ".").replace(",", ".")      # some issues write the decimal point as "․" (Armenian)
    toks = re.findall(r"\d+\.?\d*|\.\d+", line)
    out, i = [], 0
    while i < len(toks):
        t = toks[i]
        if i + 1 < len(toks) and ((len(t) == 1 and "." in toks[i + 1] and not toks[i + 1].startswith(".") and len(toks[i + 1].split(".")[0]) == 1)
                                  or toks[i + 1].startswith(".") or t.endswith(".")):
            t, i = t + toks[i + 1], i + 1
        i += 1
        try:
            v = float(t)
        except ValueError:
            continue
        if 2 <= v <= 40:
            out.append(v)
    return out

def parse_mortgage_rates(text, issue_ym):
    """{"amd": {month: rate}, "usd": {...}} from one bulletin (the last value is the issue month)."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if not re.search(r"Mortgage loans of households|անձանց\s+հիպոտեկային\s+վարկեր", ln):
            continue
        got = {}
        for ln2 in lines[i + 1:i + 5]:
            m = re.match(r"\s*(AMD|USD|ՀՀ\s*դրամով|ԱՄՆ\s*դոլարով)(.*)", ln2)
            if m:
                cur = "amd" if m.group(1) in ("AMD",) or "դրամ" in m.group(1) else "usd"
                vals = rate_numbers(m.group(2))
                if len(vals) >= 5:
                    got[cur] = {month_add(issue_ym, k - len(vals) + 1): v for k, v in enumerate(vals)}
        if got.get("amd"):
            return got
    return None

def update_mortgage_rates(saved, max_downloads=60):
    rates = saved.get("mortgage_rate", {"amd": {}, "usd": {}})
    tried = set(saved.get("bulletins_tried", []))
    this = dt.date.today().strftime("%Y-%m")
    # every third month from mid-2011 (each issue covers 9-13 months); the last 4 months are always checked again
    want = []
    ym = "2011-06"
    while ym <= this:
        want.append(ym)
        ym = month_add(ym, 3)
    want += [month_add(this, -k) for k in range(1, 5)]
    downloads, written = 0, set()
    try:
        from pypdf import PdfReader  # noqa: F401
    except ImportError:
        notice("Armenia: pypdf is missing, so the mortgage-rate bulletins were not read.")
        return rates, sorted(tried)
    for ym in sorted(set(want), reverse=True):
        recent = ym >= month_add(this, -4)
        if (ym in tried and not recent) or downloads >= max_downloads:
            continue
        y, m = int(ym[:4]), int(ym[5:])
        for url in bulletin_urls(y, m):
            pdf = DL / "cba" / f"bulletin_{ym}.pdf"
            try:
                pdf.parent.mkdir(parents=True, exist_ok=True)
                pdf.write_bytes(fetch(url, timeout=300, tries=1))
            except Exception:
                continue
            downloads += 1
            try:
                got = parse_mortgage_rates(pdf_text(pdf), ym)
            except Exception as e:
                got = None
                print(f"  bulletin {ym}: could not read ({e})")
            pdf.unlink(missing_ok=True)
            if got:
                # issues are read newest first, so the first figure for a month in this run is the latest
                # (revised) one; figures saved by earlier runs are replaced
                for cur, s in got.items():
                    for k, v in s.items():
                        if (cur, k) not in written:
                            rates.setdefault(cur, {})[k] = v
                            written.add((cur, k))
                print(f"  bulletin {ym}: mortgage rates for {len(got['amd'])} months")
            else:
                print(f"  bulletin {ym}: no mortgage-rate table found")
            break
        if not recent:
            tried.add(ym)
        time.sleep(0.5)
    rates = {cur: dict(sorted(s.items())) for cur, s in rates.items()}
    return rates, sorted(tried)

def euro_longterm_rates():
    """New household loans of over 5 years in euros (CBA file 14): average rate per month. Also AMD and USD."""
    rows = xlsx_rows(fetch(CBA_STAT + "14_Deposit_and_lending_rates_eng.xlsx"))
    over5 = 1 + 7 * 5      # maturity buckets of 5 columns (sum, min, average, max, stdev); "Over 5 year" is the 8th
    month = section = sector = None
    out = {}
    for r in rows:
        if not r:
            continue
        c0, c1 = r[0], (r[1] if len(r) > 1 else None)
        if c0 in (None, "") and isinstance(c1, float) and 20000 < c1 < 80000:
            month = header_month(c1)
        elif isinstance(c0, str):
            s = c0.strip()
            if s in ("Loans", "Deposits"):
                section = s
            elif s in ("Households", "Non Financial Corporations"):
                sector = s
            elif s in ("AMD", "USD", "EUR") and section == "Loans" and sector == "Households" and month and len(r) > over5 + 2:
                vol, avg = r[over5], r[over5 + 2]
                if isinstance(avg, float) and isinstance(vol, float) and vol > 0:
                    out.setdefault(s.lower(), {})[month] = round(avg, 2)
    return out

def update_cba():
    saved = load_json(CACHE / "cba.json", {})
    out = dict(saved)
    # Mortgage loans outstanding (commercial banks, end of month, million AMD): AMD, foreign currency, total
    try:
        rows = xlsx_rows(fetch(CBA_STAT + "7_loans%20by%20branches_eng.xlsx"), "branches")
        header, blocks = None, []
        for r in rows:
            label = r[0].strip().lower() if r and isinstance(r[0], str) else ""
            if label.startswith("mortgage") and header:
                blocks.append({header[i]: round(v, 1) for i, v in enumerate(r) if i and i < len(header) and header[i] and isinstance(v, float)})
            elif r and not label and sum(1 for v in r[1:] if header_month(v)) > 24:   # the row of month headings
                header = [header_month(v) for v in r]
        if len(blocks) != 3:
            raise ValueError(f"expected 3 mortgage rows (AMD, FX, total), found {len(blocks)}")
        amd, fx, total = blocks
        out["mortgage"] = {m: {"amd": amd.get(m), "fx": fx.get(m), "total": total[m]} for m in sorted(total) if total[m]}
        print(f"CBA mortgages: {len(out['mortgage'])} months, latest {max(out['mortgage'])}")
    except Exception as e:
        notice(f"Armenia: CBA mortgage file failed: {e}")
    # Mortgage interest rates (drams and US dollars) and the euro long-term household loan rate
    try:
        out["mortgage_rate"], out["bulletins_tried"] = update_mortgage_rates(saved)
        print(f"CBA mortgage rates: {len(out['mortgage_rate'].get('amd', {}))} months, latest {max(out['mortgage_rate'].get('amd') or {'-': 0})}")
    except Exception as e:
        notice(f"Armenia: CBA mortgage-rate bulletins failed: {e}")
    try:
        out["longterm_rate"] = euro_longterm_rates()
        print(f"CBA long-term household loan rates (AMD/USD/EUR): {len(out['longterm_rate'].get('eur', {}))} months")
    except Exception as e:
        notice(f"Armenia: CBA long-term loan rates failed: {e}")
    # Policy (refinancing) rate history
    try:
        rows = xlsx_rows(fetch(CBA_STAT + "8_policy_rates_eng.xlsx"))
        changes = []
        for r in rows:
            if r and isinstance(r[0], str) and isinstance(r[1] if len(r) > 1 else None, float):
                m = re.match(r"\s*(\d{2})\.(\d{2})\.(\d{4})", r[0])
                if m:
                    changes.append([f"{m.group(3)}-{m.group(2)}-{m.group(1)}", r[1]])
        if len(changes) < 20:
            raise ValueError("too few rows")
        out["policy_rate"] = sorted(changes)
        print(f"CBA policy rate: {len(changes)} changes, latest {changes[-1]}")
    except Exception as e:
        notice(f"Armenia: CBA policy-rate file failed: {e}")
    # AMD per USD, monthly average of the official daily rate (web service built for websites)
    usd = dict(saved.get("usd", {}))
    this_month = dt.date.today().strftime("%Y-%m")
    start = "2018-01"
    ym = start
    fetched = 0
    while ym <= this_month:
        if ym not in usd or ym >= month_add(this_month, -1):
            y, m = int(ym[:4]), int(ym[5:])
            last = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1))
            body = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
<soap:Body><ExchangeRatesByDateRangeByISO xmlns="http://www.cba.am/"><ISOCodes>USD</ISOCodes>
<DateFrom>{ym}-01</DateFrom><DateTo>{last.isoformat()}</DateTo></ExchangeRatesByDateRangeByISO></soap:Body></soap:Envelope>"""
            try:
                x = fetch("https://api.cba.am/exchangerates.asmx", data=body.encode(),
                          headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '"http://www.cba.am/ExchangeRatesByDateRangeByISO"'})
                rates = [float(t) for t in re.findall(rb"<Rate>([\d.]+)</Rate>", x)]
                if rates:
                    usd[ym] = round(sum(rates) / len(rates), 2)
                fetched += 1
                time.sleep(0.3)
            except Exception as e:
                notice(f"Armenia: CBA exchange rate for {ym} failed: {e}")
                break
        ym = month_add(ym, 1)
    out["usd"] = dict(sorted(usd.items()))
    print(f"CBA AMD/USD: {len(usd)} months (fetched {fetched}), latest {max(usd) if usd else None}")
    save_json(CACHE / "cba.json", out, pretty=True)
    return out

# ---------------------------------------------------------------- output
def build(max_reports):
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        import pypdf  # noqa: F401
        update_cadastre(max_reports)
    except ImportError:
        notice("Armenia: the pypdf library is missing, so new Cadastre reports were not read (pip install pypdf).")
    series, zones, meta = combine_cadastre()
    armstat = update_armstat()
    cba = update_cba()

    dp = series.get("district_price", {})
    if not dp:
        raise SystemExit("No Cadastre price data at all; not writing the Armenia data file.")
    latest = max(max(s) for s in dp.values())
    if latest < month_add(dt.date.today().strftime("%Y-%m"), -4):
        notice(f"Armenia: the newest Cadastre prices are for {latest}, which is more than 3 months old.")

    data = {
        "built": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"),
        "latest_month": latest,
        "months_read": meta["months_read"],
        "months_unreadable": meta["months_unreadable"],
        "districts": {}, "towns": {}, "marzes": {}, "national": {},
    }
    for d in PLACES["districts"]:
        i = d["id"]
        data["districts"][i] = {"hy": d["hy"], "en": d["en"],
                                "price": dp.get(i, {}), "deals": series.get("district_deals", {}).get(i, {}),
                                "sales": series.get("district_sales", {}).get(i, {}), "zones": zones.get(i),
                                "zone_prices": {k.split("#")[1]: v for k, v in sorted(series.get("zone_price", {}).items(), key=lambda kv: int(kv[0].split("#")[1])) if k.split("#")[0] == i},
                                "apartments": armstat.get("district_apartments", {}).get(i, {})}
    for t in PLACES["towns"]:
        i = t["id"]
        data["towns"][i] = {"hy": t["hy"], "en": t["en"], "marz": t["marz"], "lat": t["lat"], "lon": t["lon"],
                            "price": series.get("town_price", {}).get(i, {})}
    for m in PLACES["marzes"]:
        i = m["id"]
        data["marzes"][i] = {"hy": m["hy"], "en": m["en"],
                             "deals": series.get("marz_deals", {}).get(i, {}), "sales": series.get("marz_sales", {}).get(i, {}),
                             **{k: armstat.get(k, {}).get(i, {}) for k in ("poverty", "wage", "space_per_person", "new_housing_m2", "population", "house_sales", "apartment_sales")}}
    data["national"] = {
        "usd": cba.get("usd", {}), "mortgage": cba.get("mortgage", {}), "policy_rate": cba.get("policy_rate", []),
        "mortgage_rate": cba.get("mortgage_rate", {}), "longterm_rate": cba.get("longterm_rate", {}),
        "inflation": armstat.get("inflation", {}),
        "deals": series.get("marz_deals", {}).get("armenia", {}), "sales": series.get("marz_sales", {}).get("armenia", {}),
        **{k: armstat.get(k, {}).get("armenia", {}) for k in ("poverty", "wage", "space_per_person", "new_housing_m2", "population", "house_sales", "apartment_sales")},
    }
    save_json(OUT / "armenia.json", data)
    shutil.copyfile(STATIC / "areas.geojson", OUT / "areas.geojson")
    # Yerevan's valuation zones, traced from the official map (see setup/trace_zones.py)
    if (STATIC / "yerevan_zones.geojson").exists():
        shutil.copyfile(STATIC / "yerevan_zones.geojson", OUT / "yerevan_zones.geojson")
    size = (OUT / "armenia.json").stat().st_size
    print(f"Wrote {OUT / 'armenia.json'} ({size // 1024} KB), latest month {latest}, "
          f"{len(meta['months_read'])} reports read, {len(meta['months_unreadable'])} unreadable")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-reports", type=int, default=12, help="most new Cadastre reports to download in one run")
    args = ap.parse_args()
    try:
        build(args.max_reports)
    finally:
        if notices:
            with open(NOTICES, "a", encoding="utf-8") as f:
                f.write("\n".join(["", "### Armenia map", ""] + [f"- {n}" for n in notices]) + "\n")

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
