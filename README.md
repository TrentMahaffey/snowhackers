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
## Operations (production on 192.168.0.110, updated 2026-10-10)

snowhackers.com is served by `snowapi-nginx` (:9003, Host header `snowhackers.com` required
for local curl) in front of `snowapi-snowdash` (Django). Sibling repos: `~/snowcamtimelapse`
(captures + timelapses) and `~/snowcammeasurement` (labels, fine-tuning, OpenCV).

### Site pages

| URL | What |
|---|---|
| `/` | Storm showcase: hero clip, biggest-storms gallery, live stakes (`views.home`) |
| `/resort/<prefix>/` | Latest frame (daylight fallback at night), timelapses, storms, season chart |
| `/curate/` | Pin/hide storm clips; key in `snowdash/dashboard/data/curate_key.txt` (gitignored) |
| `/forecast/` | 7-day zone snow forecasts: model numbers + AI-written discussion (`views.forecast_page`) |
| `/map/` | SNOTEL map (moved from `/`) |
| `/snowcams/`, `/snowcams/search/`, `/snowcams/accuracy/` | Timelapse grid, image search, model accuracy demo |

### Scheduled jobs (host crontab, user trent)

| When | Job |
|---|---|
| 02:07 | `bin/update_snow.sh` — SNOTEL obs + GFS/ICON/ECMWF forecasts |
| :13 hourly | `bin/refresh_cam_predictions.sh` — runs the fine-tuned Qwen2.5-VL model on the Blackwell (192.168.0.117) over the latest frame per cam in `cam_config.json`; routes each cam to the adapter in `cam_routing.json` (run 7 default, run 9 Sherpa, run 10 newer cams); skips the hour while `training.lock` exists on the Blackwell |
| 06/08/12/16:40 | `bin/update_resort_reports.sh` — resort-published snow reports → table `resort_snow_reports` |
| 04:15 | `bin/build_forecast.py` — zone forecasts → `snowdash/dashboard/data/forecast.json` (+ `forecast_archive/`) |
| Mon 05:30 | `bin/score_forecasts.py` — model skill vs SNOTEL per zone → `data/forecast_skill.json` |
| 08:30 | `bin/build_highlights.py` — storm detection, highlight clips, chart data |

### Storm highlights (`bin/build_highlights.py`)

- Daily new snow = the largest rise of the (rolling-median-smoothed) model reading above the
  lowest reading before it, looking back to 6 pm the previous day. Melt days score 0.
- Gates: a snowy day needs ≥3"; a storm needs ≥6" total and a ≥6" day; and the two nearest
  SNOTEL stations (≤40 km) must gain ≥2" depth or ≥0.2" SWE.
- Outputs: `static/highlights/<id>.mp4|.jpg` (gitignored), `snowdash/dashboard/data/highlights.json`
  (highlights, candidates, model daily series, SNOTEL daily series), and
  `~/snowcamtimelapse/protected_days.txt`.
- SNOTEL can't catch storms that really happened but didn't show on the stake (rime ice,
  Steamboat 2025-12-07); hide those on `/curate/`. Overrides: `data/highlight_overrides.json`.

### Resort snow reports (`bin/fetch_resort_reports.py`, `bin/html_sources.py`)

Sources: Alterra mtnpowder feed, Vail `FR.snowReportData`, POWDR DOR API, Alta `window.Alta`,
Aspen WeatherDashboard, Sundance + Powder Mountain APIs, and HTML parsers for six small
resorts. Survey of endpoints: `resort_snow_report_sources.json`. View
`resort_snow_reports_fresh` keeps only reports the resort updated that day. Vail sites
reject the runner container's TLS fingerprint, so the fetch runs on the host and the runner
only loads the JSON.

### Gotchas

- `cam_predictions.json` and `cam_predictions_history.json` are bind-mounted into snowdash as
  single files: anything that replaces them by rename leaves the site frozen until restart.
  Write them in place (`rsync --inplace`) — the refresh and both backfill scripts do.
- A cam must be in both name maps in `snowdash/dashboard/views.py` (video `resorts` dict and
  `SNOWCAM_PREFIX_TO_RESORT`) and in `cam_config.json` to get predictions.
- `SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"` is required: OpenStreetMap
  tiles are blocked without a Referer (map broke 2026-10).
- Model accuracy by cam (2026-10-10 audit vs SNOTEL): Sherpa-bar cams (Snowmass, Aspen,
  Highlands, Buttermilk, Sunlight, Powderhorn) and Sundance, Powder Mountain, Loveland, Alta
  badly under-read storms. See `~/snowcammeasurement/finetune/TRAINING_LOGS.MD`.

### Zone forecasts (`bin/build_forecast.py`, added 2026-10-12)

- 14 zones (defined in `ZONES` in the script), each a group of stake-cam resorts; each resort uses its 2
  nearest SNOTEL stations (≤40 km). For every station the latest run per model is used — run
  timestamps differ per station, so never take a global "latest run".
- Numbers are deterministic: per-model 24h / 72h / 7-day sums, ensemble mean, model range, and a
  confidence from model agreement. Only snowfall is stored (no temperature / snow level yet).
- The discussion is written by local Qwen3 32B (Ollama on the Blackwell, reached over SSH because
  Ollama listens on localhost only). The job waits for ≥22 GB free GPU and no `training.lock`,
  then unloads the model. A verifier rejects any inch figure not in the data (up to 3 attempts,
  then a template). Post-processing rounds amounts and sentence-cases headlines.
- `forecast_archive/<date>.json` keeps each day's forecast so accuracy can be scored later
  against SNOTEL / stake cams / resort reports.

- Weather: daily high/low °F and snow level (freezing level − 1,000 ft, median over precipitating hours) from
  Open-Meteo at each station's elevation; rain hours = precipitation with T > 34 °F. Open-Meteo returns freezing
  level in ft when precipitation_unit=inch — the script reads hourly_units.
- Skill (`bin/score_forecasts.py`, weekly): each model's day-ahead forecast vs SNOTEL depth increase per zone,
  backtested over the season (~17k station-days). All models run low on snow days (−0.6 to −1.2″/day;
  Cottonwoods −2.2″). Per-zone scale factors were tested (fit Nov–Feb, test Feb–May) and made forecasts WORSE in
  13/14 zones, so they are NOT applied — skill is shown as context only. ICON verified best in 13/14 zones.
- `/forecast/` has a Leaflet map of resorts colored by 7-day snow (validated ordinal blue ramp).
- Text post-processing: amounts rounded half-up to whole inches (Python round() is banker's), sub-inch → "a dusting".
\n
Southern Colorado / New Mexico report parsers live in `bin/south_sources.py` (added 2026-10-19). Only Wolf Creek
and Ski Santa Fe publish a storm total. MCP sites (Purgatory, Sipapu, Pajarito) remove their snow table off-season
and raise until opening; Ski Cooper posts only an image (disabled); Taos numbers appear to be OpenSnow estimates.
