# SnowHackers

SNOTEL + Open-Meteo snow data fetcher with interactive dashboard.

- Pulls SNOTEL daily SNWD/WTEQ/PREC (via AWDB SOAP)
- Stores both raw inches and metric cm in Postgres
- Fetches hourly snowfall forecasts (GFS/ICON/ECMWF) via Open-Meteo
- Interactive map with forecast and observed data
- Mobile-friendly dashboard with station cards
- Snow cam timelapse viewer (optional)

## Quick Start (Docker)

1. **Clone and setup environment:**
   ```bash
   git clone https://github.com/TrentMahaffey/snowhackers.git
   cd snowhackers
   cp .env.example .env
   ```

2. **Edit `.env` file:**
   - Generate a Django secret key: `python -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())'`
   - Update `DJANGO_SECRET_KEY` with the generated value
   - Adjust other settings as needed

3. **Optional - Snow Cam Integration:**
   If you have snow cam timelapse videos, update `docker-compose.yml` lines 107-108:
   ```yaml
   - /path/to/your/timelapses:/snowcam-timelapses:ro
   - /path/to/your/snapshots:/snowcam-snapshots:ro
   ```
   Otherwise, comment out or remove these volume mounts.

4. **Start the services:**
   ```bash
   docker compose up -d
   ```

5. **Access the dashboard:**
   - Map: http://localhost:9003/
   - Dashboard: http://localhost:9003/tables/
   - Snow Cams: http://localhost:9003/snowcams/ (if configured)

6. **Load initial data:**
   ```bash
   # Fetch SNOTEL stations
   docker compose run --rm runner python -m snowapi stations

   # Run initial forecast
   docker compose run --rm runner python -m snowapi forecast-once --days-ahead 14

   # Initialize materialized views
   docker compose exec -T db psql -U snowuser -d snow < db/migrations/001_add_composite_indexes.sql
   docker compose exec -T db psql -U snowuser -d snow < db/migrations/002_create_materialized_views.sql
   docker compose exec -T db psql -U snowuser -d snow < db/migrations/003_create_observed_vs_forecast_mv.sql
   ```

## Development Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .