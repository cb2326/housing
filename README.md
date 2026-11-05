# Housing Map

An interactive map of the US housing market by state, metro and county: home values and growth, a 1-year
forecast, an "overvalued" gauge, affordability, inventory, days on market, price cuts, rents, income and population.

- `site/index.html`: the map (Leaflet + US county shapes from us-atlas).
- `build_data.py`: downloads the public data and writes `site/data/` (standard-library Python only).
- `.github/workflows/update.yml`: rebuilds the data and publishes the site on GitHub Pages on the 5th and 20th of each month.

Data: Zillow Research (ZHVI, ZORI, forecasts), Realtor.com Research (inventory metrics), U.S. Census Bureau
(SAIPE income and poverty, Population Estimates), Freddie Mac PMMS via FRED. Basemap: Esri. For personal use; not financial advice.
