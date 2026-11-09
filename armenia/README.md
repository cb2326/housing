# Armenia map (openhousingatlas.com/armenia/)

A separate part of the site with its own build, so a problem here never affects the US map.

- `build_armenia.py`: downloads the data and writes `site/armenia/data/` (`armenia.json` + `areas.geojson`).
  It needs one extra library, `pypdf`, to read the Cadastre's PDF reports (`pip install pypdf`).
- `static/areas.geojson`: outlines of the 10 marzes, Yerevan and Yerevan's 12 districts (OpenStreetMap, ODbL).
- `static/places.json`: Armenian and English names, plus positions of the 45 towns in the Cadastre reports.
- `cache/`: saved results, kept in the repository. `cache/cadastre.json` holds every report read so far, one line per
  month. `"status": "unreadable"` means that PDF's tables are pictures (most 2020–2024 reports). `"numbers"` means only
  the place names are pictures; those months are matched to places by their numbers against neighbouring months.
- `setup/`: the one-time scripts that made `static/` (OpenStreetMap export and town matching). They don't need to run again.

The GitHub workflow runs this after the US build (step "Armenia data"). Anything that needs attention, such as a new
report that couldn't be read, is added to the same emailed notice as the US data checks.

Data: State Committee of the Real Estate Cadastre (apartment prices, transactions), Statistical Committee of Armenia
(wages, poverty, population, housing), Central Bank of Armenia (mortgages, policy rate, exchange rates).
