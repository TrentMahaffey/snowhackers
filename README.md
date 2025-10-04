# snowapi

SNOTEL + Open-Meteo snow data fetcher.

- Pulls SNOTEL daily SNWD/WTEQ/PREC (via AWDB SOAP).
- Stores both raw inches and metric cm in Postgres.
- Fetches hourly snowfall forecasts (GFS/ICON/ECMWF) via Open-Meteo.
- Ships a CLI and importable functions.

## Install (dev)
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .