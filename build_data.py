"""Housing Map - monthly data builder.

Downloads free public housing data and writes small JSON files the map reads:
  site/data/meta.json                 when it was built, latest months, sources, mortgage rate
  site/data/{state,metro,county}.json latest value of every metric for every place
  site/data/hist/{level}/{key}.json   monthly history for the charts (one file per state)

Sources (no keys or accounts needed):
  Zillow Research      home values (ZHVI), rents (ZORI), 1-year forecast (metros)
  Realtor.com          listings, days on market, price cuts, new listings, listing prices
  U.S. Census Bureau   median household income + poverty (SAIPE), population + births/deaths/migration
                       (Population Estimates), county-to-metro list
  FRED (Freddie Mac)   30-year mortgage rate

Runs on GitHub (see .github/workflows/update.yml) once a month, or locally:  python build_data.py
Only uses the Python standard library.
"""
import csv
import gzip
import io
import json
import math
import os
import sys
import time
import urllib.request
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "site", "data")
CACHE = os.path.join(HERE, ".cache")  # downloads are reused for 20 hours when testing locally
UA = {"User-Agent": "Mozilla/5.0 (compatible; HousingMapBuilder/1.0; personal non-commercial use)"}
csv.field_size_limit(10_000_000)

ZILLOW = "https://files.zillowstatic.com/research/public_csvs"
REALTOR = "https://econdata.s3-us-west-2.amazonaws.com/Reports/Core"
CENSUS = "https://www2.census.gov/programs-surveys"

STATES = {  # FIPS: (abbreviation, name)
    "01": ("AL", "Alabama"), "02": ("AK", "Alaska"), "04": ("AZ", "Arizona"), "05": ("AR", "Arkansas"),
    "06": ("CA", "California"), "08": ("CO", "Colorado"), "09": ("CT", "Connecticut"), "10": ("DE", "Delaware"),
    "11": ("DC", "District of Columbia"), "12": ("FL", "Florida"), "13": ("GA", "Georgia"), "15": ("HI", "Hawaii"),
    "16": ("ID", "Idaho"), "17": ("IL", "Illinois"), "18": ("IN", "Indiana"), "19": ("IA", "Iowa"),
    "20": ("KS", "Kansas"), "21": ("KY", "Kentucky"), "22": ("LA", "Louisiana"), "23": ("ME", "Maine"),
    "24": ("MD", "Maryland"), "25": ("MA", "Massachusetts"), "26": ("MI", "Michigan"), "27": ("MN", "Minnesota"),
    "28": ("MS", "Mississippi"), "29": ("MO", "Missouri"), "30": ("MT", "Montana"), "31": ("NE", "Nebraska"),
    "32": ("NV", "Nevada"), "33": ("NH", "New Hampshire"), "34": ("NJ", "New Jersey"), "35": ("NM", "New Mexico"),
    "36": ("NY", "New York"), "37": ("NC", "North Carolina"), "38": ("ND", "North Dakota"), "39": ("OH", "Ohio"),
    "40": ("OK", "Oklahoma"), "41": ("OR", "Oregon"), "42": ("PA", "Pennsylvania"), "44": ("RI", "Rhode Island"),
    "45": ("SC", "South Carolina"), "46": ("SD", "South Dakota"), "47": ("TN", "Tennessee"), "48": ("TX", "Texas"),
    "49": ("UT", "Utah"), "50": ("VT", "Vermont"), "51": ("VA", "Virginia"), "53": ("WA", "Washington"),
    "54": ("WV", "West Virginia"), "55": ("WI", "Wisconsin"), "56": ("WY", "Wyoming"),
}
ABBR_TO_FIPS = {a: f for f, (a, n) in STATES.items()}
NAME_TO_FIPS = {n.lower(): f for f, (a, n) in STATES.items()}


# ---------- downloading ----------
def fetch(url, name=None):
    """Download a file (cached for 20 hours under .cache/ so local test runs are quick). Returns bytes."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name or url.rsplit("/", 1)[-1])
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 20 * 3600:
        with open(path, "rb") as f:
            return f.read()
    last = None
    for attempt in range(3):
        try:
            print(f"  downloading {url}", flush=True)
            data = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=180).read()
            if url.endswith(".gz"):
                data = gzip.decompress(data)
            with open(path, "wb") as f:
                f.write(data)
            return data
        except Exception as e:  # network hiccup: try again (but a missing file stays missing)
            last = e
            if getattr(e, "code", None) == 404:
                break
            time.sleep(5 * (attempt + 1))
    raise last


def fetch_first(urls, name):
    """The first of several URLs that works (e.g. this year's Census file, else last year's)."""
    for url in urls:
        try:
            return fetch(url, name), url
        except Exception as e:
            print(f"  (not available: {url} - {e})")
    return None, None


def rows(data, encoding="utf-8"):
    return csv.reader(io.StringIO(data.decode(encoding, "replace")))


def num(s):
    try:
        v = float(s)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


# ---------- Zillow (wide CSVs: one column per month) ----------
def zillow(path, level):
    """{place id: {"name":..., "series": {"YYYY-MM": value}}} for one Zillow file."""
    data = fetch(f"{ZILLOW}/{path}", f"zillow_{path.replace('/', '_')}")
    r = rows(data)
    head = next(r)
    month_cols = [(i, h[:7]) for i, h in enumerate(head) if len(h) == 10 and h[4] == "-"]
    col = {h: i for i, h in enumerate(head)}
    out = {}
    for row in r:
        if not row:
            continue
        if level == "county":
            pid = row[col["StateCodeFIPS"]].zfill(2) + row[col["MunicipalCodeFIPS"]].zfill(3)
        elif level == "state":
            pid = NAME_TO_FIPS.get(row[col["RegionName"]].lower())
        else:
            pid = row[col["RegionName"]]  # Zillow metro name, matched to a Census metro later
        if not pid:
            continue
        series = {m: v for i, m in month_cols if (v := num(row[i])) is not None}
        out[pid] = {"name": row[col["RegionName"]], "series": series, "row": {h: row[i] for h, i in col.items() if i < len(row) and h in ("Metro", "State")}}
    return out


# ---------- Realtor.com (long CSVs: one row per place per month) ----------
REALTOR_FIELDS = {
    "inv": "active_listing_count", "dom": "median_days_on_market", "newl": "new_listing_count",
    "cut": "price_reduced_share", "lp": "median_listing_price", "pend": "pending_listing_count",
}


def realtor(level):
    name = {"county": "County", "metro": "Metro", "state": "State"}[level]
    data = fetch(f"{REALTOR}/RDC_Inventory_Core_Metrics_{name}_History.csv", f"realtor_{level}.csv")
    r = rows(data)
    head = next(r)
    col = {h: i for i, h in enumerate(head)}
    out = {}
    for row in r:
        if len(row) < len(head) - 1 or not row[0].isdigit():
            continue  # notes at the bottom of the file
        ym = f"{row[0][:4]}-{row[0][4:6]}"
        if level == "county":
            pid = row[col["county_fips"]].zfill(5)
        elif level == "metro":
            pid = row[col["cbsa_code"]]
        else:
            pid = ABBR_TO_FIPS.get(row[col["state_id"]].upper())
        if not pid:
            continue
        rec = out.setdefault(pid, {k: {} for k in REALTOR_FIELDS})
        for k, field in REALTOR_FIELDS.items():
            v = num(row[col[field]])
            if v is not None:
                rec[k][ym] = v
    return out


# ---------- Census ----------
def census_metros():
    """County -> metro (CBSA code) list, metro names and population (Census Population Estimates, metro file)."""
    year = date.today().year
    urls = [f"{CENSUS}/popest/datasets/2020-{y}/metro/totals/cbsa-est{y}-alldata.csv" for y in range(year, year - 4, -1)]
    data, url = fetch_first(urls, "census_cbsa.csv")
    r = rows(data, "latin-1")
    head = next(r)
    col = {h: i for i, h in enumerate(head)}
    names, county_to_cbsa = {}, {}
    for row in r:
        if not row:
            continue
        cbsa, stcou, lsad = row[col["CBSA"]], row[col["STCOU"]], row[col["LSAD"]]
        if not stcou and not row[col["MDIV"]] and "Metropolitan Statistical Area" in lsad:
            names[cbsa] = row[col["NAME"]]
        elif stcou:
            county_to_cbsa[stcou.zfill(5)] = cbsa
    county_to_cbsa = {c: m for c, m in county_to_cbsa.items() if m in names}  # metros only (not micro areas)
    return names, county_to_cbsa


def census_population():
    """Population, births, deaths, migration by county and state (latest Census Population Estimates)."""
    year = date.today().year
    urls = [f"{CENSUS}/popest/datasets/2020-{y}/counties/totals/co-est{y}-alldata.csv" for y in range(year, year - 4, -1)]
    data, url = fetch_first(urls, "census_pop.csv")
    r = rows(data, "latin-1")
    head = next(r)
    col = {h: i for i, h in enumerate(head)}
    y = max(int(h[-4:]) for h in head if h.startswith("POPESTIMATE") and h[-4:].isdigit())
    out = {"_year": y}
    for row in r:
        if not row:
            continue
        sumlev, st, co = row[col["SUMLEV"]], row[col["STATE"]].zfill(2), row[col["COUNTY"]].zfill(3)
        pid = st if sumlev == "040" else st + co if sumlev == "050" else None
        if not pid:
            continue
        g = lambda name: num(row[col[name]]) if name in col else None
        out[pid] = {
            "name": row[col["CTYNAME"]],
            "pop": g(f"POPESTIMATE{y}"), "pop_prev": g(f"POPESTIMATE{y - 1}"), "pop_2020": g("POPESTIMATE2020"),
            "births": g(f"BIRTHS{y}"), "deaths": g(f"DEATHS{y}"), "natural": g(f"NATURALCHG{y}"),
            "dom_mig": g(f"DOMESTICMIG{y}"), "intl_mig": g(f"INTERNATIONALMIG{y}"),
        }
    return out


def census_income():
    """Median household income and poverty rate by county and state (Census SAIPE, latest year)."""
    year = date.today().year
    for y in range(year, year - 5, -1):
        yy = str(y)[2:]
        data, url = fetch_first([f"{CENSUS}/saipe/datasets/{y}/{y}-state-and-county/est{yy}all.txt"], f"saipe{yy}.txt")
        if data:
            break
    out = {"_year": y}
    for line in data.decode("latin-1").splitlines():
        t = line.split()
        if len(t) < 23 or not t[0].isdigit():
            continue
        st, co = t[0].zfill(2), t[1].zfill(3)
        pid = st if co == "000" else st + co
        if st == "00":
            pid = "US"
        out[pid] = {"poverty": num(t[5]), "income": num(t[20])}
    return out


def mortgage_rate():
    """Latest weekly 30-year fixed rate (Freddie Mac via FRED) and its monthly history."""
    data = fetch("https://fred.stlouisfed.org/graph/fredgraph.csv?id=MORTGAGE30US", "fred_mortgage.csv")
    monthly = {}
    for row in list(rows(data))[1:]:
        if len(row) == 2 and (v := num(row[1])) is not None:
            monthly[row[0][:7]] = v
    last_day = list(rows(data))[-1][0]
    return monthly, monthly[max(monthly)], last_day


# ---------- metric math ----------
def latest(series):
    if not series:
        return None, None
    k = max(series)
    return k, series[k]


def months_back(ym, n):
    y, m = int(ym[:4]), int(ym[5:])
    m -= n
    while m <= 0:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}"


def change(series, n):
    """% change over n months, from the latest month."""
    k, v = latest(series)
    if k is None:
        return None
    old = series.get(months_back(k, n))
    return (v / old - 1) * 100 if old else None


def trend_gap(series):
    """How far today's value sits above (+) or below (-) its 2000-2019 long-term trend line (log-linear fit)."""
    pts = [(i, math.log(v)) for i, (k, v) in enumerate(sorted(series.items())) if k < "2020-01" and v > 0]
    if len(pts) < 120:
        return None
    n = len(pts)
    mx, my = sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    slope = sum((p[0] - mx) * (p[1] - my) for p in pts) / sxx
    i_now = len(series) - 1
    fair = math.exp(my + slope * (i_now - mx))
    return (latest(series)[1] / fair - 1) * 100


def window_extreme(series, start, end, fn):
    vals = [v for k, v in series.items() if start <= k <= end]
    return fn(vals) if vals else None


def payment(price, rate):
    """Monthly principal + interest: 30-year fixed, 20% down."""
    loan, r = price * 0.8, rate / 100 / 12
    return loan * r / (1 - (1 + r) ** -360)


def vs_prepandemic(series):
    """Latest value vs the same calendar month's 2017-2019 average, in %."""
    k, v = latest(series)
    if k is None:
        return None
    base = [series.get(f"{y}-{k[5:]}") for y in (2017, 2018, 2019)]
    base = [b for b in base if b]
    return (v / (sum(base) / len(base)) - 1) * 100 if base else None


def rnd(v, digits):
    if v is None or not math.isfinite(v):
        return None
    return round(v, digits) if digits else int(round(v))


def metrics_for(z, zr, rt, inc, pop, rate, forecast=None):
    """All map metrics for one place. z/zr: Zillow value/rent series, rt: Realtor.com series, inc/pop: Census dicts."""
    m = {}
    hv_k, hv = latest(z or {})
    rent_k, rent = latest(zr or {})
    income = (inc or {}).get("income")
    if hv:
        m["hv"] = rnd(hv, 0)
        m["hv_mom"] = rnd(change(z, 1), 2)
        m["hv_yoy"] = rnd(change(z, 12), 1)
        m["hv_5y"] = rnd(change(z, 60), 1)
        m["overvalued"] = rnd(trend_gap(z), 1)
        peak22 = window_extreme(z, "2022-01", "2022-12", max)
        m["from_2022_peak"] = rnd((hv / peak22 - 1) * 100, 1) if peak22 else None
        peak = window_extreme(z, "2005-01", "2008-12", max)
        trough = window_extreme(z, "2008-01", "2013-12", min)
        m["crash_2007"] = rnd((trough / peak - 1) * 100, 1) if peak and trough else None
        pmt = payment(hv, rate)
        m["mtg_pmt"] = rnd(pmt, 0)
        if income:
            m["hv_income"] = rnd(hv / income, 1)
            m["mtg_pct_income"] = rnd(pmt * 12 / income * 100, 1)
        m["salary_needed"] = rnd(pmt * 12 / 0.30, 0)  # payment at 30% of gross income
    if forecast is not None:
        m["fc_1y"] = rnd(forecast, 1)
    if rent:
        m["rent"] = rnd(rent, 0)
        m["rent_yoy"] = rnd(change(zr, 12), 1)
        if income:
            m["rent_pct_income"] = rnd(rent * 12 / income * 100, 1)
        if hv:
            m["price_rent"] = rnd(hv / (rent * 12), 1)
            m["gross_yield"] = rnd(rent * 12 / hv * 100, 2)
            m["buy_vs_rent"] = rnd(payment(hv, rate) - rent, 0)
    if rt:
        for k in ("inv", "dom", "newl", "lp"):
            kk, v = latest(rt[k])
            if v is not None:
                m[k] = rnd(v, 0)
                m[k + "_yoy"] = rnd(change(rt[k], 12), 1)
        kk, v = latest(rt["cut"])
        if v is not None:
            m["cut"] = rnd(v * 100, 1)
        m["inv_vs_2019"] = rnd(vs_prepandemic(rt["inv"]), 1)
        _, pend = latest(rt["pend"])
        _, inv = latest(rt["inv"])
        if pend and inv:
            m["months_supply"] = rnd(inv / pend, 1)  # rough: active listings per pending sale
    if inc:
        m["income"] = rnd(income, 0)
        m["poverty"] = rnd(inc.get("poverty"), 1)
    if pop and pop.get("pop"):
        p = pop["pop"]
        m["pop"] = rnd(p, 0)
        if pop.get("pop_prev"):
            m["pop_growth"] = rnd((p / pop["pop_prev"] - 1) * 100, 2)
        if pop.get("pop_2020"):
            m["pop_growth_5y"] = rnd((p / pop["pop_2020"] - 1) * 100, 1)
        for k in ("births", "deaths", "natural", "dom_mig", "intl_mig"):
            if pop.get(k) is not None:
                m[k + "_pct"] = rnd(pop[k] / p * 100, 2)
    return {k: v for k, v in m.items() if v is not None}


def trim_series(s, start):
    return {k: (round(v) if v >= 1000 else round(v, 4)) for k, v in sorted(s.items()) if k >= start}


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))


# ---------- main ----------
def main():
    print("Census: metros, population, income")
    metro_names, county_to_cbsa = census_metros()
    pop = census_population()
    inc = census_income()
    print("FRED: mortgage rate")
    rate_hist, rate, rate_day = mortgage_rate()

    print("Zillow")
    zv = {lvl: zillow(f"zhvi/{name}_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv", lvl)
          for lvl, name in (("county", "County"), ("metro", "Metro"), ("state", "State"))}
    zr = {lvl: zillow(f"zori/{name}_zori_uc_sfrcondomfr_sm_month.csv", lvl)
          for lvl, name in (("county", "County"), ("metro", "Metro"))}
    fc_raw = zillow("zhvf_growth/Metro_zhvf_growth_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv", "metro")
    print("Realtor.com")
    rt = {lvl: realtor(lvl) for lvl in ("county", "metro", "state")}

    # Zillow names metros like "Los Angeles, CA"; the Census says "Los Angeles-Long Beach-Anaheim, CA".
    # Match on (first city, first state); where the Census renamed a metro, fall back to Zillow's own
    # county-to-metro column (counties vote for the Census metro they belong to).
    def short_key(name):
        city, _, st = name.rpartition(", ")
        return (city.split("-")[0].strip().lower(), st.split("-")[0].strip().upper())
    key_to_cbsa = {short_key(n): c for c, n in metro_names.items()}
    votes = {}
    for fips, rec in zv["county"].items():
        zname = rec["row"].get("Metro")
        if zname and fips in county_to_cbsa:
            v = votes.setdefault(short_key(zname), {})
            v[county_to_cbsa[fips]] = v.get(county_to_cbsa[fips], 0) + 1
    for k, v in votes.items():
        key_to_cbsa.setdefault(k, max(v, key=v.get))

    def zmetro(src):
        out = {}
        for zname, rec in src.items():
            cbsa = key_to_cbsa.get(short_key(zname)) if ", " in zname else None
            if cbsa and cbsa not in out:
                out[cbsa] = rec
        return out
    zv["metro"], zr["metro"] = zmetro(zv["metro"]), zmetro(zr["metro"])
    forecast = {}
    for cbsa, rec in zmetro(fc_raw).items():
        s = rec["series"]
        if s:
            forecast[cbsa] = s[max(s)]  # the last column is the 12-month-ahead forecast

    # Metro income/poverty/population: population-weighted from their counties.
    metro_counties = {}
    for c, m in county_to_cbsa.items():
        metro_counties.setdefault(m, []).append(c)

    def combine(cbsa):
        cs = metro_counties.get(cbsa, [])
        ps = [(pop.get(c, {}).get("pop") or 0, c) for c in cs]
        total = sum(p for p, _ in ps)
        if not total:
            return None, None
        agg_pop = {k: sum((pop.get(c, {}).get(k) or 0) for c in cs) for k in ("pop", "pop_prev", "pop_2020", "births", "deaths", "natural", "dom_mig", "intl_mig")}
        wi = [(p, inc[c]) for p, c in ps if c in inc and inc[c].get("income")]
        wt = sum(p for p, _ in wi)
        agg_inc = {"income": sum(p * i["income"] for p, i in wi) / wt, "poverty": sum(p * (i["poverty"] or 0) for p, i in wi) / wt} if wt else None
        return agg_inc, agg_pop

    places = {"state": {}, "metro": {}, "county": {}}
    hist = {"state": {}, "metro": {}, "county": {}}
    HSTART = "2000-01"

    for fips, (abbr, name) in STATES.items():
        m = metrics_for(zv["state"].get(fips, {}).get("series"), None, rt["state"].get(fips), inc.get(fips), pop.get(fips), rate)
        places["state"][fips] = {"n": name, "s": abbr, "v": m}
        hist["state"].setdefault("all", {})[fips] = series_bundle(zv["state"].get(fips), None, rt["state"].get(fips), HSTART)

    for cbsa, name in metro_names.items():
        ai, ap = combine(cbsa)
        m = metrics_for(zv["metro"].get(cbsa, {}).get("series"), zr["metro"].get(cbsa, {}).get("series"), rt["metro"].get(cbsa), ai, ap, rate, forecast.get(cbsa))
        if not m:
            continue
        st = name.rsplit(", ", 1)[-1].split("-")[0]
        places["metro"][cbsa] = {"n": name, "s": st, "v": m, "c": sorted(metro_counties.get(cbsa, []))}
        hist["metro"].setdefault(st, {})[cbsa] = series_bundle(zv["metro"].get(cbsa), zr["metro"].get(cbsa), rt["metro"].get(cbsa), HSTART)

    county_ids = set(zv["county"]) | set(rt["county"]) | {k for k in pop if len(k) == 5}
    for fips in sorted(county_ids):
        if fips[:2] not in STATES:
            continue
        cbsa = county_to_cbsa.get(fips)
        m = metrics_for(zv["county"].get(fips, {}).get("series"), zr["county"].get(fips, {}).get("series"), rt["county"].get(fips),
                        inc.get(fips), pop.get(fips), rate, forecast.get(cbsa))
        if not m:
            continue
        name = county_name(fips, zv, rt, pop)
        places["county"][fips] = {"n": name, "s": STATES[fips[:2]][0], "v": m, **({"m": cbsa} if cbsa else {})}
        hist["county"].setdefault(STATES[fips[:2]][0], {})[fips] = series_bundle(zv["county"].get(fips), zr["county"].get(fips), rt["county"].get(fips), HSTART)

    us_inc = inc.get("US", {})
    zillow_month = max((max(r["series"]) for r in zv["state"].values() if r["series"]), default=None)
    realtor_month = max((max(r["inv"]) for r in rt["state"].values() if r["inv"]), default=None)
    meta = {
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "months": {"zillow": zillow_month, "realtor": realtor_month, "income": inc["_year"], "population": pop["_year"]},
        "mortgage": {"rate": rate, "date": rate_day, "history": trim_series(rate_hist, "2000-01")},
        "us": {"income": us_inc.get("income"), "poverty": us_inc.get("poverty")},
        "counts": {k: len(v) for k, v in places.items()},
    }
    write_json(os.path.join(OUT, "meta.json"), meta)
    for lvl, ps in places.items():
        write_json(os.path.join(OUT, f"{lvl}.json"), ps)
        for group in hist[lvl].values():  # one small file per place, loaded when you click it
            for pid, bundle in group.items():
                write_json(os.path.join(OUT, "hist", lvl, f"{pid}.json"), bundle)
    print(f"Done: {meta['counts']} - Zillow {zillow_month}, Realtor.com {realtor_month}, mortgage {rate}% ({rate_day})")


def series_bundle(zrec, zrrec, rtrec, start):
    b = {}
    if zrec and zrec.get("series"):
        b["hv"] = trim_series(zrec["series"], start)
    if zrrec and zrrec.get("series"):
        b["rent"] = trim_series(zrrec["series"], start)
    if rtrec:
        for k in ("inv", "dom", "cut", "lp", "newl"):
            if rtrec.get(k):
                b[k] = trim_series(rtrec[k], start)
    return b


def county_name(fips, zv, rt, pop):
    z = zv["county"].get(fips)
    if z:
        return z["name"]
    if pop.get(fips, {}).get("name"):
        return pop[fips]["name"]
    return f"County {fips}"


if __name__ == "__main__":
    sys.exit(main())
