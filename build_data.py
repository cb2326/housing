"""Housing Map - data builder.

Downloads free public housing data and writes small JSON files the map reads:
  site/data/meta.json                 when it was built, latest months/years, mortgage rate
  site/data/{state,metro,county}.json latest value of every data point for every place
  site/data/hist/{level}/{id}.json    history for the charts (monthly series + yearly data points)
  site/data/zip/...                   the same for ~30,000 ZIP codes, one file per state

Sources (no accounts needed; the Census API key is optional but unlocks the American Community Survey):
  Zillow Research      home values (all homes, single-family, condos), rents, 1-year forecast
  Realtor.com          listings, pending sales, days on market, price cuts, new listings, listing prices
  U.S. Census Bureau   population, births/deaths/migration, age (Population Estimates); income + poverty (SAIPE);
                       housing units; building permits; land area; American Community Survey (with a key)
  CFPB / FFIEC HMDA    every home-purchase mortgage application in the US (Mortgage Quality data points)
  NOAA                 average temperature by county
  MIT Election Lab     county presidential results (via the tonmcg/US_County_Level_Election_Results_08-24 copy)
  Freddie Mac          30-year mortgage rate

Things that change once a year (mortgage data, Census survey answers, Zillow forecasts so far) are kept in
data_cache/, which the GitHub workflow saves back into the repository, so they are only worked out once.

Runs on GitHub (see .github/workflows/update.yml) twice a month, or locally:  python build_data.py
Only uses the Python standard library.
"""
import csv
import gzip
import io
import json
import math
import os
import re
import sys
import time
import urllib.request
import zipfile
from collections import Counter
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "site", "data")
CACHE = os.path.join(HERE, ".cache")  # downloads are reused for 20 hours when testing locally
DCACHE = os.path.join(HERE, "data_cache")  # yearly results, saved in the repository by the workflow
UA = {"User-Agent": "Mozilla/5.0 (compatible; HousingMapBuilder/1.0; personal non-commercial use)"}
PLAIN = {"User-Agent": "HousingMapBuilder/1.0"}
CENSUS_KEY = os.environ.get("CENSUS_API_KEY", "").strip()
HMDA_YEARS_PER_RUN = int(os.environ.get("HMDA_YEARS_PER_RUN", "4"))
csv.field_size_limit(10_000_000)

ZILLOW = "https://files.zillowstatic.com/research/public_csvs"
REALTOR = "https://econdata.s3-us-west-2.amazonaws.com/Reports/Core"
CENSUS = "https://www2.census.gov/programs-surveys"
THIS_YEAR = date.today().year

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
def fetch(url, name=None, headers=None, timeout=180, tries=3):
    """Download a file (cached for 20 hours under .cache/ so local test runs are quick). Returns bytes."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name or url.rsplit("/", 1)[-1])
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 20 * 3600:
        with open(path, "rb") as f:
            return f.read()
    last = None
    for attempt in range(tries):
        try:
            print(f"  downloading {url.split('&key=')[0]}", flush=True)
            data = urllib.request.urlopen(urllib.request.Request(url, headers=headers or UA), timeout=timeout).read()
            if url.endswith(".gz") or data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            with open(path, "wb") as f:
                f.write(data)
            return data
        except Exception as e:  # network hiccup: try again (but a missing file stays missing)
            last = e
            if getattr(e, "code", None) in (400, 404):
                break
            time.sleep(5 * (attempt + 1))
    raise last


def fetch_first(urls, name):
    """The first of several URLs that works (e.g. this year's Census file, else last year's)."""
    for url in urls:
        try:
            return fetch(url, name), url
        except Exception as e:
            print(f"  (not available: {url.split('&key=')[0]} - {e})")
    return None, None


def try_fetch(url, name, headers=None):
    try:
        return fetch(url, name, headers)
    except Exception as e:
        print(f"  (skipped {url.split('&key=')[0]}: {e})")
        return None


def rows(data, encoding="utf-8"):
    return csv.reader(io.StringIO(data.decode(encoding, "replace")))


def num(s):
    try:
        v = float(s)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def load_dcache(name):
    path = os.path.join(DCACHE, name)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))


def save_dcache(name, obj):
    write_json(os.path.join(DCACHE, name), obj)


# ---------- Zillow (wide CSVs: one column per month) ----------
def zillow(path, level):
    """{place id: {"name":..., "series": {"YYYY-MM": value}}} for one Zillow file ({} if Zillow doesn't publish it)."""
    data = try_fetch(f"{ZILLOW}/{path}", f"zillow_{path.replace('/', '_')}")
    if not data:
        return {}
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
    "ppsf": "median_listing_price_per_square_foot", "sqft": "median_square_feet",
    "inc": "price_increased_share",
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


# ---------- Census: metros ----------
def census_metros():
    """County -> metro (CBSA code) list and metro names (Census Population Estimates, metro file)."""
    urls = [f"{CENSUS}/popest/datasets/2020-{y}/metro/totals/cbsa-est{y}-alldata.csv" for y in range(THIS_YEAR, THIS_YEAR - 4, -1)]
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


# ---------- yearly data, collected as raw numbers per county (and state) so metros can be added up ----------
# R[pid][field][year] = value.  pid = 2-digit state FIPS or 5-digit county FIPS.
def rput(R, pid, field, year, v):
    if v is not None:
        R.setdefault(pid, {}).setdefault(field, {})[year] = v


def census_population(R):
    """Population, births, deaths and migration for every year since 2010 (two Census vintages). Returns the latest year."""
    old = try_fetch(f"{CENSUS}/popest/datasets/2010-2019/counties/totals/co-est2019-alldata.csv", "census_pop_2010s.csv")
    new, _ = fetch_first([f"{CENSUS}/popest/datasets/2020-{y}/counties/totals/co-est{y}-alldata.csv" for y in range(THIS_YEAR, THIS_YEAR - 4, -1)], "census_pop.csv")
    latest = None
    names = {}
    for data, first in ((old, 2010), (new, 2020)):
        if not data:
            continue
        r = rows(data, "latin-1")
        head = next(r)
        col = {h: i for i, h in enumerate(head)}
        last = max(int(h[-4:]) for h in head if h.startswith("POPESTIMATE") and h[-4:].isdigit())
        if first == 2020:
            latest = last
        for row in r:
            if not row:
                continue
            sumlev, st, co = row[col["SUMLEV"]], row[col["STATE"]].zfill(2), row[col["COUNTY"]].zfill(3)
            if st not in STATES or sumlev not in ("040", "050"):
                continue
            pid = st if sumlev == "040" else st + co
            names[pid] = row[col["CTYNAME"]]
            g = lambda name: num(row[col[name]]) if name in col else None
            for y in range(first, last + 1):
                if first == 2010 and y >= 2020:
                    continue
                rput(R, pid, "pop", y, g(f"POPESTIMATE{y}"))
                if y in (2010, 2020):
                    continue  # those years' births etc. only cover a few months
                rput(R, pid, "births", y, g(f"BIRTHS{y}"))
                rput(R, pid, "deaths", y, g(f"DEATHS{y}"))
                rput(R, pid, "natural", y, g(f"NATURALCHG{y}") if f"NATURALCHG{y}" in col else g(f"NATURALINC{y}"))
                rput(R, pid, "dom_mig", y, g(f"DOMESTICMIG{y}"))
                rput(R, pid, "intl_mig", y, g(f"INTERNATIONALMIG{y}"))
    return latest, names


def census_age(R):
    """Age groups and median age by county for every year since 2010 (Population Estimates by age). Returns the latest year."""
    old = try_fetch(f"{CENSUS}/popest/datasets/2010-2020/counties/asrh/CC-EST2020-AGESEX-ALL.csv", "census_age_2010s.csv")
    new, url = fetch_first([f"{CENSUS}/popest/datasets/2020-{y}/counties/asrh/cc-est{y}-agesex-all.csv" for y in range(THIS_YEAR, THIS_YEAR - 4, -1)], "census_age.csv")
    latest = None
    for data, year_of in ((old, lambda k: 2007 + k if 3 <= k <= 12 else None), (new, lambda k: 2018 + k if k >= 2 else None)):
        if not data:
            continue
        r = rows(data, "latin-1")
        head = next(r)
        col = {h: i for i, h in enumerate(head)}
        for row in r:
            if not row or row[col["SUMLEV"]].zfill(3) != "050":  # the 2010s file writes "50", not "050"
                continue
            y = year_of(int(row[col["YEAR"]]))
            if y is None:
                continue
            pid = row[col["STATE"]].zfill(2) + row[col["COUNTY"]].zfill(3)
            if pid[:2] not in STATES:
                continue
            g = lambda *names: sum(num(row[col[n]]) or 0 for n in names if n in col)
            pop = g("POPESTIMATE")
            if not pop:
                continue
            rput(R, pid, "agepop", y, pop)
            rput(R, pid, "u18", y, g("UNDER5_TOT", "AGE513_TOT", "AGE1417_TOT"))
            rput(R, pid, "a2544", y, g("AGE2544_TOT"))
            rput(R, pid, "a65", y, g("AGE65PLUS_TOT"))
            rput(R, pid, "a75", y, g("AGE7579_TOT", "AGE8084_TOT", "AGE85PLUS_TOT"))
            med = num(row[col["MEDIAN_AGE_TOT"]]) if "MEDIAN_AGE_TOT" in col else None
            if med:
                rput(R, pid, "medage", y, med)
                rput(R, pid, "medage_w", y, med * pop)
            if data is new:
                latest = max(latest or 0, y)
    return latest


def census_income(R):
    """Median household income and poverty rate by county and state for every year since 2005 (Census SAIPE)."""
    latest = None
    for y in range(2005, THIS_YEAR + 1):
        yy = str(y)[2:]
        data = None
        if y < THIS_YEAR - 2 and os.path.exists(os.path.join(CACHE, f"saipe{yy}.txt")):
            data = open(os.path.join(CACHE, f"saipe{yy}.txt"), "rb").read()
        if data is None:
            try:
                data = fetch(f"{CENSUS}/saipe/datasets/{y}/{y}-state-and-county/est{yy}all.txt", f"saipe{yy}.txt")
            except Exception:
                continue
        latest = y
        for line in data.decode("latin-1").splitlines():
            t = line.split()
            if len(t) < 23 or not t[0].isdigit():
                continue
            st, co = t[0].zfill(2), t[1].zfill(3)
            if st not in STATES:
                continue
            pid = st if co == "000" else st + co
            inc, pov = num(t[20]), num(t[5])
            if inc and 5000 < inc < 500000:
                rput(R, pid, "income", y, inc)
            if pov is not None and 0 <= pov <= 100:
                rput(R, pid, "poverty", y, pov)
    return latest


def census_permits(R):
    """New homes authorised by building permits, by county, every year since 2000 (Census Building Permits Survey)."""
    latest = None
    for y in range(2000, THIS_YEAR + 1):
        data = try_fetch(f"https://www2.census.gov/econ/bps/County/co{y}a.txt", f"bps_co{y}.txt")
        if not data:
            continue
        latest = y
        for row in rows(data, "latin-1"):
            if len(row) < 17 or not row[0].strip()[:4].isdigit() or not row[1].strip().isdigit():
                continue
            pid = row[1].strip().zfill(2) + row[2].strip().zfill(3)
            if pid[:2] not in STATES:
                continue
            units = sum(num(row[i]) or 0 for i in (7, 10, 13, 16))
            rput(R, pid, "permits", y, units)
    return latest


def census_housing_units(R):
    """Housing units by county: Census estimates for 2010-2020, then rolled forward with each year's building permits."""
    data = try_fetch(f"{CENSUS}/popest/datasets/2010-2020/housing/HU-EST2020_ALL.csv", "census_hu.csv")
    if not data:
        return None
    r = rows(data, "latin-1")
    head = next(r)
    col = {h: i for i, h in enumerate(head)}
    for row in r:
        if not row or row[col["SUMLEV"]] != "050":
            continue
        pid = row[col["STATE"]].zfill(2) + row[col["COUNTY"]].zfill(3)
        for y in range(2010, 2021):
            rput(R, pid, "units", y, num(row[col[f"HUESTIMATE{y}"]]))
    latest = 2020
    for pid, rec in R.items():
        u, p = rec.get("units"), rec.get("permits", {})
        if not u or 2020 not in u:
            continue
        y = 2021
        while y in p:
            u[y] = u[y - 1] + p[y]
            latest = max(latest, y)
            y += 1
    return latest


def land_area():
    """Land area in square miles by county and ZIP (Census Gazetteer)."""
    out = {}
    for kind in ("counties", "zcta"):
        for y in range(THIS_YEAR, THIS_YEAR - 5, -1):
            data = try_fetch(f"https://www2.census.gov/geo/docs/maps-data/data/gazetteer/{y}_Gazetteer/{y}_Gaz_{kind}_national.zip", f"gaz_{kind}.zip")
            if data:
                break
        if not data:
            continue
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            text = z.read(z.namelist()[0]).decode("latin-1")
        lines = text.splitlines()
        sep = "|" if "|" in lines[0] else "\t"  # tab-separated until 2025, then |
        head = [h.strip() for h in lines[0].split(sep)]
        gi, ai = head.index("GEOID"), head.index("ALAND_SQMI")
        for line in lines[1:]:
            t = line.split(sep)
            if len(t) > ai and (a := num(t[ai])):
                out[("z" if kind == "zcta" else "") + t[gi].strip()] = a  # ZIPs get a "z" (county 06037 ≠ ZIP 06037)
    return out


NOAA_STATES = "AL AZ AR CA CO CT DE FL GA ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()


def noaa_temperature(R):
    """Average yearly temperature (°F) by county and state since 1990 (NOAA climate divisional data)."""
    base = "https://www.ncei.noaa.gov/pub/data/cirs/climdiv/"
    listing = try_fetch(base, "noaa_listing.html")
    if not listing:
        return None
    names = re.findall(r"climdiv-tmpc(?:cy|st)-v[\d.]+-\d{8}", listing.decode("latin-1"))
    latest = None
    for kind in ("cy", "st"):
        fname = max((n for n in names if f"tmpc{kind}" in n), default=None)
        data = fname and try_fetch(base + fname, f"noaa_tmp{kind}.txt")
        if not data:
            continue
        for line in data.decode("latin-1").splitlines():
            if len(line) < 11:
                continue
            code, y = line[:11], int(line[7:11]) if kind == "cy" else int(line[6:10])
            vals = [num(v) for v in line[11:].split()] if kind == "cy" else [num(v) for v in line[10:].split()]
            if y < 1990 or len(vals) < 12 or any(v is None or v < -90 for v in vals[:12]):
                continue  # this year isn't complete yet
            if kind == "cy":
                n = int(code[:2])
                abbr = "AK" if n == 50 else NOAA_STATES[n - 1] if 1 <= n <= 48 else None
                if not abbr:
                    continue
                pid = ABBR_TO_FIPS[abbr] + code[2:5]
            else:
                n = int(line[:3])
                abbr = "AK" if n == 50 else NOAA_STATES[n - 1] if 1 <= n <= 48 else None
                if not abbr or line[3] != "0":
                    continue
                pid = ABBR_TO_FIPS[abbr]
            rput(R, pid, "temp", y, round(sum(vals[:12]) / 12, 1))
            latest = max(latest or 0, y)
    return latest


def election_results(R):
    """Presidential vote by county, 2008-2024."""
    base = "https://raw.githubusercontent.com/tonmcg/US_County_Level_Election_Results_08-24/master/"
    latest = None
    data = try_fetch(base + "US_County_Level_Presidential_Results_08-16.csv", "votes_08_16.csv")
    if data:
        r = rows(data)
        head = next(r)
        col = {h: i for i, h in enumerate(head)}
        for row in r:
            if not row:
                continue
            pid = row[col["fips_code"]].zfill(5)
            for y in (2008, 2012, 2016):
                t, d, g = (num(row[col[f"{k}_{y}"]]) for k in ("total", "dem", "gop"))
                if t:
                    rput(R, pid, "votes_t", y, t); rput(R, pid, "votes_d", y, d); rput(R, pid, "votes_r", y, g)
                    latest = y
    for y in range(2020, THIS_YEAR + 1, 4):
        data = try_fetch(base + f"{y}_US_County_Level_Presidential_Results.csv", f"votes_{y}.csv")
        if not data:
            continue
        r = rows(data)
        head = next(r)
        col = {h: i for i, h in enumerate(head)}
        for row in r:
            if not row:
                continue
            pid = row[col["county_fips"]].zfill(5)
            t = num(row[col["total_votes"]])
            if t:
                rput(R, pid, "votes_t", y, t); rput(R, pid, "votes_d", y, num(row[col["votes_dem"]])); rput(R, pid, "votes_r", y, num(row[col["votes_gop"]]))
                latest = y
    return latest


# ---------- American Community Survey (needs a free Census API key) ----------
ACS_VARS = {
    "units": "B25001_001E", "occ": "B25003_001E", "own": "B25003_002E", "rentocc": "B25003_003E",
    "mtg_tot": "B25081_001E", "mtg_yes": "B25081_002E",
    "hh": "B11001_001E", "fam": "B11001_002E", "alone": "B11001_008E",
    "work": "B08301_001E", "wfh": "B08301_021E",
    "vac_rent": "B25004_002E", "vac_rented": "B25004_003E", "vac_other": "B25004_008E",
    "o_tot": "B25007_002E", "o_2534": "B25007_004E", "o_3544": "B25007_005E", "o_6574": "B25007_009E", "o_7584": "B25007_010E", "o_85": "B25007_011E",
    "r_tot": "B25007_012E", "r_2534": "B25007_014E", "r_3544": "B25007_015E",
    "income": "B19013_001E", "pop": "B01003_001E",
}
# Version 2 added these (unemployment, housing-cost burden, second homes, age of homes, commute, rent by bedrooms).
ACS_V = 2
ACS_MORE = {
    "lf": "B23025_003E", "unemp": "B23025_005E",
    "rb_tot": "B25070_001E", "rb_30": "B25070_007E", "rb_35": "B25070_008E", "rb_40": "B25070_009E", "rb_50": "B25070_010E", "rb_nc": "B25070_011E",
    "obm_tot": "B25091_002E", "obm_30": "B25091_008E", "obm_35": "B25091_009E", "obm_40": "B25091_010E", "obm_50": "B25091_011E", "obm_nc": "B25091_012E",
    "obn_tot": "B25091_013E", "obn_30": "B25091_019E", "obn_35": "B25091_020E", "obn_40": "B25091_021E", "obn_50": "B25091_022E", "obn_nc": "B25091_023E",
    "units2": "B25001_001E", "seasonal": "B25004_006E", "yrbuilt": "B25035_001E",
    "tt_agg": "B08013_001E", "w_tot": "B08301_001E", "w_home": "B08301_021E",
    "rent_med": "B25064_001E", "rent3": "B25031_005E", "rent4": "B25031_006E",
}


def acs_derive_more(v):
    g = v.get
    s = lambda *ks: sum(g(k) or 0 for k in ks) if all(g(k) is not None for k in ks) else None
    def pct(a, b):
        return a / b * 100 if a is not None and b else None
    m = {
        "unemployment": pct(g("unemp"), g("lf")),
        "rent_burden": pct(s("rb_30", "rb_35", "rb_40", "rb_50"), (g("rb_tot") or 0) - (g("rb_nc") or 0)),
        "owner_burden": pct(s("obm_30", "obm_35", "obm_40", "obm_50", "obn_30", "obn_35", "obn_40", "obn_50"),
                            (g("obm_tot") or 0) - (g("obm_nc") or 0) + (g("obn_tot") or 0) - (g("obn_nc") or 0)),
        "seasonal_pct": pct(g("seasonal"), g("units2")),
        "year_built": g("yrbuilt") if g("yrbuilt") and 1900 < g("yrbuilt") < 2100 else None,
        "commute": g("tt_agg") / (g("w_tot") - (g("w_home") or 0)) if g("tt_agg") and g("w_tot") and g("w_tot") > (g("w_home") or 0) else None,
        "rent_census": g("rent_med"), "rent_3br": g("rent3"), "rent_4br": g("rent4"),
    }
    return {k: round(x, 2) for k, x in m.items() if x is not None and math.isfinite(x)}


ACS_EXTRA = {"tax": "B25103_001E", "value": "B25077_001E", "edu": "B15003_001E", "ba": "B15003_022E", "ma": "B15003_023E", "prof": "B15003_024E", "phd": "B15003_025E"}
# Age groups for ZIP codes (counties and states get these from the yearly population estimates instead).
_AGE_CELLS = [3, 4, 5, 6, 11, 12, 13, 14, 20, 21, 22, 23, 24, 25, 27, 28, 29, 30, 35, 36, 37, 38, 44, 45, 46, 47, 48, 49]
ACS_AGE = {"agepop": "B01001_001E", "medage": "B01002_001E", **{f"c{n}": f"B01001_{n:03d}E" for n in _AGE_CELLS}}
ACS_GEOS = {"state": "state:*", "county": "county:*", "metro": "metropolitan%20statistical%20area/micropolitan%20statistical%20area:*", "zip": "zip%20code%20tabulation%20area:*"}


def acs_call(year, geo, varmap):
    names = list(varmap)
    url = f"https://api.census.gov/data/{year}/acs/acs5?get={','.join(varmap[n] for n in names)}&for={ACS_GEOS[geo]}&key={CENSUS_KEY}"
    data = fetch(url, f"acs_{year}_{geo}_{len(names)}.json", PLAIN, timeout=240, tries=2)
    table = json.loads(data)
    head = table[0]
    out = {}
    for row in table[1:]:
        if geo == "county":
            pid = row[head.index("state")] + row[head.index("county")]
        else:
            pid = row[-1]
        vals = {}
        for i, n in enumerate(names):
            v = num(row[i])
            if v is not None and v >= 0:  # the Census marks missing values with big negative numbers
                vals[n] = v
        out[pid] = vals
    return out


def acs_derive(v):
    """Survey counts -> data points (all in %, except the two ratios)."""
    def pct(a, b):
        return a / b * 100 if a is not None and b else None
    g = v.get
    s = lambda *ks: sum(g(k) or 0 for k in ks) if all(g(k) is not None for k in ks) else None
    m = {
        "college": pct(s("ba", "ma", "prof", "phd"), g("edu")),
        "remote": pct(g("wfh"), g("work")),
        "homeown": pct(g("own"), g("occ")),
        "mortgaged": pct(g("mtg_yes"), g("mtg_tot")),
        "family_hh": pct(g("fam"), g("hh")),
        "single_hh": pct(g("alone"), g("hh")),
        "vacancy": pct(g("vac_rent"), s("rentocc", "vac_rent", "vac_rented")),
        "shadow": pct(g("vac_other"), g("units")),
        "prop_tax_rate": pct(g("tax"), g("value")),
        "own_2544_pct": pct(s("o_2534", "o_3544"), g("o_tot")),
        "own_65_pct": pct(s("o_6574", "o_7584", "o_85"), g("o_tot")),
        "own_75_pct": pct(s("o_7584", "o_85"), g("o_tot")),
        "rent_2544_pct": pct(s("r_2534", "r_3544"), g("r_tot")),
    }
    young, old65, old75 = s("r_2534", "r_3544"), s("o_6574", "o_7584", "o_85"), s("o_7584", "o_85")
    m["r_rent2544_own65"] = young / old65 if young is not None and old65 else None
    m["r_rent2544_own75"] = young / old75 if young is not None and old75 else None
    m["acs_income"] = g("income")
    m["acs_pop"] = g("pop")
    m["acs_units"] = g("units")
    p = g("agepop")
    if p and g("c3") is not None:
        cells = lambda ns: sum(g(f"c{n}") or 0 for n in ns)
        u18, a2544 = cells((3, 4, 5, 6, 27, 28, 29, 30)), cells((11, 12, 13, 14, 35, 36, 37, 38))
        a65, a75 = cells((20, 21, 22, 23, 24, 25, 44, 45, 46, 47, 48, 49)), cells((23, 24, 25, 47, 48, 49))
        m.update({"u18_pct": u18 / p * 100, "a2544_pct": a2544 / p * 100, "a65_pct": a65 / p * 100, "a75_pct": a75 / p * 100,
                  "med_age": g("medage")})
        if a65:
            m["r_u18_65"], m["r_2544_65"] = u18 / a65, a2544 / a65
        if a75:
            m["r_u18_75"], m["r_2544_75"] = u18 / a75, a2544 / a75
    return {k: round(x, 3) for k, x in m.items() if x is not None and math.isfinite(x)}


def acs_years():
    """Data points from the American Community Survey (5-year), for every year since 2010; ZIP codes since 2020.
    Each year is saved in data_cache/acs/ so it is only downloaded once."""
    if not CENSUS_KEY:
        print("  (no CENSUS_API_KEY set: only American Community Survey years saved earlier are used)")
    out, latest = {}, None
    for y in range(2010, THIS_YEAR):
        cached = load_dcache(f"acs/{y}.json")
        if cached is not None and cached.get("v", 1) < ACS_V and CENSUS_KEY:
            # saved before version 2: fetch only the newer questions and add them
            for geo, recs in cached.items():
                if geo == "v":
                    continue
                try:
                    more = acs_call(y, geo, ACS_MORE)
                except Exception as e:
                    print(f"  (ACS {y} {geo} extra questions not available: {e})")
                    continue
                for pid, vals in more.items():
                    d = acs_derive_more(vals)
                    if d and pid in recs:
                        recs[pid].update(d)
            cached["v"] = ACS_V
            save_dcache(f"acs/{y}.json", cached)
        if cached is None:
            if not CENSUS_KEY:
                continue
            res = {}
            for geo in ("state", "county", "metro", "zip"):
                if geo == "zip" and y < 2020:
                    continue
                try:
                    base = acs_call(y, geo, ACS_VARS)
                except Exception as e:
                    print(f"  (ACS {y} {geo} not available: {e})")
                    continue
                try:
                    extra = acs_call(y, geo, ACS_EXTRA if y >= 2012 else {k: v for k, v in ACS_EXTRA.items() if k in ("tax", "value")})
                except Exception:
                    extra = {}
                try:
                    more = acs_call(y, geo, ACS_MORE)
                except Exception:
                    more = {}
                if geo == "zip":
                    try:
                        for pid, vals in acs_call(y, geo, ACS_AGE).items():
                            extra.setdefault(pid, {}).update(vals)
                    except Exception as e:
                        print(f"  (ACS {y} ZIP ages not available: {e})")
                res[geo] = {pid: d for pid, vals in base.items() if (d := {**acs_derive({**vals, **extra.get(pid, {})}), **acs_derive_more(more.get(pid, {}))})}
            if not res.get("county"):
                continue  # not published yet
            res["v"] = ACS_V
            save_dcache(f"acs/{y}.json", res)
            cached = res
        out[y] = cached
        latest = y
    return out, latest


# ---------- HMDA: every home-purchase mortgage application (yearly, published each spring) ----------
def dti_number(s):
    s = s.strip()
    table = {"<20%": 15, "20%-<30%": 25, "30%-<36%": 33, "50%-60%": 55, ">60%": 65}
    if s in table:
        return table[s]
    return num(s)


def median_of(counter):
    total = sum(counter.values())
    if not total:
        return None
    half, run = total / 2, 0
    for k in sorted(counter):
        run += counter[k]
        if run >= half:
            return k
    return None


class Loans:
    __slots__ = ("act", "lt", "occ", "rate", "rate_n", "dti43", "dti_n", "ltv", "dti", "inc", "amt", "val")

    def __init__(self):
        self.act = [0] * 6
        self.lt = [0] * 5
        self.occ = [0] * 4
        self.rate = self.rate_n = self.dti43 = self.dti_n = 0
        self.ltv, self.dti, self.inc, self.amt, self.val = Counter(), Counter(), Counter(), Counter(), Counter()

    def result(self):
        a = self.act
        orig = a[1]
        m = {"apps": sum(a[1:6]), "origs": orig}
        decided = a[1] + a[2] + a[3]
        if decided >= 10:
            m["denial"] = a[3] / decided * 100
        if orig >= 10:
            m.update({"conv_pct": self.lt[1] / orig * 100, "fha_pct": self.lt[2] / orig * 100, "va_pct": self.lt[3] / orig * 100,
                      "usda_pct": self.lt[4] / orig * 100, "govt_pct": sum(self.lt[2:5]) / orig * 100,
                      "second_pct": self.occ[2] / orig * 100, "investor_pct": self.occ[3] / orig * 100, "si_pct": (self.occ[2] + self.occ[3]) / orig * 100})
            if self.rate_n:
                m["avg_rate"] = self.rate / self.rate_n
            if self.dti_n:
                m["dti43"] = self.dti43 / self.dti_n * 100
            for k, c, mult in (("ltv", self.ltv, 1), ("dti", self.dti, 1), ("buyer_income", self.inc, 1000), ("loan_amt", self.amt, 1), ("price_med", self.val, 1)):
                med = median_of(c)
                if med is not None:
                    m[k] = med * mult
        return {k: round(v, 3) for k, v in m.items()}


def hmda_year(y, county_to_cbsa):
    """Stream one year of purchase-mortgage applications (about 2 GB) and add them up by county, metro and state."""
    url = f"https://ffiec.cfpb.gov/v2/data-browser-api/view/nationwide/csv?years={y}&actions_taken=1,2,3,4,5&loan_purposes=1"
    print(f"  streaming HMDA {y} (takes a few minutes)", flush=True)
    resp = io.BufferedReader(urllib.request.urlopen(urllib.request.Request(url, headers=PLAIN), timeout=600), 1 << 20)
    stream = gzip.GzipFile(fileobj=resp) if resp.peek(2)[:2] == b"\x1f\x8b" else resp
    text = io.TextIOWrapper(stream, encoding="utf-8", errors="replace", newline="")
    r = csv.reader(text)
    head = next(r)
    c = {h: i for i, h in enumerate(head)}
    ci, si, ai, li, oi, lni = c["county_code"], c["state_code"], c["action_taken"], c["loan_type"], c["occupancy_type"], c["lien_status"]
    ri, vi, di, ii, mi_, pi = c["interest_rate"], c["combined_loan_to_value_ratio"], c["debt_to_income_ratio"], c["income"], c["loan_amount"], c["property_value"]
    buckets = {"county": {}, "metro": {}, "state": {}}
    n = 0
    for row in r:
        if len(row) < len(head) or row[lni] != "1":
            continue
        n += 1
        county = row[ci]
        st = ABBR_TO_FIPS.get(row[si])
        targets = []
        if st:
            targets.append(buckets["state"].setdefault(st, Loans()))
        if len(county) == 5 and county.isdigit():
            targets.append(buckets["county"].setdefault(county, Loans()))
            if county in county_to_cbsa:
                targets.append(buckets["metro"].setdefault(county_to_cbsa[county], Loans()))
        if not targets:
            continue
        act = int(row[ai]) if row[ai].isdigit() else 0
        if not 1 <= act <= 5:
            continue
        for b in targets:
            b.act[act] += 1
        if act != 1:
            continue
        lt, occ = row[li], row[oi]
        rate, ltv, dti, inc, amt, val = num(row[ri]), num(row[vi]), dti_number(row[di]), num(row[ii]), num(row[mi_]), num(row[pi])
        for b in targets:
            if lt in ("1", "2", "3", "4"):
                b.lt[int(lt)] += 1
            if occ in ("1", "2", "3"):
                b.occ[int(occ)] += 1
            if rate is not None and 1 <= rate <= 15:
                b.rate += rate
                b.rate_n += 1
            if ltv is not None and 1 <= ltv <= 150:
                b.ltv[round(ltv)] += 1
            if dti is not None:
                b.dti[round(dti)] += 1
                b.dti_n += 1
                if dti > 43:
                    b.dti43 += 1
            if inc is not None and 0 < inc < 10000:
                b.inc[round(inc)] += 1
            if amt:
                b.amt[round(amt / 5000) * 5000] += 1
            if val:
                b.val[round(val / 5000) * 5000] += 1
    print(f"  HMDA {y}: {n:,} first-lien purchase applications", flush=True)
    return {lvl: {pid: b.result() for pid, b in bs.items()} for lvl, bs in buckets.items()}


def hmda_available(y):
    try:
        data = urllib.request.urlopen(urllib.request.Request(
            f"https://ffiec.cfpb.gov/v2/data-browser-api/view/nationwide/aggregations?years={y}&actions_taken=1&loan_purposes=1", headers=PLAIN), timeout=60).read()
        return json.loads(data)["aggregations"][0]["count"] > 100000
    except Exception:
        return False


def hmda_years(county_to_cbsa):
    """Every year of mortgage data since 2018 (when the current format began). New years are worked out at most
    HMDA_YEARS_PER_RUN at a time (newest first) and saved in data_cache/hmda/."""
    out, todo = {}, []
    for y in range(THIS_YEAR, 2017, -1):
        cached = load_dcache(f"hmda/{y}.json")
        if cached:
            out[y] = cached
        elif y >= THIS_YEAR - 2 and not hmda_available(y):
            continue
        else:
            todo.append(y)
    for y in todo[:HMDA_YEARS_PER_RUN]:
        try:
            res = hmda_year(y, county_to_cbsa)
        except Exception as e:
            print(f"  (HMDA {y} failed: {e})")
            continue
        save_dcache(f"hmda/{y}.json", res)
        out[y] = res
    return out, max(out, default=None)


# ---------- schools: state test results (U.S. Department of Education, EDFacts) + the NCES school list ----------
# School-level results as published by the Department of Education, from the Education Data Center's public archive
# of the EDFacts files. Years are the spring of the school year (2022 = 2021-22). 2020 and 2021 are left out: testing
# was cancelled or patchy during the pandemic.
EDFACTS = "https://storage.googleapis.com/edc-education-exports/edfacts/"
SCHOOL_FILES = {
    2017: ("math-achievement-sch-sy2016-17.csv", "rla-achievement-sch-sy2016-17.csv"),
    2018: ("math-achievement-sch-sy2017-18.csv", "rla-achievement-sch-sy2017-18.csv"),
    2019: ("math-achievement-sch-sy2018-19-long.zip", "rla-achievement-sch-sy2018-19-long.zip"),
    2022: ("EDFacts_2022_School_math.csv", "EDFacts_2022_School_ela.csv"),
}


def pct_mid(s):
    """'45', '40-44', 'GE50', '<=5%', 'PS' (hidden for privacy) -> a number of percent (the middle of a range)."""
    s = (s or "").strip().replace("%", "").upper()
    if not s or s in ("PS", "N/A", "NA", ".", "-"):
        return None
    for pre, f in ((">=", lambda x: (x + 100) / 2), ("GE", lambda x: (x + 100) / 2), (">", lambda x: (x + 100) / 2), ("GT", lambda x: (x + 100) / 2),
                   ("<=", lambda x: x / 2), ("LE", lambda x: x / 2), ("<", lambda x: x / 2), ("LT", lambda x: x / 2)):
        if s.startswith(pre):
            x = num(s[len(pre):])
            return f(x) if x is not None else None
    if "-" in s:
        a, b = (num(t) for t in s.split("-", 1))
        return (a + b) / 2 if a is not None and b is not None else None
    return num(s)


def school_results(year):
    """{NCES school id: share of students proficient (math and reading together)} for one year."""
    acc = {}
    for fname in SCHOOL_FILES[year]:
        path = fetch_file(EDFACTS + fname, "edf_" + fname)
        if fname.endswith(".zip"):
            zf = zipfile.ZipFile(path)
            fh = io.TextIOWrapper(zf.open(next(n for n in zf.namelist() if n.lower().endswith(".csv"))), encoding="latin-1", newline="")
        else:
            fh = open(path, encoding="latin-1", newline="")
        with fh:
            r = csv.reader(fh)
            head = [h.strip() for h in next(r)]
            c = {h: i for i, h in enumerate(head)}
            if "NCESSCHID" in c:  # 2022 layout: one row per grade and group; many schools have no "All Grades" row,
                # so their grades are added up instead (see `whole` below)
                pick = lambda row: (row[c["NCESSCHID"]].zfill(12), row[c["Denominator"]], row[c["ProficientOrAbove_percent"]],
                                    row[c["AgeGrade"]] == "All Grades") \
                    if row[c["Subgroup"]] == "All Students in School" else None
            elif "CATEGORY" in c:  # 2019 layout: one row per grade and group
                pick = lambda row: (row[c["NCESSCH"]].zfill(12), row[c["NUMVALID"]], row[c["PCTPROF"]]) \
                    if row[c["CATEGORY"]] == "ALL" and row[c["GRADE"]] == "00" else None
            else:  # older layout: one wide row per school
                ni = next(i for h, i in c.items() if h.startswith("ALL_") and "00NUMVALID" in h)
                pi = next(i for h, i in c.items() if h.startswith("ALL_") and "00PCTPROF" in h)
                pick = lambda row: (row[c["NCESSCH"]].zfill(12), row[ni], row[pi])
            whole, by_grade = {}, {}  # this subject: the school's all-grades figure, and its grades added up
            for row in r:
                if len(row) < len(head) - 1:
                    continue
                got = pick(row)
                if not got:
                    continue
                sid, n, p = got[0], num(got[1]), pct_mid(got[2])
                if p is None or not n:
                    continue
                if len(got) == 4 and not got[3]:
                    a = by_grade.setdefault(sid, [0.0, 0.0])
                    a[0] += p * n
                    a[1] += n
                else:
                    whole[sid] = (p * n, n)
            for sid, (s, n) in {**by_grade, **whole}.items():
                if n >= 10:
                    a = acc.setdefault(sid, [0.0, 0.0])
                    a[0] += s
                    a[1] += n
    return {sid: s / n for sid, (s, n) in acc.items() if n}


def school_scores():
    """{year: {school id: 1-10}}: each school's proficiency ranked against the other schools in its state
    (states use different tests, so schools are only compared within their state). Saved in data_cache/schools/."""
    out = {}
    for year in SCHOOL_FILES:
        cached = load_dcache(f"schools/scores_{year}.json")
        if cached is None:
            try:
                res = school_results(year)
            except Exception as e:
                print(f"  (school results {year} failed: {e})")
                continue
            by_state = {}
            for sid, p in res.items():
                by_state.setdefault(sid[:2], {})[sid] = p
            cached = {}
            for items in by_state.values():
                for sid, rank in percentile_ranks(items).items():
                    cached[sid] = min(10, 1 + int(rank * 10))
            save_dcache(f"schools/scores_{year}.json", cached)
            print(f"  school scores {year}: {len(cached):,} schools")
        out[year] = cached
    return out


def school_directory():
    """Every open regular public school (NCES Common Core of Data, via the Urban Institute's Education Data API):
    {id: [name, ZIP, county, lowest grade, highest grade, students, charter]}. The latest edition is saved once."""
    for y in range(THIS_YEAR, THIS_YEAR - 5, -1):
        cached = load_dcache(f"schools/directory_{y}.json")
        if cached:
            return cached, y
    for y in range(THIS_YEAR, THIS_YEAR - 5, -1):
        out = {}
        try:
            for fips in STATES:
                url, page = f"https://educationdata.urban.org/api/v1/schools/ccd/directory/{y}/?fips={int(fips)}", 1
                while url:
                    d = json.loads(fetch(url, f"ccd_{y}_{fips}_{page}.json", PLAIN, timeout=300))
                    for s in d.get("results", []):
                        if s.get("school_type") != 1 or s.get("school_status") not in (1, 3, 4, 5, 8):
                            continue  # regular schools that are open
                        enr = s.get("enrollment")
                        out[s["ncessch"]] = [s.get("school_name") or "", (s.get("zip_location") or "")[:5], s.get("county_code") or "",
                                             s.get("lowest_grade_offered"), s.get("highest_grade_offered"),
                                             enr if isinstance(enr, int) and enr > 0 else None, 1 if s.get("charter") == 1 else 0]
                    url, page = d.get("next"), page + 1
                if fips == "01" and not out:
                    raise ValueError("not published yet")
        except Exception as e:
            print(f"  (school list {y} not available: {e})")
            continue
        if len(out) > 50000:
            save_dcache(f"schools/directory_{y}.json", out)
            return out, y
    return {}, None


def grade_label(g):
    return None if g is None or g < -1 else "PK" if g == -1 else "K" if g == 0 else str(min(g, 12))


def school_ratings(directory, scores, county_to_cbsa):
    """Enrollment-weighted average school score for every ZIP, county and metro, for each year: {level: {id: {year: v}}}."""
    out = {"zip": {}, "county": {}, "metro": {}}
    for year, sc in scores.items():
        sums = {"zip": {}, "county": {}, "metro": {}}
        for sid, (name, zp, county, lo, hi, enr, charter) in directory.items():
            s = sc.get(sid)
            if s is None:
                continue
            county = str(county).zfill(5) if county else ""  # the school list drops the leading zero ("1095")
            w = enr or 100
            for lvl, key in (("zip", zp), ("county", county), ("metro", county_to_cbsa.get(county))):
                if key:
                    a = sums[lvl].setdefault(key, [0.0, 0.0])
                    a[0] += s * w
                    a[1] += w
        for lvl in sums:
            for key, (t, w) in sums[lvl].items():
                out[lvl].setdefault(key, {})[str(year)] = round(t / w, 1)
    return out


# ---------- natural hazards (FEMA National Risk Index) and walkability (EPA) ----------
# FEMA publishes the National Risk Index on its official ArcGIS account (FEMA_NationalRiskIndex), which programs can
# query. Scores are national percentiles, 0-100 (higher = more risk). Counties come straight from FEMA; ZIP codes are an
# area-weighted average of the census tracts they overlap; metros and states are population-weighted county averages.
NRI_QUERY = "https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/{}/FeatureServer/0/query"
NRI_FIELDS = {"risk": ("RISK_SCORE",), "flood_risk": ("IFLD_RISKS", "CFLD_RISKS"), "fire_risk": ("WFIR_RISKS",), "hurricane_risk": ("HRCN_RISKS",),
              "tornado_risk": ("TRND_RISKS",), "heat_risk": ("HWAV_RISKS",), "quake_risk": ("ERQK_RISKS",)}


def nri_layer(service, id_field):
    from urllib.parse import urlencode
    fields = [id_field, "POPULATION", "NRI_VER"] + sorted({f for fs in NRI_FIELDS.values() for f in fs})
    out, offset, ver = {}, 0, None
    while True:
        url = NRI_QUERY.format(service) + "?" + urlencode({"where": "1=1", "outFields": ",".join(fields), "returnGeometry": "false",
                                                             "orderByFields": id_field, "resultOffset": offset, "resultRecordCount": 2000, "f": "json"})
        d = json.loads(fetch(url, f"nri_{service}_{offset}.json", PLAIN, timeout=240))
        feats = d.get("features", [])
        for f in feats:
            a = f["attributes"]
            ver = ver or a.get("NRI_VER")
            rec = {"pop": a.get("POPULATION") or 0}
            for key, flds in NRI_FIELDS.items():
                vals = [a.get(x) for x in flds if isinstance(a.get(x), (int, float))]
                if vals:
                    rec[key] = round(max(vals), 1)  # flood = the worse of inland and coastal flooding
            out[str(a[id_field])] = rec
        if len(feats) < 2000 and not d.get("exceededTransferLimit"):
            break
        offset += len(feats)
    return out, ver


def hazards():
    """{"county": {fips: {...}}, "zip": {zip: {...}}, "ver": "December 2025"}, kept in data_cache/ until FEMA updates it."""
    cached = load_dcache("hazards.json")
    try:
        probe = json.loads(urllib.request.urlopen(urllib.request.Request(
            NRI_QUERY.format("National_Risk_Index_Counties") + "?where=1%3D1&outFields=NRI_VER&resultRecordCount=1&returnGeometry=false&f=json",
            headers=PLAIN), timeout=60).read())
        ver = probe["features"][0]["attributes"]["NRI_VER"]
    except Exception as e:
        print(f"  (FEMA risk index unavailable: {e})")
        return cached or {}
    if cached and cached.get("ver") == ver:
        return cached
    counties, ver = nri_layer("National_Risk_Index_Counties", "STCOFIPS")
    tracts, _ = nri_layer("National_Risk_Index_Census_Tracts", "TRACTFIPS")
    # ZIP codes: average of their tracts, weighted by how much land each tract shares with the ZIP
    rel = fetch("https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_tract20_natl.txt", "zcta_tract20.txt")
    lines = rel.decode("utf-8-sig", "replace").splitlines()
    head = lines[0].split("|")
    zi, ti, ai = head.index("GEOID_ZCTA5_20"), head.index("GEOID_TRACT_20"), head.index("AREALAND_PART")
    acc = {}
    for line in lines[1:]:
        t = line.split("|")
        if len(t) <= ai or not t[zi] or t[ti] not in tracts:
            continue
        w = num(t[ai]) or 0
        if not w:
            continue
        z = acc.setdefault(t[zi], {})
        for k, v in tracts[t[ti]].items():
            if k != "pop":
                s = z.setdefault(k, [0.0, 0.0])
                s[0] += v * w
                s[1] += w
    zips = {z: {k: round(s / w, 1) for k, (s, w) in d.items() if w} for z, d in acc.items()}
    res = {"ver": ver, "county": counties, "zip": zips}
    save_dcache("hazards.json", res)
    print(f"  FEMA risk index ({ver}): {len(counties):,} counties, {len(zips):,} ZIPs")
    return res


def walkability():
    """EPA National Walkability Index (1 = least walkable, 20 = most), averaged by population from neighbourhood block
    groups up to counties and (through census tracts) to ZIP codes. Kept in data_cache/ (the EPA file is from 2021)."""
    cached = load_dcache("walk.json")
    if cached:
        return cached
    path = fetch_file("https://edg.epa.gov/EPADataCommons/public/OA/EPA_SmartLocationDatabase_V3_Jan_2021_Final.csv", "epa_sld.csv")
    county, tract = {}, {}
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        r = csv.reader(f)
        head = next(r)
        c = {h: i for i, h in enumerate(head)}
        for row in r:
            if len(row) < len(head) - 2:
                continue
            w, pop = num(row[c["NatWalkInd"]]), num(row[c["TotPop"]]) or 0
            if w is None:
                continue
            st, co, tr = row[c["STATEFP"]].zfill(2), row[c["COUNTYFP"]].zfill(3), row[c["TRACTCE"]].zfill(6)
            for d, key in ((county, st + co), (tract, st + co + tr)):
                a = d.setdefault(key, [0.0, 0.0])
                a[0] += w * pop
                a[1] += pop
    county = {k: round(s / p, 1) for k, (s, p) in county.items() if p}
    tract = {k: s / p for k, (s, p) in tract.items() if p}
    rel = fetch("https://www2.census.gov/geo/docs/maps-data/data/rel/zcta_tract_rel_10.txt", "zcta_tract10.txt")
    lines = rel.decode("utf-8-sig", "replace").splitlines()
    head = lines[0].split(",")
    zi, gi, pi = head.index("ZCTA5"), head.index("GEOID"), head.index("POPPT")
    acc = {}
    for line in lines[1:]:
        t = line.split(",")
        if len(t) <= pi or t[gi] not in tract:
            continue
        w = num(t[pi]) or 0
        if w:
            a = acc.setdefault(t[zi], [0.0, 0.0])
            a[0] += tract[t[gi]] * w
            a[1] += w
    res = {"county": county, "zip": {z: round(s / w, 1) for z, (s, w) in acc.items() if w}}
    save_dcache("walk.json", res)
    print(f"  walkability: {len(county):,} counties, {len(res['zip']):,} ZIPs")
    return res


# ---------- jobs and pay (BLS Quarterly Census of Employment and Wages, yearly averages) ----------
def qcew_years():
    """{year: {area: [jobs, average yearly pay]}} for every county ("06037"), state ("06000") and metro ("C3108").
    Finished years are kept in data_cache/qcew.json."""
    cache = load_dcache("qcew.json") or {}
    for y in range(2010, THIS_YEAR):
        if str(y) in cache:
            continue
        data = try_fetch(f"https://data.bls.gov/cew/data/api/{y}/a/industry/10.csv", f"qcew_{y}.csv", PLAIN)
        if not data:
            continue
        r = rows(data)
        head = next(r)
        c = {h: i for i, h in enumerate(head)}
        out = {}
        for row in r:
            if len(row) < len(head) or row[c["own_code"]] != "0" or row[c["agglvl_code"]] not in ("70", "50", "40"):
                continue
            emp, pay = num(row[c["annual_avg_emplvl"]]), num(row[c["avg_annual_pay"]])
            if emp:
                out[row[c["area_fips"]]] = [emp, pay]
        if len(out) > 1000:
            cache[str(y)] = out
    save_dcache("qcew.json", cache)
    return {int(y): v for y, v in cache.items()}


# ---------- flood insurance (FEMA's National Flood Insurance Program, via OpenFEMA) ----------
def flood_insurance():
    """County shares of homes in high-risk flood zones and with flood insurance, plus every flood claim since 2000
    (count and dollars paid) by county and ZIP code and year. Claims are refreshed every 6 months (data_cache/flood.json)."""
    cached = load_dcache("flood.json")
    if cached and cached.get("built"):
        age = (date.today().year - int(cached["built"][:4])) * 12 + date.today().month - int(cached["built"][5:7])
        if age < 6:
            return cached
    from urllib.parse import urlencode
    base = "https://www.fema.gov/api/open/"
    res = {"built": date.today().isoformat()[:7], "pen": {}, "county": {}, "zip": {}}
    try:
        d = json.loads(fetch(base + "v1/NfipResidentialPenetrationRates?" + urlencode({"$top": 10000}), "fema_pen.json", PLAIN, timeout=240, tries=6))
        for rec in d.get("NfipResidentialPenetrationRates", []):
            f = (rec.get("fipsCode") or "").zfill(5)
            if f[:2] in STATES:
                res["pen"][f] = [rec.get("resContractsInForce") or 0, rec.get("resContractsInForceSfha") or 0,
                                 rec.get("totalResStructures") or 0, rec.get("totalResStructuresSfha") or 0]
    except Exception as e:
        print(f"  (FEMA flood-zone shares unavailable: {e})")
    try:
        skip = 0
        while True:
            q = urlencode({"$select": "countyCode,reportedZipCode,yearOfLoss,amountPaidOnBuildingClaim,amountPaidOnContentsClaim",
                           "$filter": "yearOfLoss ge 2000", "$top": 10000, "$skip": skip, "$orderby": "id"})
            d = json.loads(fetch(base + "v3/NfipClaims?" + q, f"fema_claims_{skip}.json", PLAIN, timeout=300, tries=8))
            time.sleep(0.5)  # be gentle with FEMA's servers
            batch = d.get("NfipClaims", [])
            for c in batch:
                y = c.get("yearOfLoss")
                if not y:
                    continue
                paid = (c.get("amountPaidOnBuildingClaim") or 0) + (c.get("amountPaidOnContentsClaim") or 0)
                for lvl, key in (("county", (c.get("countyCode") or "")[:5]), ("zip", (c.get("reportedZipCode") or "")[:5])):
                    if len(key) == 5 and key.isdigit():
                        a = res[lvl].setdefault(key, {}).setdefault(str(int(y)), [0, 0])
                        a[0] += 1
                        a[1] += paid
            if len(batch) < 10000:
                break
            skip += len(batch)
        print(f"  FEMA flood insurance: {len(res['pen']):,} counties, {skip + len(batch):,} claims")
    except Exception as e:
        # Claims didn't finish: use the last complete set if there is one, and try again next run (nothing saved).
        print(f"  (FEMA flood claims unavailable: {e})")
        res["county"], res["zip"] = (cached or {}).get("county", {}), (cached or {}).get("zip", {})
        if not res["pen"]:
            res["pen"] = (cached or {}).get("pen", {})
        return res
    for lvl in ("county", "zip"):
        for k, ys in res[lvl].items():
            for y in ys:
                ys[y][1] = round(ys[y][1])
    save_dcache("flood.json", res)
    return res


def flood_metrics(claims_by_year, homes, last_year):
    """Yearly 10-year totals of flood claims and dollars paid (and claims per 1,000 homes)."""
    out = {}
    if not claims_by_year:
        return out
    for y in range(2009, last_year + 1):
        window = [claims_by_year.get(str(x)) for x in range(y - 9, y + 1)]
        n = sum(w[0] for w in window if w)
        paid = sum(w[1] for w in window if w)
        out.setdefault("flood_claims", {})[str(y)] = n
        out.setdefault("flood_paid", {})[str(y)] = paid
        if homes:
            out.setdefault("flood_claims_rate", {})[str(y)] = round(n / homes * 1000, 2)
    return out


# ---------- transportation noise (BTS National Transportation Noise Map, by census tract) ----------
def read_dbf(data, wanted):
    """Yield {field: text} for each record of a .dbf table (the attribute table of a shapefile)."""
    import struct
    nrec, hlen, rlen = struct.unpack("<IHH", data[4:12])
    fields, pos, off = [], 32, 1
    while data[pos] != 0x0D:
        name = data[pos:pos + 11].split(b"\x00")[0].decode("latin-1")
        flen = data[pos + 16]
        fields.append((name, off, flen))
        off += flen
        pos += 32
    keep = [(n, o, l) for n, o, l in fields if n in wanted]
    for i in range(nrec):
        start = hlen + i * rlen
        if data[start:start + 1] == b"*":
            continue  # deleted record
        yield {n: data[start + o:start + o + l].decode("latin-1").strip() for n, o, l in keep}


def noise_levels():
    """Population-weighted road, rail and aircraft noise (dB) and the share of residents exposed to 60 dB or more,
    by county and ZIP code. Tract figures from Seto and Huang (2023), University of Washington, built on the US DOT
    National Transportation Noise Map. Kept in data_cache/noise.json."""
    cached = load_dcache("noise.json")
    if cached:
        return cached
    bands = [("ns4050n", 47.5), ("ns5060n", 55), ("ns6070n", 65), ("ns7080n", 75), ("ns8090n", 85), ("ns90n", 90)]
    tracts = {}
    for name in ("conus_shp.zip", "AK_HI_shp.zip"):
        try:
            path = fetch_file("https://www.edmundseto.com/NTNE_map/" + name, "noise_" + name)
        except Exception as e:
            print(f"  (noise data unavailable: {e})")
            continue
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if not n.lower().endswith(".dbf"):
                    continue
                for rec in read_dbf(z.read(n), {"GEOID", "estimat"} | {b for b, _ in bands}):
                    pop = num(rec.get("estimat")) or 0
                    ns = [(num(rec.get(b)) or 0, mid) for b, mid in bands]
                    exposed = sum(x for x, _ in ns)
                    if not rec.get("GEOID") or not pop:
                        continue
                    db = sum(x * mid for x, mid in ns) / exposed if exposed else None
                    loud = sum(x for (x, mid) in ns if mid >= 65) / pop * 100
                    tracts[rec["GEOID"].zfill(11)] = (pop, db, min(loud, 100))
    if not tracts:
        return {}
    county = {}
    for t, (pop, db, loud) in tracts.items():
        a = county.setdefault(t[:5], [0.0, 0.0, 0.0, 0.0])
        if db is not None:
            a[0] += db * pop
            a[1] += pop
        a[2] += loud * pop
        a[3] += pop
    rel = fetch("https://www2.census.gov/geo/docs/maps-data/data/rel/zcta_tract_rel_10.txt", "zcta_tract10.txt")
    lines = rel.decode("utf-8-sig", "replace").splitlines()
    head = lines[0].split(",")
    zi, gi, pi = head.index("ZCTA5"), head.index("GEOID"), head.index("POPPT")
    zips = {}
    for line in lines[1:]:
        t = line.split(",")
        if len(t) <= pi or t[gi] not in tracts:
            continue
        w = num(t[pi]) or 0
        if not w:
            continue
        pop, db, loud = tracts[t[gi]]
        a = zips.setdefault(t[zi], [0.0, 0.0, 0.0, 0.0])
        if db is not None:
            a[0] += db * w
            a[1] += w
        a[2] += loud * w
        a[3] += w
    pack = lambda d: {k: {"noise_db": round(a[0] / a[1], 1) if a[1] else None, "noise_loud": round(a[2] / a[3], 1)} for k, a in d.items() if a[3]}
    res = {"county": pack(county), "zip": pack(zips)}
    save_dcache("noise.json", res)
    print(f"  noise: {len(tracts):,} tracts, {len(res['county']):,} counties, {len(res['zip']):,} ZIPs")
    return res


def archive_forecasts(fc_hist, zmetro):
    """Earlier Zillow metro forecasts (2022 onwards) from the Internet Archive's saved copies of Zillow's file.
    Runs once; the results are kept in data_cache/forecast.json."""
    cdx = ("https://web.archive.org/cdx/search/cdx?url=files.zillowstatic.com/research/public_csvs/zhvf_growth/Metro_zhvf_growth_uc_sfrcondo"
           "&matchType=prefix&filter=statuscode:200&fl=timestamp,original&collapse=digest")
    try:
        listing = fetch(cdx, "wayback_fc.txt", PLAIN, timeout=120, tries=2).decode()
    except Exception as e:
        print(f"  (Internet Archive unavailable: {e})")
        return False
    snaps = [line.split(" ", 1) for line in listing.splitlines() if " " in line]
    snaps.sort(key=lambda s: ("_sm_sa_" in s[1], s[0]))  # the seasonally adjusted file wins for the same month
    got = 0
    for ts, orig in snaps:
        try:
            data = fetch(f"https://web.archive.org/web/{ts}id_/{orig}", f"wayback_fc_{ts}.csv", PLAIN, timeout=120, tries=2)
        except Exception:
            continue
        time.sleep(1)
        r = rows(data)
        head = next(r)
        if "BaseDate" not in head or "RegionName" not in head:
            continue
        bi, ni, last = head.index("BaseDate"), head.index("RegionName"), len(head) - 1  # last column: 12 months ahead
        recs = {}
        for row in r:
            if len(row) > last and row[bi] and (v := num(row[last])) is not None:
                recs[row[ni]] = (row[bi][:7], v)
        for cbsa, (month, v) in zmetro(recs).items():
            fc_hist.setdefault(cbsa, {})[month] = round(v, 2)
        got += 1
    print(f"  {got} archived Zillow forecasts")
    return got > 0


def mortgage_rate():
    """Latest weekly 30-year fixed rate (Freddie Mac's weekly survey) and its monthly history.
    Freddie Mac's own file first (FRED often stalls requests from GitHub's servers), then FRED."""
    weekly = []  # (YYYY-MM-DD, rate)
    try:
        data = fetch("https://www.freddiemac.com/pmms/docs/PMMS_history.csv", "pmms_history.csv", PLAIN, timeout=60, tries=2)
        r = rows(data)
        head = [h.strip().lower() for h in next(r)]
        di, vi = head.index("date"), head.index("pmms30")
        for row in r:
            if len(row) > vi and (v := num(row[vi])) is not None:
                m, d, y = (row[di].split("/") + ["", "", ""])[:3]
                if y:
                    weekly.append((f"{y}-{int(m):02d}-{int(d):02d}", v))
        weekly.sort()
    except Exception as e:
        print(f"  (Freddie Mac unavailable: {e}); trying FRED")
    if not weekly:
        data = fetch("https://fred.stlouisfed.org/graph/fredgraph.csv?id=MORTGAGE30US", "fred_mortgage.csv", PLAIN, timeout=40, tries=2)
        weekly = [(r[0], num(r[1])) for r in list(rows(data))[1:] if len(r) == 2 and num(r[1]) is not None]
    monthly = {day[:7]: v for day, v in weekly}
    return monthly, weekly[-1][1], weekly[-1][0]


def xlsx_rows(data):
    """Rows of the first sheet of an Excel file, as lists of text (empty cells are ''). Standard library only."""
    import xml.etree.ElementTree as ET
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    zf = zipfile.ZipFile(io.BytesIO(data))
    shared = []
    if "xl/sharedStrings.xml" in zf.namelist():
        for si in ET.fromstring(zf.read("xl/sharedStrings.xml")).iter(ns + "si"):
            shared.append("".join(t.text or "" for t in si.iter(ns + "t")))
    sheet = sorted(n for n in zf.namelist() if n.startswith("xl/worksheets/sheet"))[0]
    with zf.open(sheet) as f:
        for _, el in ET.iterparse(f):
            if el.tag != ns + "row":
                continue
            out = []
            for c in el.iter(ns + "c"):
                ref = c.get("r", "")
                col = 0
                for ch in ref:
                    if ch.isalpha():
                        col = col * 26 + ord(ch.upper()) - 64
                while len(out) < col - 1:
                    out.append("")
                v = c.find(ns + "v")
                text = "" if v is None else v.text or ""
                if c.get("t") == "s" and text:
                    text = shared[int(text)]
                elif c.get("t") == "inlineStr":
                    text = "".join(t.text or "" for t in c.iter(ns + "t"))
                out.append(text)
            el.clear()
            yield out


def price_parities():
    """BEA Regional Price Parities (US = 100): overall prices and housing costs, every year since 2008, for states,
    metros, and each state's area outside metros. Returns {"state"|"metro"|"nonmetro": {id: {key: {year: v}}}}."""
    out = {"state": {}, "metro": {}, "nonmetro": {}}
    lines = {"1": "rpp_all", "3": "rpp_housing"}
    for name in ("SARPP", "MARPP", "PARPP"):
        try:
            data = fetch(f"https://apps.bea.gov/regional/zip/{name}.zip", f"bea_{name}.zip", PLAIN, timeout=60, tries=2)
            zf = zipfile.ZipFile(io.BytesIO(data))
            fname = next(n for n in zf.namelist() if n.startswith(name) and n.endswith(".csv"))
            text = zf.read(fname)
        except Exception as e:
            print(f"  (BEA {name} not available: {e})")
            continue
        r = csv.reader(io.StringIO(text.decode("latin-1")), skipinitialspace=True)
        head = next(r)
        years = [(i, h) for i, h in enumerate(head) if h.isdigit()]
        for row in r:
            if len(row) < len(head) or row[4].strip() not in lines:
                continue
            geo = row[0].strip().strip('"')
            if name == "SARPP" and geo.endswith("000") and geo[:2] in STATES:
                lvl, pid = "state", geo[:2]
            elif name == "MARPP" and geo not in ("00000", "00999"):
                lvl, pid = "metro", geo
            elif name == "PARPP" and geo.endswith("999") and geo[:2] in STATES:
                lvl, pid = "nonmetro", geo[:2]
            else:
                continue
            series = {y: round(v, 1) for i, y in years if (v := num(row[i])) is not None}
            if series:
                out[lvl].setdefault(pid, {})[lines[row[4].strip()]] = series
    print(f"  BEA price parities: {len(out['state'])} states, {len(out['metro'])} metros")
    return out


def fhfa_hpi():
    """FHFA house price index (all transactions), one value a year back to 1975, for states, metros, counties and ZIPs.
    Returns {"state"|"metro"|"county"|"zip": {id: {year: index}}}."""
    out = {}
    for lvl, name, idc, yc in (("state", "hpi_at_state", 2, 3), ("metro", "hpi_at_cbsa", 0, 2), ("county", "hpi_at_county", 2, 3),
                               ("zip", "hpi_at_zip5", 0, 1)):
        try:
            data = fetch(f"https://www.fhfa.gov/hpi/download/annual/{name}.xlsx", f"fhfa_{name}.xlsx", PLAIN, timeout=180, tries=2)
        except Exception as e:
            print(f"  (FHFA {name} not available: {e})")
            continue
        d = out.setdefault(lvl, {})
        for row in xlsx_rows(data):
            if len(row) <= yc + 2 or not row[yc].isdigit():
                continue
            pid = row[idc].strip()
            hpi = num(row[yc + 2])
            if hpi is None or (lvl == "metro" and len(pid) != 5):
                continue
            d.setdefault(pid, {})[row[yc]] = round(hpi, 2)
        print(f"  FHFA {lvl}: {len(d)} places")
    return out


def hpi_long(hpi, z):
    """FHFA's index turned into dollars: scaled so its latest year matches Zillow's typical value that year.
    Gives an estimated typical home value for every year back to 1975 (or whenever FHFA starts)."""
    if not hpi or not z:
        return None
    for y in sorted(hpi, reverse=True):
        months = [v for k, v in z.items() if k.startswith(y + "-")]
        if len(months) >= 6 and hpi[y]:
            f = sum(months) / len(months) / hpi[y]
            return {yy: tidy(v * f) for yy, v in hpi.items()}
    return None


def hpi_change(hpi):
    return {y: round((v / hpi[str(int(y) - 1)] - 1) * 100, 2) for y, v in hpi.items() if hpi.get(str(int(y) - 1))}


HUD_TOKEN = os.environ.get("HUD_API_TOKEN", "").strip()
FMR_KEYS = ("fmr_0br", "fmr_1br", "fmr_2br", "fmr_3br", "fmr_4br")


def hud_fmr():
    """HUD Fair Market Rents (studio to 4 bedrooms) by county, every year since 2017, and by ZIP code for the latest year
    where HUD sets them by ZIP (Small Area FMRs). Needs a free HUD API token (repository secret HUD_API_TOKEN).
    Saved in data_cache/hud.json: past years never change, so each is downloaded once."""
    cache = load_dcache("hud.json") or {"years": {}, "zip": {}, "zip_year": None}
    if not HUD_TOKEN:
        print("  (no HUD_API_TOKEN set: HUD Fair Market Rents saved earlier are used)")
        return cache
    base = "https://www.huduser.gov/hudapi/public/fmr/"

    def get(path):
        for attempt in range(4):
            try:
                req = urllib.request.Request(base + path, headers={**PLAIN, "Authorization": f"Bearer {HUD_TOKEN}", "Accept": "application/json"})
                return json.loads(urllib.request.urlopen(req, timeout=90).read())["data"]
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise RuntimeError(f"HUD refused the token ({e.code}): check the HUD_API_TOKEN secret and that the FMR API is selected on your HUD account")
                if e.code == 404:
                    return None
                time.sleep(10 * (attempt + 1))  # 429 / 5xx: wait and try again
            except Exception:
                time.sleep(5 * (attempt + 1))
        return None

    bed = ("Efficiency", "One-Bedroom", "Two-Bedroom", "Three-Bedroom", "Four-Bedroom")
    try:
        latest = None
        for y in [None] + list(range(THIS_YEAR, 2016, -1)):
            if y is not None and (str(y) in cache["years"] or (latest and y >= int(latest))):
                continue
            counties, sa_metros, year = {}, [], None
            for st, (abbr, _) in STATES.items():
                d = get(f"statedata/{abbr}" + (f"?year={y}" if y else ""))
                time.sleep(0.4)
                if not d:
                    continue
                year = str(d.get("year") or y)
                for c in d.get("counties") or []:
                    f = str(c.get("fips_code") or "")[:5]
                    vals = [num(c.get(b)) for b in bed]
                    if len(f) == 5 and all(v is not None for v in vals):
                        counties.setdefault(f, []).append(vals)
                if y is None:
                    sa_metros += [m["code"] for m in d.get("metroareas") or [] if str(m.get("smallarea_status")) == "1"]
            if not counties:
                continue
            # New England towns: one county can have several; use their average
            cache["years"][year] = {f: [round(sum(v[i] for v in vs) / len(vs)) for i in range(5)] for f, vs in counties.items()}
            print(f"  HUD Fair Market Rents {year}: {len(counties)} counties")
            if y is None:
                latest = year
                if cache.get("zip_year") != year:
                    zips = {}
                    for code in sorted(set(sa_metros)):
                        d = get(f"data/{code}")
                        time.sleep(0.4)
                        for row in (d or {}).get("basicdata") or []:
                            z = str(row.get("zip_code", ""))
                            vals = [num(row.get(b)) for b in bed]
                            if len(z) == 5 and z.isdigit() and all(v is not None for v in vals):
                                zips[z] = [round(v) for v in vals]
                    if zips:
                        cache["zip"], cache["zip_year"] = zips, year
                        print(f"  HUD Small Area FMRs {year}: {len(zips)} ZIP codes in {len(set(sa_metros))} metro areas")
            save_dcache("hud.json", cache)
    except Exception as e:
        print(f"  (HUD Fair Market Rents: {e})")
    return cache


def irs_migration(county_to_cbsa, names):
    """Who moves in and out of each place, from IRS tax returns (one file per year, people = exemptions, income = AGI).
    Metros and states leave out moves between their own counties. Saved by year, so each year is downloaded once.
    Returns {year: {"county"|"metro"|"state": {id: {key: value}}}} and the latest year's top origins and destinations."""
    cache = load_dcache("irs.json") or {}
    years = cache.setdefault("years", {})
    flows_out = cache.get("flows") or {}
    for y in range(THIS_YEAR - 1, 2014, -1):
        ys = str(y)
        if ys in years:
            if int(ys) < THIS_YEAR - 3:
                break  # everything older is saved already
            continue
        tag = f"{(y - 1) % 100:02d}{y % 100:02d}"
        try:
            inflow = fetch(f"https://www.irs.gov/pub/irs-soi/countyinflow{tag}.csv", f"irs_in_{tag}.csv", PLAIN, timeout=90, tries=2)
            outflow = fetch(f"https://www.irs.gov/pub/irs-soi/countyoutflow{tag}.csv", f"irs_out_{tag}.csv", PLAIN, timeout=90, tries=2)
        except Exception as e:
            print(f"  (IRS migration {y} not available: {e})")
            continue
        tot = {}    # county -> [in returns, in people, in agi, out returns, out people, out agi, same-state in (3), same-state out (3)]
        pairs = {}  # (from county, to county) -> [returns, people, agi]
        top = {}    # county -> {"in": [...], "out": [...]}
        for data, inbound in ((inflow, True), (outflow, False)):
            r = rows(data, "latin-1")
            next(r)
            for row in r:
                if len(row) < 9:
                    continue
                a, b = row[0].zfill(2) + row[1].zfill(3), row[2].zfill(2) + row[3].zfill(3)  # the county, the other place
                if a[:2] not in STATES:
                    continue
                n1, n2, agi = num(row[6]), num(row[7]), num(row[8])
                if n1 is None or n1 < 0 or n2 is None or n2 < 0:
                    continue
                agi = agi if agi is not None and agi >= 0 else 0
                t = tot.setdefault(a, [0] * 12)
                off = 0 if inbound else 3
                if b == "96000":
                    t[off:off + 3] = [n1, n2, agi]
                elif b == "97001":
                    t[6 + off:9 + off] = [n1, n2, agi]
                elif b[:2] in STATES and b != a:
                    if inbound:
                        pairs[(b, a)] = [n1, n2, agi]
                    lst = top.setdefault(a, {"in": [], "out": []})["in" if inbound else "out"]
                    lst.append([b, int(n2), round(agi * 1000 / n1) if n1 else None])
        res = {"county": {}, "metro": {}, "state": {}}

        def put(lvl, pid, n):
            if n[1] or n[4]:
                res[lvl][pid] = {"mig_in": int(n[1]), "mig_out": int(n[4]), "mig_net": int(n[1] - n[4]),
                                 "mig_in_income": round(n[2] * 1000 / n[0]) if n[0] else None,
                                 "mig_out_income": round(n[5] * 1000 / n[3]) if n[3] else None}
        for c, t in tot.items():
            put("county", c, t)
        groups = {}
        for c, m in county_to_cbsa.items():
            groups.setdefault(m, []).append(c)
        for m, cs in groups.items():
            members = set(cs)
            n = [sum(tot.get(c, [0] * 12)[i] for c in cs) for i in range(6)]
            for (src, dst), v in pairs.items():
                if src in members and dst in members:  # a move between two of the metro's own counties
                    for i in range(3):
                        n[i] -= v[i]
                        n[3 + i] -= v[i]
            put("metro", m, n)
        for st in STATES:
            cs = [c for c in tot if c[:2] == st]
            n = [sum(tot[c][i] - tot[c][6 + i] for c in cs) for i in range(6)]  # minus moves within the state
            put("state", st, n)
        years[ys] = res
        if not flows_out or y >= int(cache.get("flows_year") or 0):
            flows_out, cache["flows_year"] = {}, y
            for c, d in top.items():
                flows_out[c] = {k: [[f, n, inc] for f, n, inc in sorted(v, key=lambda x: -x[1])[:8]] for k, v in d.items()}
        print(f"  IRS migration {y}: {len(res['county'])} counties")
    cache["flows"] = flows_out
    save_dcache("irs.json", cache)
    return years, flows_out, cache.get("flows_year")


def _plain_name(s):
    """'Doña Ana County' -> 'dona ana', 'Baltimore city' / 'Baltimore (City)' -> 'baltimore city': for matching names."""
    import unicodedata
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = s.replace("(city)", "city").replace(".", "").replace("'", "")
    for suffix in (" county", " parish", " borough", " census area", " city and borough", " municipality", " municipio"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return " ".join(s.split())


def air_quality(names):
    """EPA's yearly air quality by county: unhealthy days per year (scaled to a full year of readings) and median AQI.
    Counties are matched by name (EPA's file has no codes); counties without a monitor have no figures."""
    cache = load_dcache("aqi.json") or {}
    lookup = {}
    for pid, n in names.items():
        if len(pid) == 5:
            lookup[(pid[:2], _plain_name(n))] = pid
    by_state_name = {n.lower(): f for f, (a, n) in STATES.items()}
    cache.pop(str(THIS_YEAR), None)
    for y in range(2015, THIS_YEAR):  # full years only (this year's file covers just the months so far)
        ys = str(y)
        if ys in cache and y < THIS_YEAR - 2:
            continue  # the last two years are refreshed, as EPA adds late readings
        try:
            data = fetch(f"https://aqs.epa.gov/aqsweb/airdata/annual_aqi_by_county_{y}.zip", f"aqi_{y}.zip", PLAIN, timeout=60, tries=2)
        except Exception as e:
            print(f"  (EPA air quality {y} not available: {e})")
            continue
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            text = zf.read(zf.namelist()[0])
        r = rows(text, "latin-1")
        head = next(r)
        col = {h: i for i, h in enumerate(head)}
        out = {}
        for row in r:
            st = by_state_name.get(row[col["State"]].lower())
            pid = st and lookup.get((st, _plain_name(row[col["County"]])))
            days = num(row[col["Days with AQI"]])
            if not pid or not days or days < 90:
                continue
            bad = sum(num(row[col[k]]) or 0 for k in ("Unhealthy for Sensitive Groups Days", "Unhealthy Days", "Very Unhealthy Days", "Hazardous Days"))
            out[pid] = [round(bad / days * 365, 1), num(row[col["Median AQI"]])]
        if out:
            cache[ys] = out
            print(f"  EPA air quality {y}: {len(out)} counties")
    save_dcache("aqi.json", cache)
    return cache


def cpi_inflation():
    """US consumer price inflation, % change from a year earlier, by month (BLS CPI-U, all items, not seasonally adjusted).
    BLS's public API first (10 years per request), FRED as a backup; saved, so a failed download keeps the last copy."""
    cached = load_dcache("cpi.json") or {}
    index = {}
    try:
        for start in range(1999, THIS_YEAR + 1, 10):
            body = json.dumps({"seriesid": ["CUUR0000SA0"], "startyear": str(start), "endyear": str(min(start + 9, THIS_YEAR))}).encode()
            req = urllib.request.Request("https://api.bls.gov/publicAPI/v1/timeseries/data/", data=body,
                                         headers={**PLAIN, "Content-Type": "application/json"})
            res = json.loads(urllib.request.urlopen(req, timeout=60).read())
            if res.get("status") != "REQUEST_SUCCEEDED":
                raise ValueError(res.get("message"))
            for d in res["Results"]["series"][0]["data"]:
                if d["period"].startswith("M") and d["period"] != "M13" and num(d["value"]) is not None:
                    index[f"{d['year']}-{d['period'][1:]}"] = num(d["value"])
            time.sleep(1)
    except Exception as e:
        print(f"  (BLS CPI unavailable: {e}); trying FRED")
        try:
            data = fetch("https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCNS", "fred_cpi.csv", PLAIN, timeout=40, tries=2)
            index = {r[0][:7]: num(r[1]) for r in list(rows(data))[1:] if len(r) == 2 and num(r[1]) is not None}
        except Exception as e2:
            print(f"  (FRED CPI unavailable: {e2})")
    yoy = {k: round((v / index[months_back(k, 12)] - 1) * 100, 2) for k, v in index.items() if index.get(months_back(k, 12))}
    if len(yoy) < len(cached) * 0.9:
        print("  CPI: keeping the saved copy")
        return cached
    yoy = dict(sorted(yoy.items()))
    save_dcache("cpi.json", yoy)
    print(f"  CPI inflation through {max(yoy) if yoy else None}")
    return yoy


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


def trend_gap(series, min_points=120):
    """How far today's value sits above (+) or below (-) its pre-2020 long-term trend line (log-linear fit)."""
    pts = [(i, math.log(v)) for i, (k, v) in enumerate(sorted(series.items())) if k < "2020-01" and v > 0]
    if len(pts) < min_points:
        return None
    n = len(pts)
    mx, my = sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    slope = sum((p[0] - mx) * (p[1] - my) for p in pts) / sxx
    i_now = len(series) - 1
    fair = math.exp(my + slope * (i_now - mx))
    return (latest(series)[1] / fair - 1) * 100


def income_month(inc, ym):
    """Household income for a month: each year's figure sits at mid-year, months in between are interpolated, and months
    after the last published year grow at the last 3 years' average pace (kept between 0% and 4% a year)."""
    yrs = sorted(int(y) for y, v in (inc or {}).items() if v)
    if not yrs:
        return None
    t = int(ym[:4]) + (int(ym[5:7]) - 7) / 12
    first, last = yrs[0], yrs[-1]
    if t >= last:
        prev = inc.get(str(last - 3))
        g = min(0.04, max(0.0, (inc[str(last)] / prev) ** (1 / 3) - 1)) if prev else 0.03
        return inc[str(last)] * (1 + g) ** (t - last)
    if t <= first:
        return inc[str(first)] if t > first - 1 else None
    lo = max(y for y in yrs if y <= t)
    hi = min(y for y in yrs if y > t)
    return inc[str(lo)] + (inc[str(hi)] - inc[str(lo)]) * (t - lo) / (hi - lo)


def fair_ratio(series, inc, years=20, min_months=60):
    """The place's average price-to-income ratio over the last 20 years (or as far back as both go). Fair value for any
    month = that month's income x this ratio; today's price above it = overvalued. Same idea Reventure describes."""
    if not series or not inc:
        return None
    last = max(series)
    start = months_back(last, years * 12)
    ratios = [v / i for k, v in series.items() if k > start and v and (i := income_month(inc, k))]
    return sum(ratios) / len(ratios) if len(ratios) >= min_months else None


def fair_gap(series, inc, years=20, min_months=60):
    r = fair_ratio(series, inc, years, min_months)
    if not r:
        return None
    k, v = latest(series)
    return (v / (income_month(inc, k) * r) - 1) * 100


def house_rent(rent, ratio):
    """Estimated rent for houses: a place's typical rent x its metro's house-to-all-homes rent ratio, month by month."""
    if not rent or not ratio:
        return None
    out = {k: round(v * ratio[k]) for k, v in rent.items() if ratio.get(k)}
    return out or None


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


def tidy(v):
    """Round for storage: big numbers to whole numbers, small ones to a few decimals."""
    if v is None or not math.isfinite(v):
        return None
    a = abs(v)
    return int(round(v)) if a >= 1000 else round(v, 1) if a >= 100 else round(v, 2) if a >= 1 else round(v, 4)


EXPENSES = 0.35  # share of rent assumed to go to taxes, insurance, upkeep and vacancy (for the cap rate)


def metrics_for(z, zr, rt, income, rate, forecast=None, sfr=None, condo=None, rent_sfr=None, units=None, tax_rate=None, inc_series=None):
    """Monthly-data metrics for one place. z/zr/sfr/condo/rent_sfr: Zillow series, rt: Realtor.com series."""
    m = {}
    hv_k, hv = latest(z or {})
    rent_k, rent = latest(zr or {})
    if hv:
        m["hv"] = rnd(hv, 0)
        m["hv_mom"] = rnd(change(z, 1), 2)
        m["hv_yoy"] = rnd(change(z, 12), 1)
        m["hv_5y"] = rnd(change(z, 60), 1)
        m["overvalued"] = rnd(fair_gap(z, inc_series), 1)
        r = fair_ratio(z, inc_series)
        if r:
            m["fair_value"] = rnd(income_month(inc_series, max(z)) * r, 0)
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
        if tax_rate:
            m["prop_tax"] = rnd(hv * tax_rate / 100, 0)
    for key, s in (("sfr", sfr), ("condo", condo)):
        k, v = latest(s or {})
        if v:
            m[key] = rnd(v, 0)
            m[key + "_yoy"] = rnd(change(s, 12), 1)
    if forecast is not None:
        m["fc_1y"] = rnd(forecast, 1)
    if rent:
        m["rent"] = rnd(rent, 0)
        m["rent_yoy"] = rnd(change(zr, 12), 1)
        m["rent_overvalued"] = rnd(fair_gap(zr, inc_series, min_months=48), 1)
        if income:
            m["rent_pct_income"] = rnd(rent * 12 / income * 100, 1)
        if hv:
            m["price_rent"] = rnd(hv / (rent * 12), 1)
            m["gross_yield"] = rnd(rent * 12 / hv * 100, 2)
            m["cap_rate"] = rnd(rent * 12 * (1 - EXPENSES) / hv * 100, 2)
            m["buy_vs_rent"] = rnd(payment(hv, rate) - rent, 0)
    k, v = latest(rent_sfr or {})
    if v:
        m["rent_sfr"] = rnd(v, 0)
    if rt:
        for k in ("inv", "dom", "newl", "lp"):
            kk, v = latest(rt[k])
            if v is not None:
                m[k] = rnd(v, 0)
                m[k + "_yoy"] = rnd(change(rt[k], 12), 1)
        m["inv_mom"] = rnd(change(rt["inv"], 1), 1)
        _, ppsf = latest(rt.get("ppsf") or {})
        _, sqft = latest(rt.get("sqft") or {})
        if ppsf:
            m["ppsf"] = rnd(ppsf, 0)
        if sqft:
            m["med_sqft"] = rnd(sqft, 0)
        kk, v = latest(rt["cut"])
        if v is not None:
            m["cut"] = rnd(v * 100, 1)
        kk, v = latest(rt.get("inc") or {})
        if v is not None:
            m["price_up"] = rnd(v * 100, 1)
        m["inv_vs_2019"] = rnd(vs_prepandemic(rt["inv"]), 1)
        _, pend = latest(rt["pend"])
        _, inv = latest(rt["inv"])
        if pend:
            m["sales"] = rnd(pend, 0)
            m["sales_yoy"] = rnd(change(rt["pend"], 12), 1)
            m["sales_vs_2019"] = rnd(vs_prepandemic(rt["pend"]), 1)
        if pend and inv:
            m["months_supply"] = rnd(inv / pend, 1)  # rough: active listings per pending sale
            m["pending_ratio"] = rnd(pend / inv * 100, 0)  # pending sales per 100 listings
        if inv and units:
            m["inv_pct_houses"] = rnd(inv / units * 100, 2)
    return {k: v for k, v in m.items() if v is not None}


def trim_series(s, start):
    return {k: (round(v) if v >= 1000 else round(v, 4)) for k, v in sorted(s.items()) if k >= start}


# ---------- yearly data points ----------
SUMMABLE = ("pop", "births", "deaths", "natural", "dom_mig", "intl_mig", "agepop", "u18", "a2544", "a65", "a75", "medage_w",
            "permits", "units", "votes_t", "votes_d", "votes_r")


def add_up(R, members):
    """Raw yearly numbers for a group of counties (a metro, or a state where the source has no state rows)."""
    out = {}
    for f in SUMMABLE:
        acc = {}
        for c in members:
            for y, v in R.get(c, {}).get(f, {}).items():
                acc[y] = acc.get(y, 0) + v
        if acc:
            out[f] = acc
    # Weighted averages: income and poverty by population, temperature by land area.
    for f, wf in (("income", "pop"), ("poverty", "pop"), ("temp", "area")):
        acc, wts = {}, {}
        for c in members:
            rec = R.get(c, {})
            for y, v in rec.get(f, {}).items():
                w = rec.get("area_") if wf == "area" else (rec.get("pop", {}).get(y) or (latest({str(k): x for k, x in rec.get("pop", {}).items()})[1]))
                if w:
                    acc[y] = acc.get(y, 0) + v * w
                    wts[y] = wts.get(y, 0) + w
        if acc:
            out[f] = {y: acc[y] / wts[y] for y in acc if wts[y]}
    area = sum(R.get(c, {}).get("area_") or 0 for c in members)
    if area:
        out["area_"] = area
    return out


def derive(r):
    """Raw yearly numbers -> yearly data points {metric: {"YYYY": value}}."""
    out = {}

    def put(k, y, v):
        if v is not None and math.isfinite(v):
            out.setdefault(k, {})[str(y)] = tidy(v)
    g = lambda f: r.get(f) or {}
    pop, area = g("pop"), r.get("area_")
    for y, p in sorted(pop.items()):
        if not p:
            continue
        put("pop", y, p)
        if pop.get(y - 1):
            put("pop_growth", y, (p / pop[y - 1] - 1) * 100)
        if pop.get(y - 5):
            put("pop_growth_5y", y, (p / pop[y - 5] - 1) * 100)
        if area:
            put("density", y, p / area)
        for f in ("births", "deaths", "natural", "dom_mig", "intl_mig"):
            v = g(f).get(y)
            if v is not None:
                put(f, y, v)
                put(f + "_pct", y, v / p * 100)
        b, d = g("births").get(y), g("deaths").get(y)
        if b and d:
            put("birth_death", y, b / d)
    for y, p in sorted(g("agepop").items()):
        if not p:
            continue
        u18, a2544, a65, a75 = (g(f).get(y) for f in ("u18", "a2544", "a65", "a75"))
        if u18 is not None:
            put("u18_pct", y, u18 / p * 100)
        if a2544 is not None:
            put("a2544_pct", y, a2544 / p * 100)
        if a65 is not None:
            put("a65_pct", y, a65 / p * 100)
        if a75 is not None:
            put("a75_pct", y, a75 / p * 100)
        if a65:
            put("r_u18_65", y, (u18 or 0) / a65)
            put("r_2544_65", y, (a2544 or 0) / a65)
        if a75:
            put("r_u18_75", y, (u18 or 0) / a75)
            put("r_2544_75", y, (a2544 or 0) / a75)
        med = g("medage").get(y) or (g("medage_w").get(y, 0) / p or None)
        put("med_age", y, med)
    inc = g("income")
    for y, v in sorted(inc.items()):
        put("income", y, v)
        if inc.get(y - 1):
            put("income_growth", y, (v / inc[y - 1] - 1) * 100)
    for y, v in g("poverty").items():
        put("poverty", y, v)
    units = g("units")
    for y, v in sorted(units.items()):
        put("units", y, v)
        if units.get(y - 1):
            put("units_growth", y, (v / units[y - 1] - 1) * 100)
    for y, v in g("permits").items():
        put("permits", y, v)
        base = units.get(y - 1) or units.get(y)
        if base:
            put("permits_pct", y, v / base * 100)
    for y, t in g("votes_t").items():
        if t:
            put("vote_r", y, (g("votes_r").get(y) or 0) / t * 100)
            put("vote_d", y, (g("votes_d").get(y) or 0) / t * 100)
    for y, v in g("temp").items():
        put("temp", y, v)
    return out


def percentile_ranks(values):
    """{id: value} -> {id: 0..1} (ties share a rank)."""
    items = sorted(values.items(), key=lambda kv: kv[1])
    n = len(items)
    return {k: (i / (n - 1) if n > 1 else 0.5) for i, (k, _) in enumerate(items)}


def growth_scores(A):
    """Long-Term Growth Score (0-100), yearly, for one level: the average percentile rank of 5-year population growth,
    net domestic migration (as % of population) and 5-year median income growth."""
    years = sorted({y for rec in A.values() for y in rec.get("pop_growth_5y", {})})
    for y in years:
        y5 = str(int(y) - 5)
        parts = []
        for key in ("pop_growth_5y", "dom_mig_pct"):
            parts.append(percentile_ranks({pid: rec[key][y] for pid, rec in A.items() if y in rec.get(key, {})}))
        inc = {pid: rec["income"][y] / rec["income"][y5] - 1 for pid, rec in A.items() if y in rec.get("income", {}) and rec["income"].get(y5)}
        parts.append(percentile_ranks(inc))
        for pid in parts[0]:
            got = [p[pid] for p in parts if pid in p]
            if len(got) == 3:
                A[pid].setdefault("growth_score", {})[y] = round(sum(got) / 3 * 100)


def latest_annual(a):
    return {k: s[max(s)] for k, s in a.items() if s}


def county_name(fips, zv, rt, names):
    z = zv["county"].get(fips)
    if z:
        return z["name"]
    if names.get(fips):
        return names[fips]
    return f"County {fips}"


# ---------- main ----------
def main():
    print("Census: metros, population, age, income, housing, permits, land area")
    metro_names, county_to_cbsa = census_metros()
    R = {}
    pop_year, pop_names = census_population(R)
    age_year = census_age(R)
    inc_year = census_income(R)
    permit_year = census_permits(R)
    unit_year = census_housing_units(R)
    area = land_area()
    for pid, a in area.items():
        if len(pid) == 5 and pid in R:
            R[pid]["area_"] = a
    print("NOAA temperatures, election results")
    temp_year = noaa_temperature(R)
    vote_year = election_results(R)
    print("American Community Survey")
    acs, acs_year = acs_years()
    print("Mortgage data (HMDA)")
    hmda, hmda_year_ = hmda_years(county_to_cbsa)
    print("Mortgage rate")
    rate_hist, rate, rate_day = mortgage_rate()
    print("Inflation (CPI)")
    cpi = cpi_inflation()

    print("Zillow")
    LV = (("county", "County"), ("metro", "Metro"), ("state", "State"))
    zv = {lvl: zillow(f"zhvi/{name}_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv", lvl) for lvl, name in LV}
    zs = {lvl: zillow(f"zhvi/{name}_zhvi_uc_sfr_tier_0.33_0.67_sm_sa_month.csv", lvl) for lvl, name in LV}
    zc = {lvl: zillow(f"zhvi/{name}_zhvi_uc_condo_tier_0.33_0.67_sm_sa_month.csv", lvl) for lvl, name in LV}
    zr = {lvl: zillow(f"zori/{name}_zori_uc_sfrcondomfr_sm_month.csv", lvl) for lvl, name in LV[:2]}
    zrs = {"metro": zillow("zori/Metro_zori_uc_sfr_sm_month.csv", "metro")}
    fc_raw = zillow("zhvf_growth/Metro_zhvf_growth_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv", "metro")
    zx = {key: {lvl: zillow(path.format(name), lvl) for lvl, name in LV} for key, path in ZILLOW_EXTRA.items()}
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
    for d in (zv, zs, zc, zr, zrs, *zx.values()):
        d["metro"] = zmetro(d["metro"])
    forecast = {}
    for cbsa, rec in zmetro(fc_raw).items():
        s = rec["series"]
        if s:
            forecast[cbsa] = s[max(s)]  # the last column is the 12-month-ahead forecast
    zillow_month = max((max(r["series"]) for r in zv["state"].values() if r["series"]), default=None)
    # Zillow only publishes today's forecast, so each one is saved to build up a history; earlier ones come once
    # from the Internet Archive's saved copies of Zillow's file.
    fc_hist = load_dcache("forecast.json") or {}
    if not fc_hist.get("_archive") and archive_forecasts(fc_hist, zmetro):
        fc_hist["_archive"] = 1
    for cbsa, v in forecast.items():
        fc_hist.setdefault(cbsa, {})[zillow_month] = round(v, 2)
    save_dcache("forecast.json", fc_hist)
    # Rent for houses: Zillow publishes single-family rents for metros only. Counties and ZIP codes get an estimate:
    # their own typical rent x their metro's house-to-all-homes rent ratio for the same month.
    rent_ratio = {}
    for cbsa, rec in zrs["metro"].items():
        allr = (zr["metro"].get(cbsa) or {}).get("series") or {}
        r = {k: v / allr[k] for k, v in (rec.get("series") or {}).items() if v and allr.get(k)}
        if r:
            rent_ratio[cbsa] = r

    # ----- yearly data points for every place -----
    metro_counties = {}
    for c, m in county_to_cbsa.items():
        metro_counties.setdefault(m, []).append(c)
    counties_by_state = {}
    for pid in R:
        if len(pid) == 5:
            counties_by_state.setdefault(pid[:2], []).append(pid)
    A = {"state": {}, "metro": {}, "county": {}}
    for pid, rec in R.items():
        if len(pid) == 5:
            A["county"][pid] = derive(rec)
    for st in STATES:
        summed = add_up(R, counties_by_state.get(st, []))
        own = R.get(st, {})
        for f in ("pop", "births", "deaths", "natural", "dom_mig", "intl_mig", "income", "poverty", "temp"):
            if own.get(f):
                summed[f] = own[f]  # the source's own state figures where it has them
        A["state"][st] = derive(summed)
    for cbsa, cs in metro_counties.items():
        A["metro"][cbsa] = derive(add_up(R, cs))
    for y, res in acs.items():
        for lvl in ("state", "county", "metro"):
            for pid, d in (res.get(lvl) or {}).items():
                rec = A[lvl].setdefault(pid, {})
                for k, v in d.items():
                    if not k.startswith("acs_"):
                        rec.setdefault(k, {})[str(y)] = v
    for y, res in hmda.items():
        for lvl in ("state", "county", "metro"):
            for pid, d in res.get(lvl, {}).items():
                rec = A[lvl].setdefault(pid, {})
                for k, v in d.items():
                    rec.setdefault(k, {})[str(y)] = tidy(v)
    print("Schools")
    sdir, sdir_year = school_directory()
    sscores = school_scores()
    srat = school_ratings(sdir, sscores, county_to_cbsa)
    for lvl in ("county", "metro"):
        for pid, s in srat[lvl].items():
            A[lvl].setdefault(pid, {})["school_score"] = s
    # Each ZIP's schools for the list in the side panel: [name, grades, students, score, charter], best first.
    latest_scores = sscores.get(max(sscores)) if sscores else {}
    zip_schools = {}
    for sid, (name, zp, county, lo, hi, enr, charter) in sdir.items():
        if zp:
            lo_l, hi_l = grade_label(lo), grade_label(hi)
            grades = f"{lo_l}–{hi_l}" if lo_l and hi_l and lo_l != hi_l else lo_l or hi_l or ""
            zip_schools.setdefault(zp, []).append([name, grades, enr, latest_scores.get(sid), charter])
    for lst in zip_schools.values():
        lst.sort(key=lambda s: (-(s[3] or 0), s[0]))
    print(f"  {len(sdir):,} schools ({sdir_year} list), scores for {sorted(sscores)}")

    print("Jobs and pay, flood insurance, noise")
    qcew, flood, noise = qcew_years(), flood_insurance(), noise_levels()
    for y, areas in qcew.items():
        for code, (emp, pay) in areas.items():  # (not "area": that name holds the land areas used for ZIP density)
            lvl, pid = ("metro", code[1:] + "0") if code.startswith("C") else ("state", code[:2]) if code.endswith("000") else ("county", code)
            a = A[lvl].setdefault(pid, {})
            a.setdefault("jobs", {})[str(y)] = emp
            if pay:
                a.setdefault("avg_pay", {})[str(y)] = pay
    for lvl in A:
        for a in A[lvl].values():
            for k, g in (("jobs", "job_growth"), ("avg_pay", "pay_growth")):
                s_ = a.get(k) or {}
                for y, v in s_.items():
                    if s_.get(str(int(y) - 1)):
                        a.setdefault(g, {})[y] = round((v / s_[str(int(y) - 1)] - 1) * 100, 2)
    print("Moves in and out (IRS), air quality (EPA)")
    mig_years, mig_flows, mig_flows_year = irs_migration(county_to_cbsa, pop_names)
    for y, res in mig_years.items():
        for lvl in ("county", "metro", "state"):
            for pid, d in res.get(lvl, {}).items():
                a = A[lvl].setdefault(pid, {})
                for k, v in d.items():
                    if v is not None:
                        a.setdefault(k, {})[y] = v
                p = (a.get("pop") or {}).get(y) or (a.get("pop") or {}).get(str(int(y) - 1))
                if p:
                    a.setdefault("mig_net_pct", {})[y] = round(d["mig_net"] / p * 100, 2)
    aqi = air_quality(pop_names)
    for y, d in aqi.items():
        for fips, (bad, med) in d.items():
            a = A["county"].setdefault(fips, {})
            a.setdefault("aqi_bad_days", {})[y] = bad
            if med is not None:
                a.setdefault("aqi_median", {})[y] = med
    # metros and states: population-weighted averages of their monitored counties
    st_groups = {}
    for f in A["county"]:
        st_groups.setdefault(f[:2], []).append(f)
    for lvl, groups in (("metro", metro_counties), ("state", st_groups)):
        for pid, members in groups.items():
            for y, d in aqi.items():
                for i, k in ((0, "aqi_bad_days"), (1, "aqi_median")):
                    acc = [(d[c][i], (A["county"].get(c, {}).get("pop") or {}).get(y) or latest_annual(A["county"].get(c, {})).get("pop") or 0)
                           for c in members if c in d and d[c][i] is not None]
                    acc = [(v, w) for v, w in acc if w]
                    if acc:
                        A[lvl].setdefault(pid, {}).setdefault(k, {})[y] = round(sum(v * w for v, w in acc) / sum(w for _, w in acc), 1)
    print("Cost of living (BEA), price history (FHFA), Fair Market Rents (HUD)")
    rpp = price_parities()
    for lvl in ("state", "metro"):
        for pid, d in rpp[lvl].items():
            A[lvl].setdefault(pid, {}).update(d)
    for f in list(A["county"]):  # counties use their metro's figures, or their state's figures outside metros
        src = rpp["metro"].get(county_to_cbsa.get(f)) or rpp["nonmetro"].get(f[:2])
        if src:
            A["county"][f].update(src)
    for lvl in A:
        for a in A[lvl].values():
            inc, r = a.get("income"), a.get("rpp_all")
            if inc and r:
                a["income_real"] = {y: round(v / r[y] * 100) for y, v in inc.items() if r.get(y)}
    hpi = fhfa_hpi()
    for lvl in ("state", "metro", "county"):
        for pid, s in (hpi.get(lvl) or {}).items():
            if lvl != "county" or pid[:2] in STATES:
                A[lvl].setdefault(pid, {})["hpi_change"] = hpi_change(s)
    hud = hud_fmr()
    for y, d in (hud.get("years") or {}).items():
        for f, vals in d.items():
            if f[:2] in STATES:
                a = A["county"].setdefault(f, {})
                for i, k in enumerate(FMR_KEYS):
                    a.setdefault(k, {})[y] = vals[i]
        cpop = {f: latest_annual(A["county"].get(f, {})).get("pop") or 0 for f in d}
        for lvl, groups in (("metro", metro_counties), ("state", {s: [f for f in d if f[:2] == s] for s in STATES})):
            for pid, members in groups.items():
                acc = [(d[f], cpop.get(f) or 0) for f in members if f in d and cpop.get(f)]
                tw = sum(w for _, w in acc)
                if tw:
                    a = A[lvl].setdefault(pid, {})
                    for i, k in enumerate(FMR_KEYS):
                        a.setdefault(k, {})[y] = round(sum(v[i] * w for v, w in acc) / tw)
    # flood insurance: shares of homes in high-risk zones and insured (counts add up to metros and states), and claims
    flood_year = (flood.get("built") or str(THIS_YEAR))[:4]
    pen = flood.get("pen") or {}
    groups_of = {"county": {f: [f] for f in pen}, "metro": metro_counties, "state": {}}
    for f in pen:
        groups_of["state"].setdefault(f[:2], []).append(f)
    for lvl, groups in groups_of.items():
        for pid, members in groups.items():
            tot = [sum(pen[c][i] for c in members if c in pen) for i in range(4)]
            if tot[2]:
                a = A[lvl].setdefault(pid, {})
                a["flood_ins_pct"] = {flood_year: round(tot[0] / tot[2] * 100, 2)}
                a["flood_zone_pct"] = {flood_year: round(tot[3] / tot[2] * 100, 2)}
    claims_county = flood.get("county") or {}
    by_state_all = {}
    for f in A["county"]:
        by_state_all.setdefault(f[:2], []).append(f)
    for lvl, groups in (("county", {f: [f] for f in A["county"]}), ("metro", metro_counties), ("state", by_state_all)):
        for pid, members in groups.items():
            years = {}
            for c in members:
                for y, (n, paid) in (claims_county.get(c) or {}).items():
                    acc = years.setdefault(y, [0, 0])
                    acc[0] += n
                    acc[1] += paid
            if years:
                homes = latest_annual(A[lvl].get(pid, {})).get("units")
                A[lvl].setdefault(pid, {}).update(flood_metrics(years, homes, THIS_YEAR - 1))
    # noise: counties from the tract figures; metros and states weighted by population
    for fips, d in (noise.get("county") or {}).items():
        a = A["county"].setdefault(fips, {})
        for k, v in d.items():
            if v is not None:
                a[k] = {"2020": v}
    for lvl, groups in (("metro", metro_counties), ("state", by_state_all)):
        for pid, members in groups.items():
            for k in ("noise_db", "noise_loud"):
                acc = [((noise.get("county") or {}).get(c, {}).get(k), latest_annual(A["county"].get(c, {})).get("pop") or 0) for c in members]
                acc = [(v, w) for v, w in acc if v is not None and w]
                if acc:
                    A[lvl].setdefault(pid, {})[k] = {"2020": round(sum(v * w for v, w in acc) / sum(w for _, w in acc), 1)}

    print("Natural hazards and walkability")
    haz, walk = hazards(), walkability()
    haz_year = (re.findall(r"\d{4}", haz.get("ver") or "") or [str(THIS_YEAR)])[0]
    county_pop = {c: latest_annual(a).get("pop") or 0 for c, a in A["county"].items()}
    for fips, rec in haz.get("county", {}).items():
        a = A["county"].setdefault(fips, {})
        for k, v in rec.items():
            if k != "pop":
                a[k] = {haz_year: v}
        county_pop.setdefault(fips, rec.get("pop") or 0)
    for fips, w in walk.get("county", {}).items():
        A["county"].setdefault(fips, {})["walk"] = {"2021": w}
    # Metros and states: population-weighted averages of their counties.
    haz_keys = list(NRI_FIELDS) + ["walk"]
    by_state = {}
    for fips in A["county"]:
        by_state.setdefault(fips[:2], []).append(fips)
    for lvl, groups in (("metro", metro_counties), ("state", by_state)):
        for pid, members in groups.items():
            for k in haz_keys:
                acc = [(next(iter(A["county"][c][k].values())), county_pop.get(c) or 1) for c in members if c in A["county"] and A["county"][c].get(k)]
                if acc:
                    yr = next(iter(A["county"][[c for c in members if c in A["county"] and A["county"][c].get(k)][0]][k]))
                    A[lvl].setdefault(pid, {})[k] = {yr: round(sum(v * w for v, w in acc) / sum(w for _, w in acc), 1)}
    for lvl in A:
        growth_scores(A[lvl])

    def yearly_tax(a, z):
        """Property tax on the typical home: that year's tax rate x the home value that December."""
        rates = a.get("prop_tax_rate")
        if not rates or not z:
            return
        for y, r in rates.items():
            hv = z.get(f"{y}-12") or latest({k: v for k, v in z.items() if k <= f"{y}-12"})[1]
            if hv:
                a.setdefault("prop_tax", {})[y] = tidy(hv * r / 100)

    places = {"state": {}, "metro": {}, "county": {}}
    hist = {"state": {}, "metro": {}, "county": {}}
    HSTART = "2000-01"
    srcs = lambda d, lvl, pid: (d.get(lvl) or {}).get(pid, {}).get("series")

    # States: Zillow has no state forecast, so each month's is the average of the state's metros, weighted by population.
    state_fc = {}
    for fips, (abbr, _) in STATES.items():
        ms = [c for c, n in metro_names.items() if n.rsplit(", ", 1)[-1].split("-")[0] == abbr and c in fc_hist]
        wts = {c: latest_annual(A["metro"].get(c, {})).get("pop") or 0 for c in ms}
        series = {}
        for month in {mm for c in ms for mm in fc_hist[c]}:
            pairs = [(fc_hist[c][month], wts[c]) for c in ms if month in fc_hist[c] and wts[c]]
            tw = sum(w for _, w in pairs)
            if tw:
                series[month] = round(sum(v * w for v, w in pairs) / tw, 2)
        if series:
            state_fc[fips] = series

    def build(lvl, pid, name, st, extra, fc_key):
        a = A[lvl].get(pid, {})
        z = srcs(zv, lvl, pid)
        hl = hpi_long((hpi.get(lvl) or {}).get(pid), z)
        if hl:
            a = A[lvl].setdefault(pid, a)
            a["hv_long"] = hl
        la = latest_annual(a)
        yearly_tax(a, z)
        fc_series = state_fc.get(pid) if lvl == "state" else fc_hist.get(fc_key)
        rs = srcs(zrs, lvl, pid) or (house_rent(srcs(zr, lvl, pid), rent_ratio.get(fc_key)) if lvl == "county" else None)
        m = metrics_for(z, srcs(zr, lvl, pid), rt[lvl].get(pid), la.get("income"), rate, (fc_series or {}).get(zillow_month),
                        srcs(zs, lvl, pid), srcs(zc, lvl, pid), rs, la.get("units"), la.get("prop_tax_rate"), a.get("income"))
        xs = {key: srcs(zx[key], lvl, pid) for key in ZILLOW_EXTRA}
        m.update(extra_metrics(xs))
        v = {**{k: x for k, x in la.items() if k not in ("prop_tax",)}, **m}
        if not v:
            return
        places[lvl][pid] = {"n": name, "s": st, "v": v, **extra}
        b = {}
        for key, src in (("hv", zv), ("sfr", zs), ("condo", zc), ("rent", zr)):
            s = srcs(src, lvl, pid)
            if s:
                b[key] = trim_series(s, HSTART)
        if rs:
            b["rent_sfr"] = trim_series(rs, HSTART)
        rr = rt[lvl].get(pid)
        if rr:
            for k in ("inv", "dom", "cut", "lp", "newl", "pend", "ppsf", "sqft", "inc"):
                if rr.get(k):
                    b[k] = trim_series(rr[k], HSTART)
        if fc_series:
            b["fc"] = dict(sorted(fc_series.items()))
        for key, sx in xs.items():
            if sx:
                b[key] = trim_series(sx, HSTART)
        if a:
            b["a"] = a
        if lvl == "county" and mig_flows.get(pid):
            b["mig"] = mig_flows[pid]  # top places people came from and moved to
        hist[lvl][pid] = b

    for fips, (abbr, name) in STATES.items():
        build("state", fips, name, abbr, {}, fips)
    for cbsa, name in metro_names.items():
        build("metro", cbsa, name, name.rsplit(", ", 1)[-1].split("-")[0], {"c": sorted(metro_counties.get(cbsa, []))}, cbsa)
    county_ids = set(zv["county"]) | set(rt["county"]) | {k for k in R if len(k) == 5}
    for fips in sorted(county_ids):
        if fips[:2] not in STATES:
            continue
        cbsa = county_to_cbsa.get(fips)
        build("county", fips, county_name(fips, zv, rt, pop_names), STATES[fips[:2]][0], {"m": cbsa} if cbsa else {}, cbsa)

    # Yearly snapshots for the map's time slider: each December's home value and its 1-year change (latest month for
    # this year), for every state, metro and county.
    last_year = int(zillow_month[:4])
    years = list(range(2000, last_year + 1))
    for lvl in ("state", "metro", "county"):
        ts_hv, ts_yoy = {}, {}
        for pid in places[lvl]:
            s = srcs(zv, lvl, pid)
            if not s:
                continue
            hv_row, yoy_row = [], []
            for y in years:
                k = zillow_month if y == last_year else f"{y}-12"
                v, old = s.get(k), s.get(months_back(k, 12))
                hv_row.append(round(v) if v else None)
                yoy_row.append(round((v / old - 1) * 100, 1) if v and old else None)
            if any(hv_row):
                ts_hv[pid], ts_yoy[pid] = hv_row, yoy_row
        write_json(os.path.join(OUT, "time", f"{lvl}.json"), {"years": years, "last": zillow_month, "hv": ts_hv, "hv_yoy": ts_yoy})

    realtor_month = max((max(r["inv"]) for r in rt["state"].values() if r["inv"]), default=None)
    meta = {
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "months": {"zillow": zillow_month, "realtor": realtor_month, "income": inc_year, "population": pop_year},
        "years": {"pop": pop_year, "age": age_year, "income": inc_year, "permits": permit_year, "units": unit_year, "temp": temp_year,
                  "votes": vote_year, "acs": acs_year, "hmda": hmda_year_, "schools": max(sscores) if sscores else None,
                  "school_list": sdir_year, "hazards": haz.get("ver"), "walk": 2021, "jobs": max(qcew) if qcew else None,
                  "migration": max(mig_years) if mig_years else None, "air": max(aqi) if aqi else None,
                  "hud": max(hud.get("years") or {"": 0}) or None},
        "mortgage": {"rate": rate, "date": rate_day, "history": trim_series(rate_hist, "2000-01")},
        "cpi": {k: v for k, v in cpi.items() if k >= "2000-01"},
        "counts": {k: len(v) for k, v in places.items()},
    }
    for lvl, ps in places.items():
        write_json(os.path.join(OUT, f"{lvl}.json"), ps)
        for pid, bundle in hist[lvl].items():  # one small file per place, loaded when you click it
            write_json(os.path.join(OUT, "hist", lvl, f"{pid}.json"), bundle)
    print(f"Done: {meta['counts']} - Zillow {zillow_month}, Realtor.com {realtor_month}, mortgage {rate}% ({rate_day})")
    try:
        write_pages(places, hist, meta, metro_names, metro_counties, county_to_cbsa)
    except Exception as e:  # the pages are extra: never let them stop the data update
        import traceback
        traceback.print_exc()
        print(f"  (pages not written: {e})")

    # ZIP codes: the biggest files, so the state/metro/county data is let go of first.
    county_income = {pid: latest_annual(a).get("income") for pid, a in A["county"].items()}
    county_inc_hist = {pid: a["income"] for pid, a in A["county"].items() if a.get("income")}
    # ZIP codes show their county's weather and election results (neither is published by ZIP).
    # (and the county's moves in and out, and its air quality: neither is published by ZIP either)
    shared_keys = ("temp", "vote_r", "vote_d", "mig_in", "mig_out", "mig_net", "mig_net_pct", "mig_in_income", "mig_out_income",
                   "aqi_bad_days", "aqi_median", "rpp_all", "rpp_housing") + FMR_KEYS
    county_shared = {pid: {k: a[k] for k in shared_keys if a.get(k)} for pid, a in A["county"].items()}
    acs_zip = {y: res.get("zip", {}) for y, res in acs.items() if res.get("zip")}
    del zv, zs, zc, zr, zrs, rt, hist, A, R
    zip_count, zip_breaks = build_zips(county_to_cbsa, county_income, rate, fc_hist, zillow_month, acs_zip, area, county_shared,
                                       srat["zip"], zip_schools, haz.get("zip", {}), haz_year, walk.get("zip", {}),
                                       flood.get("zip") or {}, noise.get("zip") or {}, rent_ratio, county_inc_hist,
                                       hpi.get("zip") or {}, hud.get("zip") or {}, hud.get("zip_year"))
    meta["counts"]["zip"] = zip_count
    meta["zipBreaks"] = zip_breaks
    write_json(os.path.join(OUT, "meta.json"), meta)
    print(f"ZIP codes: {zip_count}")
    data_check(meta, cpi, sscores)


# ---------- search-friendly pages (one per state, metro and county) ----------
SITE_URL = "https://openhousingatlas.com"
PAGE_SECTIONS = [
    ("Prices and affordability", [("hv", "Typical home value", "usd"), ("hv_yoy", "Change in 1 year", "spct"), ("hv_5y", "Change in 5 years", "spct"),
                                  ("fair_value", "Fair home value", "usd"), ("overvalued", "Overvalued %", "spct"),
                                  ("fc_1y", "Zillow forecast, next 12 months", "spct"), ("hv_income", "Home value to income", "x"),
                                  ("mtg_pmt", "Monthly mortgage payment (20% down)", "usd"), ("salary_needed", "Income needed to afford it", "usd"),
                                  ("prop_tax_rate", "Property tax rate", "pct2")]),
    ("Rent and investing", [("rent", "Typical rent", "usd"), ("rent_sfr", "Typical rent for houses", "usd"), ("fmr_2br", "HUD Fair Market Rent, 2 bedrooms", "usd"),
                            ("price_rent", "Price-to-rent ratio", "x"), ("gross_yield", "Gross rental yield", "pct2"), ("cap_rate", "Cap rate (estimate)", "pct2")]),
    ("Market activity", [("inv", "Homes for sale", "num"), ("inv_yoy", "Listings vs a year ago", "spct"), ("dom", "Days on market", "num"),
                         ("cut", "Listings with a price cut", "pct"), ("pending_ratio", "Pending sales per 100 listings", "num"), ("ppsf", "Asking price per sq ft", "usd")]),
    ("People and jobs", [("pop", "Population", "num"), ("pop_growth", "Population change (1 year)", "spct"), ("income", "Median household income", "usd"),
                         ("income_real", "Income adjusted for cost of living", "usd"), ("unemployment", "Unemployment rate", "pct"),
                         ("mig_net", "Net moves in (IRS)", "snum"), ("rpp_all", "Cost of living (US = 100)", "n1")]),
    ("Schools, risk and environment", [("school_score", "School rating (1 to 10)", "n1"), ("risk", "FEMA hazard risk score (0 to 100)", "n1"),
                                       ("flood_risk", "Flood risk score", "n1"), ("fire_risk", "Wildfire risk score", "n1"),
                                       ("aqi_bad_days", "Unhealthy air days a year", "num"), ("walk", "Walkability (1 to 20)", "n1"),
                                       ("noise_db", "Road, rail and air noise", "db")]),
]
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def _slug(s):
    import unicodedata
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def _fmt(v, kind):
    if v is None:
        return "—"
    if kind == "usd":
        return f"${v:,.0f}"
    if kind == "spct":
        return f"{'+' if v > 0 else '−' if v < 0 else ''}{abs(v):.1f}%"
    if kind == "pct":
        return f"{v:.1f}%"
    if kind == "pct2":
        return f"{v:.2f}%"
    if kind == "x":
        return f"{v:.1f}×"
    if kind == "num":
        return f"{v:,.0f}"
    if kind == "snum":
        return f"{'+' if v > 0 else '−' if v < 0 else ''}{abs(v):,.0f}"
    if kind == "n1":
        return f"{v:.1f}"
    if kind == "db":
        return f"{v:.0f} dB"
    return str(v)


def _chart_svg(points, fair):
    """A small line chart (inline SVG): typical home value by year, and fair value as a dashed line."""
    if len(points) < 3:
        return ""
    W, H, L, R, T, B = 640, 210, 58, 12, 12, 26
    ys = [v for _, v in points] + [v for _, v in fair]
    lo, hi = 0, max(ys) * 1.06
    x0, x1 = points[0][0], points[-1][0]
    X = lambda y: L + (W - L - R) * (y - x0) / max(1, x1 - x0)
    Y = lambda v: T + (H - T - B) * (1 - (v - lo) / (hi - lo))
    path = lambda pts: " ".join(f"{X(y):.1f},{Y(v):.1f}" for y, v in pts)
    step = 10 ** max(0, len(str(int(hi))) - 1)
    for m in (1, 2, 2.5, 5, 10):
        if hi / (step * m / 10) <= 6:
            step = step * m / 10
            break
    ticks = [t * step for t in range(0, int(hi // step) + 1)]
    lab = lambda v: f"${v / 1e6:.1f}M" if v >= 1e6 else f"${v / 1e3:.0f}K"
    years = [y for y in range(x0, x1 + 1) if y % (10 if x1 - x0 > 30 else 5) == 0]
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Typical home value by year">']
    out += [f'<line x1="{L}" x2="{W - R}" y1="{Y(t):.1f}" y2="{Y(t):.1f}" stroke="#e7e2d8"/><text x="{L - 6}" y="{Y(t) + 4:.1f}" text-anchor="end">{lab(t)}</text>' for t in ticks]
    out += [f'<text x="{X(y):.1f}" y="{H - 6}" text-anchor="middle">{y}</text>' for y in years]
    if len(fair) > 2:
        out.append(f'<polyline fill="none" stroke="#8a94a3" stroke-width="2" stroke-dasharray="5 4" points="{path(fair)}"/>')
    out.append(f'<polyline fill="none" stroke="#126b60" stroke-width="2.4" points="{path(points)}"/>')
    out.append("</svg>")
    return "".join(out)


def _summary(name, v, rate):
    """Plain-English sentences built from the numbers (a template, not generated text), plus FAQ answers."""
    s, faq = [], []
    hv, yoy = v.get("hv"), v.get("hv_yoy")
    if hv:
        trend = "about the same as" if yoy is None or abs(yoy) < 0.5 else f"{'up' if yoy > 0 else 'down'} {abs(yoy):.1f}% from"
        s.append(f"The typical home in {name} is worth {_fmt(hv, 'usd')}, {trend} a year ago.")
    ov = v.get("overvalued")
    if ov is not None:
        word = ("well above" if ov > 10 else "slightly above" if ov > 0 else "slightly below" if ov > -10 else "well below")
        s.append(f"That is {word} what local incomes have historically supported: fair value is about {_fmt(v.get('fair_value'), 'usd')}, "
                 f"so the area is {abs(ov):.1f}% {'overvalued' if ov > 0 else 'undervalued'}.")
        faq.append((f"Is {name} overvalued?", s[-1].replace("That is", f"Home prices in {name} are")))
    ms, inv_yoy, dom = v.get("months_supply"), v.get("inv_yoy"), v.get("dom")
    buyer = sum([bool(ms and ms > 4), bool(inv_yoy and inv_yoy > 10), bool(yoy is not None and yoy < 0), bool(v.get("cut") and v["cut"] > 20)])
    seller = sum([bool(ms and ms < 2.5), bool(inv_yoy is not None and inv_yoy < 0), bool(yoy and yoy > 4), bool(dom and dom < 30)])
    if ms or dom:
        kind = "a buyer's market" if buyer - seller >= 2 else "a seller's market" if seller - buyer >= 2 else \
            "a balanced market, leaning toward buyers" if buyer > seller else "a balanced market, leaning toward sellers" if seller > buyer else "a balanced market"
        bits = [f"homes take about {dom:.0f} days to sell" if dom else "",
                f"listings are {'up' if inv_yoy > 0 else 'down'} {abs(inv_yoy):.0f}% from a year ago" if inv_yoy is not None else "",
                f"{v['cut']:.0f}% of listings have had a price cut" if v.get("cut") is not None else ""]
        bits = [b for b in bits if b]
        s.append(f"It is {kind}: " + ", ".join(bits) + "." if bits else f"It is {kind}.")
        faq.append((f"Is {name} a buyer's or seller's market?", s[-1].replace("It is", f"{name} is", 1)))
    if yoy is not None:
        fc = v.get("fc_1y")
        ans = f"Home values in {name} are {'down' if yoy < 0 else 'up'} {abs(yoy):.1f}% over the last year."
        if fc is not None:
            ans += f" Zillow's forecast for the next 12 months is {_fmt(fc, 'spct')}."
        faq.append((f"Are home prices going down in {name}?", ans))
        if fc is not None:
            s.append(f"Zillow expects prices to {'rise' if fc > 0 else 'fall'} about {abs(fc):.1f}% over the next year.")
    sal = v.get("salary_needed")
    if sal:
        inc = v.get("income")
        txt = (f"A household needs about {_fmt(sal, 'usd')} a year to afford the typical home here at today's {rate}% mortgage rate "
               f"(20% down, payment at 30% of income)" + (f"; the median household earns {_fmt(inc, 'usd')}." if inc else "."))
        s.append(txt)
        faq.append((f"How much do you need to earn to buy a home in {name}?", txt.replace(" here", f" in {name}")))
    if v.get("rent"):
        s.append(f"Typical rent is {_fmt(v['rent'], 'usd')} a month" + (f", or {_fmt(v['rent_sfr'], 'usd')} for a house." if v.get("rent_sfr") else "."))
    if v.get("mig_net") is not None:
        n = v["mig_net"]
        s.append(f"In the latest IRS figures, {abs(n):,.0f} more people moved {'in than out' if n > 0 else 'out than in'}.")
    return s, faq


PAGE_CSS = """*{box-sizing:border-box}body{margin:0;font:16px/1.6 Inter,'Segoe UI',system-ui,sans-serif;color:#1d1b17;background:#faf7f1}
a{color:#126b60}header.top{background:#fff;border-bottom:1px solid #e7e2d8}header.top div{max-width:960px;margin:0 auto;padding:12px 20px;display:flex;justify-content:space-between;align-items:center;gap:12px}
.logo{font:600 20px Fraunces,Georgia,serif;color:#06463f;text-decoration:none}.btn{background:#126b60;color:#fff;padding:8px 14px;border-radius:999px;text-decoration:none;font-weight:600;font-size:14px;white-space:nowrap}
main{max-width:960px;margin:0 auto;padding:20px}.crumbs{font-size:13px;color:#6b6457}.crumbs a{color:#6b6457}
h1{font:600 32px/1.2 Fraunces,Georgia,serif;margin:6px 0 4px;color:#06463f}h2{font:600 21px Fraunces,Georgia,serif;margin:28px 0 10px;color:#06463f}
.sub{color:#6b6457;font-size:14px;margin:0 0 18px}.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}
.kpi{background:#fff;border:1px solid #e7e2d8;border-radius:12px;padding:12px 14px}.kpi span{display:block;font-size:13px;color:#6b6457}.kpi b{font-size:22px}
.kpi small{display:block;font-size:13px}.up{color:#9c2f1c}.dn{color:#0b5d5a}.lede{background:#fff;border:1px solid #e7e2d8;border-radius:12px;padding:16px 18px;margin-top:16px}
.chart{background:#fff;border:1px solid #e7e2d8;border-radius:12px;padding:12px 14px}.chart svg{width:100%;height:auto;font-size:11px;fill:#6b6457}
.legend{font-size:12.5px;color:#6b6457}table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e7e2d8;border-radius:12px;overflow:hidden}
td{padding:8px 14px;border-bottom:1px solid #efeae0;font-size:15px}td:last-child{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}
.links{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:15px}.faq dt{font-weight:600;margin-top:12px}.faq dd{margin:4px 0 0}
footer{max-width:960px;margin:30px auto 0;padding:18px 20px 40px;font-size:13px;color:#6b6457;border-top:1px solid #e7e2d8}
@media(max-width:600px){h1{font-size:26px}}"""


def write_pages(places, hist, meta, metro_names, metro_counties, county_to_cbsa):
    """One static, search-friendly page per state, metro and county (site/state/…, site/metro/…, site/county/…),
    a list of every market (site/markets/), sitemap.xml and robots.txt. Rebuilt on every run from the same data."""
    from html import escape as esc
    import shutil
    root = os.path.dirname(OUT)
    for d in ("state", "metro", "county", "markets"):
        shutil.rmtree(os.path.join(root, d), ignore_errors=True)
    zm = meta["months"].get("zillow") or ""
    data_month = f"{MONTH_NAMES[int(zm[5:7]) - 1]} {zm[:4]}" if zm else ""
    built = meta["built"][:10]
    rate = meta["mortgage"]["rate"]
    st_name = {f: n for f, (a, n) in STATES.items()}
    st_abbr = {f: a for f, (a, n) in STATES.items()}
    try:
        zip_county = zcta_counties()
    except Exception:
        zip_county = {}
    zips_of = {}
    for z, c in zip_county.items():
        zips_of.setdefault(c, []).append(z)

    def url(lvl, pid):
        p = places[lvl][pid]
        if lvl == "state":
            return f"/state/{_slug(st_name.get(pid, p['n']))}/"
        if lvl == "metro":
            return f"/metro/{_slug(p['n'])}/"
        return f"/county/{st_abbr[pid[:2]].lower()}/{_slug(p['n'])}/"

    def title(lvl, pid):
        p = places[lvl][pid]
        return st_name.get(pid, p["n"]) if lvl == "state" else p["n"] if lvl == "metro" else f"{p['n']}, {p['s']}"

    used, urls = set(), []
    for lvl in ("state", "metro", "county"):
        for pid, p in places[lvl].items():
            if lvl == "county" and pid[:2] not in STATES:
                continue
            if not p["v"].get("hv") and sum(1 for x in p["v"].values() if x is not None) < 15:
                continue  # too little data for a useful page (search engines penalise near-empty pages)
            u = url(lvl, pid)
            if u in used:  # two places with the same name: keep both by adding the code
                u = u.rstrip("/") + f"-{pid}/"
            used.add(u)
            p["_url"] = u
    for lvl in ("state", "metro", "county"):
        for pid, p in places[lvl].items():
            if "_url" not in p:
                continue
            v, name = p["v"], title(lvl, pid)
            b = hist[lvl].get(pid, {})
            a = b.get("a") or {}
            # chart: FHFA history since 1975 (scaled to Zillow), else Zillow yearly averages; fair value from income
            if a.get("hv_long"):
                pts = sorted((int(y), val) for y, val in a["hv_long"].items())
            else:
                yrs = {}
                for k, val in (b.get("hv") or {}).items():
                    yrs.setdefault(int(k[:4]), []).append(val)
                pts = sorted((y, sum(vs) / len(vs)) for y, vs in yrs.items())
            fair = []
            r = fair_ratio(b.get("hv") or {}, a.get("income"))
            if r:
                fair = [(y, income_month(a["income"], f"{y}-07") * r) for y, _ in pts if income_month(a["income"], f"{y}-07")]
            chart = _chart_svg(pts, fair)
            lines, faq = _summary(name, v, rate)
            # breadcrumbs and links
            crumbs = [("Open Housing Atlas", "/"), ("All markets", "/markets/")]
            if lvl != "state":
                st = pid[:2] if lvl == "county" else next((c[:2] for c in metro_counties.get(pid, [])), None)
                if st and st in places["state"]:
                    crumbs.append((st_name[st], places["state"][st]["_url"]))
            if lvl == "county" and county_to_cbsa.get(pid) in places["metro"] and "_url" in places["metro"][county_to_cbsa[pid]]:
                m = county_to_cbsa[pid]
                crumbs.append((places["metro"][m]["n"], places["metro"][m]["_url"]))
            link_groups = []
            if lvl == "state":
                ms = sorted((m for m in places["metro"] if "_url" in places["metro"][m] and any(c[:2] == pid for c in metro_counties.get(m, []))),
                            key=lambda m: -(places["metro"][m]["v"].get("pop") or 0))
                cs = sorted((c for c in places["county"] if c[:2] == pid and "_url" in places["county"][c]), key=lambda c: places["county"][c]["n"])
                link_groups += [("Metro areas", [(places["metro"][m]["n"], places["metro"][m]["_url"]) for m in ms]),
                                ("Counties", [(places["county"][c]["n"], places["county"][c]["_url"]) for c in cs])]
            elif lvl == "metro":
                cs = sorted((c for c in metro_counties.get(pid, []) if c in places["county"] and "_url" in places["county"][c]),
                            key=lambda c: -(places["county"][c]["v"].get("pop") or 0))
                link_groups.append(("Counties in this metro", [(title("county", c), places["county"][c]["_url"]) for c in cs]))
            else:
                m = county_to_cbsa.get(pid)
                near = [c for c in metro_counties.get(m, []) if c != pid and c in places["county"] and "_url" in places["county"][c]]
                if len(near) < 6 and v.get("hv"):
                    same = [c for c in places["county"] if c[:2] == pid[:2] and c != pid and c not in near and "_url" in places["county"][c]
                            and places["county"][c]["v"].get("hv")]
                    same.sort(key=lambda c: abs(places["county"][c]["v"]["hv"] - v["hv"]))
                    near += same[: 8 - len(near)]
                link_groups.append(("Nearby and similar counties", [(title("county", c), places["county"][c]["_url"]) for c in near[:10]]))
                zl = sorted(zips_of.get(pid, []))
                if zl:
                    link_groups.append((f"ZIP codes in {p['n']} (open on the map)", [(z, f"/#zip/{z}") for z in zl]))
            kpi = lambda k, label, kind, note="": (f'<div class="kpi"><span>{label}</span><b>{_fmt(v[k], kind)}</b>{note}</div>' if v.get(k) is not None else "")
            yo = v.get("hv_yoy")
            ov = v.get("overvalued")
            kpis = "".join([
                kpi("hv", "Typical home value", "usd", f'<small class="{"up" if (yo or 0) > 0 else "dn"}">{_fmt(yo, "spct")} in a year</small>' if yo is not None else ""),
                kpi("fair_value", "Fair home value", "usd", f'<small class="{"up" if (ov or 0) > 0 else "dn"}">{abs(ov):.1f}% {"overvalued" if ov > 0 else "undervalued"}</small>' if ov is not None else ""),
                kpi("rent", "Typical rent", "usd"),
                kpi("dom", "Days on market", "num"),
                kpi("fc_1y", "Forecast, next 12 months", "spct"),
            ])
            tables = ""
            for title_s, rows in PAGE_SECTIONS:
                trs = "".join(f"<tr><td>{lab}</td><td>{_fmt(v[k], kind)}</td></tr>" for k, lab, kind in rows if v.get(k) is not None)
                if trs:
                    tables += f"<h2>{title_s}</h2><table>{trs}</table>"
            links = "".join(f"<h2>{esc(t)}</h2><div class=\"links\">" + "".join(f'<a href="{h}">{esc(n)}</a>' for n, h in items) + "</div>"
                            for t, items in link_groups if items)
            faq_html = "".join(f"<dt>{esc(q)}</dt><dd>{esc(ans)}</dd>" for q, ans in faq)
            ld = [{"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
                {"@type": "ListItem", "position": i + 1, "name": n, "item": SITE_URL + h} for i, (n, h) in enumerate(crumbs + [(name, p["_url"])])]}]
            if faq:
                ld.append({"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
                    {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": ans}} for q, ans in faq]})
            desc = (f"{name} housing market: typical home value {_fmt(v.get('hv'), 'usd')}"
                    + (f" ({_fmt(yo, 'spct')} in a year)" if yo is not None else "")
                    + (f", fair value {_fmt(v.get('fair_value'), 'usd')}" if v.get("fair_value") else "")
                    + (f", rent {_fmt(v.get('rent'), 'usd')}" if v.get("rent") else "")
                    + ". Forecast, schools, risk and more. Free, updated twice a month.")
            hash_link = f"/#{lvl}/{pid}"
            page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(name)} Housing Market: Home Prices, Rent and Forecast ({zm[:4]}) | Open Housing Atlas</title>
<meta name="description" content="{esc(desc)}"><link rel="canonical" href="{SITE_URL}{p['_url']}">
<meta property="og:title" content="{esc(name)} housing market"><meta property="og:description" content="{esc(desc)}">
<meta property="og:url" content="{SITE_URL}{p['_url']}"><meta property="og:image" content="{SITE_URL}/og.png"><meta name="twitter:card" content="summary_large_image">
<link rel="preconnect" href="https://fonts.googleapis.com"><link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600&display=swap" rel="stylesheet">
<style>{PAGE_CSS}</style><script type="application/ld+json">{json.dumps(ld, separators=(",", ":"))}</script>
<script data-goatcounter="https://openhousingatlas.goatcounter.com/count" async src="//gc.zgo.at/count.js"></script></head><body>
<header class="top"><div><a class="logo" href="/">Open Housing Atlas</a><a class="btn" href="{hash_link}">Open on the map</a></div></header>
<main><nav class="crumbs">{" › ".join(f'<a href="{h}">{esc(n)}</a>' for n, h in crumbs)}</nav>
<h1>{esc(name)} housing market</h1><p class="sub">Data through {data_month} · updated {built} · free, from Zillow, Realtor.com, the Census Bureau and other public sources</p>
<div class="kpis">{kpis}</div>
{f'<div class="lede"><p style="margin:0">{esc(" ".join(lines))}</p></div>' if lines else ""}
{f'<h2>Home value over time</h2><div class="chart">{chart}<div class="legend">Solid: typical home value · dashed: fair value (household income × the 20-year average price-to-income ratio). Sources: FHFA, Zillow, U.S. Census Bureau.</div></div>' if chart else ""}
{tables}
{f'<h2>Questions people ask</h2><dl class="faq">{faq_html}</dl>' if faq_html else ""}
{links}
<p style="margin-top:26px"><a class="btn" href="{hash_link}">See every chart for {esc(name)} on the map</a></p></main>
<footer>Open Housing Atlas is free, with no ads. Data: Zillow Research, Realtor.com, U.S. Census Bureau, BLS, IRS, FEMA, EPA, FHFA, BEA, HUD, U.S. Department of Education, Freddie Mac.
Estimates are rough guides, not appraisals or financial advice. <a href="https://ko-fi.com/chrisbogh">Support this site on Ko-fi</a>.</footer></body></html>"""
            path = os.path.join(root, *p["_url"].strip("/").split("/"), "index.html")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(page)
            urls.append(p["_url"])
    # every market, by state
    rows = []
    for st in sorted(places["state"], key=lambda s: st_name.get(s, "")):
        if "_url" not in places["state"][st]:
            continue
        cs = sorted((c for c in places["county"] if c[:2] == st and "_url" in places["county"][c]), key=lambda c: places["county"][c]["n"])
        rows.append(f'<h2><a href="{places["state"][st]["_url"]}">{esc(st_name[st])}</a></h2><div class="links">'
                    + "".join(f'<a href="{places["county"][c]["_url"]}">{esc(places["county"][c]["n"])}</a>' for c in cs) + "</div>")
    ms = sorted((m for m in places["metro"] if "_url" in places["metro"][m]), key=lambda m: places["metro"][m]["n"])
    rows.append('<h2>Metro areas</h2><div class="links">' + "".join(f'<a href="{places["metro"][m]["_url"]}">{esc(places["metro"][m]["n"])}</a>' for m in ms) + "</div>")
    hub = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Housing markets by state, metro and county ({zm[:4]}) | Open Housing Atlas</title><meta name="description" content="Home values, rent, forecasts and more for every US state, metro area and county. Free, updated twice a month.">
<link rel="canonical" href="{SITE_URL}/markets/"><link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600&display=swap" rel="stylesheet"><style>{PAGE_CSS}</style>
<script data-goatcounter="https://openhousingatlas.goatcounter.com/count" async src="//gc.zgo.at/count.js"></script></head><body>
<header class="top"><div><a class="logo" href="/">Open Housing Atlas</a><a class="btn" href="/">Open the map</a></div></header>
<main><h1>US housing markets</h1><p class="sub">Every state, metro area and county · data through {data_month}</p>{"".join(rows)}</main></body></html>"""
    os.makedirs(os.path.join(root, "markets"), exist_ok=True)
    with open(os.path.join(root, "markets", "index.html"), "w", encoding="utf-8") as f:
        f.write(hub)
    with open(os.path.join(root, "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n')
        for u in ["/", "/markets/"] + urls:
            f.write(f"<url><loc>{SITE_URL}{u}</loc><lastmod>{built}</lastmod></url>\n")
        f.write("</urlset>\n")
    with open(os.path.join(root, "robots.txt"), "w", encoding="utf-8") as f:
        f.write(f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}/sitemap.xml\n")
    # which place has which page, for the map's "Full report" button
    write_json(os.path.join(OUT, "pages.json"), {lvl: {pid: p["_url"] for pid, p in places[lvl].items() if "_url" in p} for lvl in places})
    for lvl in places:
        for p in places[lvl].values():
            p.pop("_url", None)
    print(f"  pages: {len(urls)} (states, metros, counties) + markets list + sitemap")


def data_check(meta, cpi, sscores):
    """Looks for data that is late (a download that keeps failing, or a release that changed its address) and for new
    releases this site can only add by hand. New findings go to notices.txt; the workflow turns that into a GitHub
    issue, which GitHub emails to the repository owner. Each finding is reported once, until it is resolved."""
    today = date.today()
    mo, yr = today.month, today.year
    found = {}

    def months_old(ym):
        return (yr - int(ym[:4])) * 12 + mo - int(ym[5:7]) if ym else 99

    m, y = meta.get("months", {}), meta.get("years", {})
    for name, ym, limit in (("Zillow", m.get("zillow"), 2), ("Realtor.com", m.get("realtor"), 2),
                            ("CPI inflation (BLS)", max(cpi) if cpi else None, 3)):
        if months_old(ym) > limit:
            found[f"late:{name}"] = f"**{name}** is late: the newest month on the site is {ym or 'missing'}. The download may be failing; check the latest run's log."
    rate_day = (meta.get("mortgage") or {}).get("date")
    if rate_day and (today - date.fromisoformat(rate_day)).days > 21:
        found["late:rate"] = f"**Mortgage rate (Freddie Mac)** is late: the newest rate is from {rate_day}."
    # yearly sources: (label, newest year on the site, year expected by now)
    expect = [
        ("Household income (Census SAIPE)", y.get("income"), yr - 2 if mo >= 2 else yr - 3),
        ("American Community Survey (Census)", y.get("acs"), yr - 2 if mo >= 2 else yr - 3),
        ("Population (Census)", y.get("pop"), yr - 1 if mo >= 5 else yr - 2),
        ("Building permits (Census)", y.get("permits"), yr - 1 if mo >= 7 else yr - 2),
        ("Jobs and pay (BLS QCEW)", y.get("jobs"), yr - 1 if mo >= 11 else yr - 2),
        ("Moves in and out (IRS)", y.get("migration"), yr - 3 if mo >= 9 else yr - 4),
        ("Air quality (EPA)", y.get("air"), yr - 1 if mo >= 8 else yr - 2),
    ]
    for label, have, want in expect:
        if have is None or int(have) < want:
            found[f"late:{label}:{want}"] = f"**{label}** is late: the site has {have or 'nothing'}, and {want} figures are normally out by now. The release may be delayed, or its download address changed."
    # sources added by hand
    votes = y.get("votes")
    if votes and (today >= date(int(votes) + 4, 12, 1)):
        found[f"new:votes:{int(votes) + 4}"] = f"**New presidential election results ({int(votes) + 4})** should be available. Ask Claude to add them (the site only has up to {votes})."
    last_school = max(SCHOOL_FILES)
    for sy in range(last_school + 1, yr + 1):
        try:
            urllib.request.urlopen(urllib.request.Request(f"{EDFACTS}EDFacts_{sy}_School_math.csv", headers=PLAIN, method="HEAD"), timeout=30)
            found[f"new:schools:{sy}"] = f"**Newer school test scores ({sy - 1}–{str(sy)[2:]})** are available. Ask Claude to add them (the site uses {last_school - 1}–{str(last_school)[2:]})."
            break
        except Exception:
            pass
    # report each finding once; forget the ones that were resolved, so they are reported again if they come back
    seen = set(load_dcache("notices.json") or [])
    new = [msg for key, msg in found.items() if key not in seen]
    save_dcache("notices.json", sorted(found))
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notices.txt")
    if new:
        with open(path, "w", encoding="utf-8") as f:
            f.write("The twice-monthly update found something that needs your attention:\n\n")
            f.write("\n".join(f"- {msg}" for msg in new))
            f.write("\n\nEverything else on the site updates by itself. This was posted automatically by the update workflow.\n")
        print(f"  data check: {len(new)} new notice(s)")
    elif os.path.exists(path):
        os.remove(path)


# ---------- ZIP codes ----------
# ~30,000 ZIPs: values are kept in compact arrays (one slot per month) instead of dictionaries, the 840 MB
# Realtor.com file is read line by line, and the map shapes are simplified and split into one file per state.
from array import array

MONTH0 = 2000 * 12  # month index 0 = Jan 2000


def mi(ym):
    return int(ym[:4]) * 12 + int(ym[5:7]) - 1 - MONTH0


def im(i):
    i += MONTH0
    return f"{i // 12:04d}-{i % 12 + 1:02d}"


def fetch_file(url, name):
    """Stream a large download to .cache/ and return its path."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name)
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 20 * 3600:
        return path
    import shutil
    for attempt in range(3):
        try:
            print(f"  downloading {url}", flush=True)
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=300) as r, open(path + ".part", "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            os.replace(path + ".part", path)
            return path
        except Exception as e:
            last = e
            time.sleep(10 * (attempt + 1))
    raise last


def compact(d, start):
    """{YYYY-MM: v} -> [first month, [v, v, null, ...]]: a third of the size of month-labelled values."""
    keys = sorted(k for k in d if k >= start)
    if not keys:
        return None
    lo, hi = mi(keys[0]), mi(keys[-1])
    vals = [None] * (hi - lo + 1)
    for k in keys:
        v = d[k]
        v = round(v) if abs(v) >= 1000 else round(v, 3)
        vals[mi(k) - lo] = int(v) if v == int(v) else v  # 35 rather than 35.0
    return [keys[0], vals]


def to_dict(d, start="0000"):
    if not d:
        return {}
    lo, a = d
    return {im(lo + i): float(v) for i, v in enumerate(a) if v == v and im(lo + i) >= start}


def zillow_zip(path):
    data = try_fetch(f"{ZILLOW}/{path}", f"zillow_{path.replace('/', '_')}")
    if not data:
        return {}, {}
    r = rows(data)
    head = next(r)
    col = {h: i for i, h in enumerate(head)}
    months = [(i, mi(h[:7])) for i, h in enumerate(head) if len(h) == 10 and h[4] == "-"]
    out, info = {}, {}
    for row in r:
        if not row:
            continue
        z = row[col["RegionName"]].zfill(5)
        vals = [(m, num(row[i])) for i, m in months]
        vals = [(m, v) for m, v in vals if v is not None]
        if not vals:
            continue
        lo, hi = vals[0][0], vals[-1][0]
        a = array("f", [float("nan")] * (hi - lo + 1))
        for m, v in vals:
            a[m - lo] = v
        out[z] = (lo, a)
        info[z] = (row[col.get("City", 0)] if "City" in col else "", row[col["State"]] if "State" in col else "")
    del data
    return out, info


def realtor_zip():
    path = fetch_file(f"{REALTOR}/RDC_Inventory_Core_Metrics_Zip_History.csv", "realtor_zip.csv")
    tmp = {}  # zip -> metric -> {month index: v}
    names = {}
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        r = csv.reader(f)
        head = next(r)
        col = {h: i for i, h in enumerate(head)}
        cols = {k: col[v] for k, v in REALTOR_FIELDS.items()}
        zc, nc = col["postal_code"], col.get("zip_name")
        for row in r:
            if len(row) < len(head) - 1 or not row[0].isdigit():
                continue
            m = mi(f"{row[0][:4]}-{row[0][4:6]}")
            z = row[zc].zfill(5)
            rec = tmp.get(z)
            if rec is None:
                rec = tmp[z] = {k: {} for k in REALTOR_FIELDS}
                if nc is not None:
                    names[z] = row[nc]
            for k, i in cols.items():
                v = num(row[i])
                if v is not None:
                    rec[k][m] = v
    out = {}
    for z, rec in tmp.items():
        packed = {}
        for k, d in rec.items():
            if d:
                lo, hi = min(d), max(d)
                a = array("f", [float("nan")] * (hi - lo + 1))
                for i, v in d.items():
                    a[i - lo] = v
                packed[k] = (lo, a)
        out[z] = packed
    return out, names


def zcta_counties():
    """ZIP (ZCTA) -> the county that holds most of its land (Census 2020 relationship file)."""
    data = fetch("https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt", "zcta_county.txt")
    lines = data.decode("utf-8-sig", "replace").splitlines()
    head = lines[0].split("|")
    zi, ci, ai = head.index("GEOID_ZCTA5_20"), head.index("GEOID_COUNTY_20"), head.index("AREALAND_PART")
    best = {}
    for line in lines[1:]:
        t = line.split("|")
        if len(t) <= ai or not t[zi]:
            continue
        a = num(t[ai]) or 0
        if t[zi] not in best or a > best[t[zi]][1]:
            best[t[zi]] = (t[ci], a)
    return {z: c for z, (c, a) in best.items()}


# ----- ZIP shapes: read the Census shapefile with plain Python, simplify, and pack per state -----
def read_shapefile(zip_path):
    import struct
    with zipfile.ZipFile(zip_path) as zf:
        shp_name = next(n for n in zf.namelist() if n.endswith(".shp"))
        dbf = zf.read(shp_name[:-4] + ".dbf")
        shp = zf.read(shp_name)
    # attribute table: find the ZIP column
    nrec, hlen, rlen = struct.unpack("<IHH", dbf[4:12])
    fields, pos, off = [], 32, 1
    while dbf[pos] != 0x0D:
        name = dbf[pos:pos + 11].split(b"\x00")[0].decode()
        flen = dbf[pos + 16]
        fields.append((name, off, flen))
        off += flen
        pos += 32
    zf_ = next(f for f in fields if f[0].startswith("ZCTA5CE") or f[0].startswith("GEOID"))
    zips = [dbf[hlen + i * rlen + zf_[1]: hlen + i * rlen + zf_[1] + zf_[2]].decode().strip() for i in range(nrec)]
    # geometry
    out, p, n = {}, 100, 0
    while p < len(shp) and n < nrec:
        clen = struct.unpack(">i", shp[p + 4:p + 8])[0] * 2
        c = p + 8
        stype = struct.unpack("<i", shp[c:c + 4])[0]
        if stype == 5:
            nparts, npts = struct.unpack("<ii", shp[c + 36:c + 44])
            parts = struct.unpack(f"<{nparts}i", shp[c + 44:c + 44 + 4 * nparts])
            pts = struct.unpack(f"<{2 * npts}d", shp[c + 44 + 4 * nparts:c + 44 + 4 * nparts + 16 * npts])
            bounds = list(parts) + [npts]
            rings = [[(pts[2 * j], pts[2 * j + 1]) for j in range(bounds[k], bounds[k + 1])] for k in range(nparts)]
            out[zips[n]] = rings
        p = c + clen
        n += 1
    return out


def simplify(ring, tol):
    """Douglas-Peucker line simplification (iterative)."""
    if len(ring) < 5:
        return ring
    keep = [False] * len(ring)
    keep[0] = keep[-1] = True
    stack = [(0, len(ring) - 1)]
    t2 = tol * tol
    while stack:
        a, b = stack.pop()
        ax, ay = ring[a]
        bx, by = ring[b]
        dx, dy = bx - ax, by - ay
        dd = dx * dx + dy * dy
        best, bi = -1, -1
        for i in range(a + 1, b):
            px, py = ring[i]
            if dd:
                t = ((px - ax) * dx + (py - ay) * dy) / dd
                t = 0 if t < 0 else 1 if t > 1 else t
                qx, qy = ax + t * dx - px, ay + t * dy - py
            else:
                qx, qy = ax - px, ay - py
            d = qx * qx + qy * qy
            if d > best:
                best, bi = d, i
        if best > t2:
            keep[bi] = True
            stack.append((a, bi))
            stack.append((bi, b))
    return [pt for pt, k in zip(ring, keep) if k]


def ring_area(r):
    return sum((r[i + 1][0] - r[i][0]) * (r[i + 1][1] + r[i][1]) for i in range(len(r) - 1)) / 2  # >0: clockwise


def pack_shape(rings, tol=0.0004, q=10000):
    """Rings -> polygons (shapefile outer rings are clockwise, holes counter-clockwise), simplified and
    stored as delta-encoded integers (1/10,000 of a degree, about 10 m)."""
    polys = []
    for r in rings:
        a = ring_area(r)
        if abs(a) < tol * tol * 2 and len(rings) > 1:
            continue  # specks
        s = simplify(r, tol)
        if len(s) < 4:
            continue
        flat, px, py = [], 0, 0
        for x, y in s:
            ix, iy = round(x * q), round(y * q)
            flat += [ix - px, iy - py]
            px, py = ix, iy
        if a > 0 or not polys:
            polys.append([flat])
        else:
            polys[-1].append(flat)
    return polys


def scale_breaks(vals, diverging):
    """Same colour breaks the map uses (see buildScale in index.html), computed over every ZIP nationwide."""
    vals = sorted(v for v in vals if v is not None and math.isfinite(v))
    if not vals:
        return None

    def q(s, p):
        i = (len(s) - 1) * p
        lo, hi = math.floor(i), math.ceil(i)
        return s[lo] + (s[hi] - s[lo]) * (i - lo)

    def nice(v):
        if not v:
            return 0
        p = 10 ** (math.floor(math.log10(abs(v))) - 1)
        return round(v / p) * p
    if diverging:
        a = sorted(abs(v) for v in vals)
        t = [nice(q(a, p)) for p in (0.2, 0.5, 0.8)]
        if not t[0]:
            t[0] = t[1] / 2 or a[-1] / 10 or 0.1
        for i in (1, 2):
            if t[i] <= t[i - 1]:
                t[i] = t[i - 1] * 1.5 or 1
        b = [-t[2], -t[1], -t[0], t[0], t[1], t[2]]
    else:
        b = [nice(q(vals, i / 7)) for i in range(1, 7)]
        for i in range(1, 6):
            if b[i] <= b[i - 1]:
                b[i] = b[i - 1] + abs(b[i - 1] or 1) * 0.01
    return [round(x, 6) for x in b]


# Data points coloured blue-to-red around zero (same list as the "d: 1" entries in index.html).
DIVERGING = {"hpi_change", "mig_net", "mig_net_pct", "hv_yoy", "hv_mom", "hv_5y", "fc_1y", "overvalued", "from_2022_peak", "crash_2007", "buy_vs_rent", "inv_yoy",
             "inv_vs_2019", "newl_yoy", "dom_yoy", "lp_yoy", "rent_yoy", "sfr_yoy", "condo_yoy", "inv_mom", "sales_yoy",
             "sales_vs_2019", "rent_overvalued"}


# More Zillow market data: actual sale prices, how close homes sell to asking, how fast, market heat, and the values of
# starter (bottom third) and high-end (top third) homes.
ZILLOW_EXTRA = {
    "sp": "median_sale_price/{}_median_sale_price_uc_sfrcondo_sm_month.csv",
    "stl": "mean_sale_to_list/{}_mean_sale_to_list_uc_sfrcondo_sm_month.csv",
    "pal": "pct_sold_above_list/{}_pct_sold_above_list_uc_sfrcondo_sm_month.csv",
    "dtp": "med_doz_pending/{}_med_doz_pending_uc_sfrcondo_sm_month.csv",
    "heat": "market_temp_index/{}_market_temp_index_uc_sfrcondo_month.csv",
    "hvlo": "zhvi/{}_zhvi_uc_sfrcondo_tier_0.0_0.33_sm_sa_month.csv",
    "hvhi": "zhvi/{}_zhvi_uc_sfrcondo_tier_0.67_1.0_sm_sa_month.csv",
}


def extra_metrics(xs):
    m = {}
    k, v = latest(xs.get("sp") or {})
    if v:
        m["sale_price"] = rnd(v, 0)
        m["sale_price_yoy"] = rnd(change(xs["sp"], 12), 1)
    for key, out, mult, digits in (("stl", "sale_to_list", 100, 1), ("pal", "pct_above_list", 100, 1), ("dtp", "days_to_pending", 1, 0),
                                   ("heat", "market_heat", 1, 0), ("hvlo", "hv_low", 1, 0), ("hvhi", "hv_high", 1, 0)):
        k, v = latest(xs.get(key) or {})
        if v is not None:
            m[out] = rnd(v * mult, digits)
    return {k: v for k, v in m.items() if v is not None}


# Ready-made ZIP code rankings (counties, metros and states are ranked in the browser, which has all of them loaded):
# list -> (data point, 1 = lowest first / -1 = highest first, which ZIPs qualify).
ZIP_RANKINGS = {
    "price_drop": ("hv_yoy", 1, lambda v: True),
    "price_gain": ("hv_yoy", -1, lambda v: True),
    "schools_under_500k": ("school_score", -1, lambda v: 0 < (v.get("hv") or 0) < 500000),
    "cheapest": ("hv", 1, lambda v: True),
    "fast_selling": ("dom", 1, lambda v: (v.get("inv") or 0) >= 10),
    "price_cuts": ("cut", -1, lambda v: (v.get("inv") or 0) >= 10),
    "rent_yield": ("gross_yield", -1, lambda v: True),
    "walkable": ("walk", -1, lambda v: True),
    "low_risk": ("risk", 1, lambda v: True),
}


def build_zips(county_to_cbsa, county_income, rate, fc_hist, zillow_month, acs_zip, area, county_shared, zip_school_scores, zip_schools,
               zip_haz, haz_year, zip_walk, zip_claims, zip_noise, rent_ratio, county_inc_hist, zip_hpi, safmr, safmr_year):
    print("ZIP codes: Zillow, Realtor.com, Census shapes")
    # Zillow's own ZIP forecasts, saved each run to build up a history (the chart uses the metro's until there are two).
    zfc_raw, _ = zillow_zip("zhvf_growth/Zip_zhvf_growth_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv")
    zfc_hist = load_dcache("forecast_zip.json") or {}
    for z, d in zfc_raw.items():
        k, v = latest(to_dict(d))
        if v is not None:
            zfc_hist.setdefault(z, {})[zillow_month] = round(v, 2)
    save_dcache("forecast_zip.json", zfc_hist)
    del zfc_raw
    zhv, zinfo = zillow_zip("zhvi/Zip_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv")
    zxz = {key: zillow_zip(path.format("Zip"))[0] for key, path in ZILLOW_EXTRA.items()}
    zsfr, _ = zillow_zip("zhvi/Zip_zhvi_uc_sfr_tier_0.33_0.67_sm_sa_month.csv")
    zcondo, _ = zillow_zip("zhvi/Zip_zhvi_uc_condo_tier_0.33_0.67_sm_sa_month.csv")
    zrent, _ = zillow_zip("zori/Zip_zori_uc_sfrcondomfr_sm_month.csv")
    rtz, rnames = realtor_zip()
    zc = zcta_counties()
    shapes = read_shapefile(fetch_file("https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_us_zcta520_500k.zip", "zcta_shapes.zip"))
    print(f"  {len(zhv)} Zillow ZIPs, {len(rtz)} Realtor.com ZIPs, {len(shapes)} shapes, ACS years {sorted(acs_zip)}")
    acs_years_ = sorted(acs_zip)

    by_state, index, hist_groups, all_vals, ranked = {}, {}, {}, {}, []
    for z in sorted(set(zhv) | set(rtz)):
        county = zc.get(z)
        if not county or county[:2] not in STATES or z not in shapes:
            continue
        st = STATES[county[:2]][0]
        hv_d, rent_d, sfr_d, condo_d = to_dict(zhv.get(z)), to_dict(zrent.get(z)), to_dict(zsfr.get(z)), to_dict(zcondo.get(z))
        rt_d = {k: to_dict(rtz[z].get(k)) for k in REALTOR_FIELDS} if z in rtz else None
        # Yearly survey data for the ZIP itself (needs the Census key); income falls back to the county's.
        a = {}
        for y in acs_years_:
            for k, v in acs_zip[y].get(z, {}).items():
                key = {"acs_income": "income", "acs_pop": "pop", "acs_units": "units"}.get(k, k)
                a.setdefault(key, {})[str(y)] = v
        pops = a.get("pop", {})
        if pops and area.get("z" + z):
            a["density"] = {y: tidy(p / area["z" + z]) for y, p in pops.items()}
        for y, p in pops.items():
            for back, key in ((1, "pop_growth"), (5, "pop_growth_5y")):
                old = pops.get(str(int(y) - back))
                if old:
                    a.setdefault(key, {})[y] = tidy((p / old - 1) * 100)
        inc_s = a.get("income", {})
        for y, v in inc_s.items():
            if inc_s.get(str(int(y) - 1)):
                a.setdefault("income_growth", {})[y] = tidy((v / inc_s[str(int(y) - 1)] - 1) * 100)
        for k, s in county_shared.get(county, {}).items():
            a[k] = dict(s) if k in FMR_KEYS else s
        if safmr.get(z) and safmr_year:  # HUD sets this ZIP's own rents (Small Area FMRs): use them for that year
            for i, k in enumerate(FMR_KEYS):
                a.setdefault(k, {})[safmr_year] = safmr[z][i]
        if a.get("income") and a.get("rpp_all"):
            a["income_real"] = {y: round(v / a["rpp_all"][y] * 100) for y, v in a["income"].items() if a["rpp_all"].get(y)}
        if zip_hpi.get(z):
            a["hpi_change"] = hpi_change(zip_hpi[z])
            hl = hpi_long(zip_hpi[z], hv_d)
            if hl:
                a["hv_long"] = hl
        if zip_school_scores.get(z):
            a["school_score"] = zip_school_scores[z]
        for k, v in (zip_haz.get(z) or {}).items():
            a[k] = {haz_year: v}
        if z in zip_walk:
            a["walk"] = {"2021": zip_walk[z]}
        for k, v in (zip_noise.get(z) or {}).items():
            if v is not None:
                a[k] = {"2020": v}
        if zip_claims.get(z):
            zu = latest_annual(a).get("units")
            a.update(flood_metrics(zip_claims[z], zu, THIS_YEAR - 1))
        zx_d = {key: to_dict(zxz[key].get(z)) for key in ZILLOW_EXTRA}
        own_fc, metro_fc = zfc_hist.get(z, {}), fc_hist.get(county_to_cbsa.get(county), {})
        fc_now = own_fc.get(zillow_month, metro_fc.get(zillow_month))
        la = latest_annual(a)
        income = la.get("income") or county_income.get(county)
        if a.get("prop_tax_rate") and hv_d:
            for y, r in a["prop_tax_rate"].items():
                hv = hv_d.get(f"{y}-12") or latest({k: v for k, v in hv_d.items() if k <= f"{y}-12"})[1]
                if hv:
                    a.setdefault("prop_tax", {})[y] = tidy(hv * r / 100)
        rs_d = house_rent(rent_d, rent_ratio.get(county_to_cbsa.get(county)))
        # Income history for fair value: the ZIP's own survey years, and before them its county's income scaled by how the
        # ZIP compared with the county in the years both exist (saved as "inc_est" so the charts use the same figures).
        zinc, cinc = a.get("income") or {}, county_inc_hist.get(county) or {}
        both = [y for y in zinc if cinc.get(y)]
        scale = sum(zinc[y] / cinc[y] for y in both) / len(both) if both else 1
        est = {y: round(v * scale) for y, v in cinc.items() if y not in zinc and v and (not zinc or int(y) < int(min(zinc)))}
        if est:
            a["inc_est"] = est
        v = metrics_for(hv_d, rent_d, rt_d, income, rate, fc_now, sfr_d, condo_d, rs_d, la.get("units"), la.get("prop_tax_rate"),
                        {**est, **zinc})
        if not v:
            continue
        v.update({k: x for k, x in la.items() if k not in ("prop_tax",)})
        v.update(extra_metrics(zx_d))
        city = (zinfo.get(z) or ("", ""))[0] or (rnames.get(z, "").rsplit(",", 1)[0].title())
        rec = {"c": city, "k": county}
        if county_to_cbsa.get(county):
            rec["m"] = county_to_cbsa[county]
        rec["v"] = v
        by_state.setdefault(st, {})[z] = rec
        ranked.append((z, city, st, v))
        index[z] = [city, st]
        for k, x in v.items():
            all_vals.setdefault(k, []).append(x)
        b = {}
        for key, d, start in (("hv", hv_d, "2008-01"), ("sfr", sfr_d, "2008-01"), ("condo", condo_d, "2008-01"), ("rent", rent_d, "2015-01"), ("rent_sfr", rs_d, "2015-01")):
            if d:
                b[key] = compact(d, start)
        if rt_d:
            for k in ("inv", "dom", "cut", "lp", "newl", "pend", "ppsf", "inc"):  # all of it (from 2016), so 2017-19 can be the baseline
                if rt_d.get(k):
                    b[k] = compact(rt_d[k], "2016-01")
        fc_s = own_fc if len(own_fc) >= 2 else {**metro_fc, **own_fc}  # metro's past forecasts, ZIP's own for now
        if fc_s:
            b["fc"] = compact(fc_s, "2000-01")
        for key, d in zx_d.items():
            if d:
                b[key] = compact(d, "2008-01")
        if a:
            b["a"] = a
        if zip_schools.get(z):
            b["sch"] = zip_schools[z]
        hist_groups.setdefault(z[:3], {})[z] = {k: x for k, x in b.items() if x}

    for st, recs in by_state.items():
        write_json(os.path.join(OUT, "zip", f"{st}.json"), recs)
        write_json(os.path.join(OUT, "zip", "shapes", f"{st}.json"), {z: pack_shape(shapes[z]) for z in recs})
    for prefix, group in hist_groups.items():
        write_json(os.path.join(OUT, "hist", "zip", f"{prefix}.json"), group)
    write_json(os.path.join(OUT, "zip", "index.json"), index)
    rankings = {}
    for name, (key, direction, ok) in ZIP_RANKINGS.items():
        rows = [(v[key], z, city, st) for z, city, st, v in ranked if v.get(key) is not None and ok(v)]
        rows.sort(key=lambda r: r[0] * direction)
        rankings[name] = [[z, city, st, val] for val, z, city, st in rows[:50]]
    write_json(os.path.join(OUT, "zip", "rankings.json"), rankings)
    breaks = {k: scale_breaks(vals, k in DIVERGING) for k, vals in all_vals.items()}
    return len(index), breaks


if __name__ == "__main__":
    sys.exit(main())
