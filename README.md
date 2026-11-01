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
   Point the `snowdash` volume mounts in `docker-compose.yml` at your capture output
   (`SNOWCAM_TIMELAPSE_DIR`, `SNOWCAM_TIMELAPSE7_DIR`, `SNOWCAM_TIMELAPSE30_DIR`, `SNOWCAM_OUT_DIR`
   in `.env`); the cam list lives in `cams.json`.

4. **Start the services:**
   ```bash
   docker compose up -d
   ```

5. **Access the dashboard:**
   - Storm showcase: http://localhost:9003/
   - SNOTEL map: http://localhost:9003/map/
   - Tables: http://localhost:9003/tables/
   - Snow cams / forecast: http://localhost:9003/snowcams/, http://localhost:9003/forecast/

6. **Load initial data:**
   ```bash
   # Stations, 14 days of SNOTEL obs, and GFS/ICON/ECMWF forecasts for every station
   docker compose run --rm runner python /app/main.py update-all --days-back 14
   docker compose run --rm runner python /app/main.py init-views

   # Indexes and materialized views
   docker compose exec -T db psql -U snowuser -d snow < db/migrations/001_add_composite_indexes.sql
   docker compose exec -T db psql -U snowuser -d snow < db/migrations/002_create_materialized_views.sql
   docker compose exec -T db psql -U snowuser -d snow < db/migrations/003_create_observed_vs_forecast_mv.sql
   docker compose exec -T db psql -U snowuser -d snow < db/migrations/004_create_obs_vs_fc_48_72.sql
   ```

## Development Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Operations (production on 192.168.0.110, updated 2026-10-31)

snowhackers.com is served by `snowapi-nginx` (:9003, Host header `snowhackers.com` required
for local curl) in front of `snowapi-snowdash` (Django). Sibling repos: `~/snowcamtimelapse`
(captures + timelapses) and `~/snowcammeasurement` (labels, fine-tuning, OpenCV).

### Site pages

Code: `snowdash/dashboard/views/` — `weather.py` (SNOTEL tables, map, station APIs), `snowcams.py` (timelapses, image search, accuracy), `showcase.py` (home, resort pages, curation), `forecast.py`, `common.py`. Host scripts share `bin/snowlib.py` (psql_csv, km), `bin/camreg.py` (cam registry) and `bin/history_db.py` (reading history).

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
| 02:07 | `bin/update_snow.sh` — SNOTEL obs + GFS/ICON/ECMWF forecasts, then `bin/forecast_retention.sql` (runs >48 h old keep only lead −24..+72 h) |
| :13 hourly | `bin/refresh_cam_predictions.sh` — runs the fine-tuned Qwen2.5-VL model on the Blackwell (192.168.0.117) over the latest frame per model cam in `cams.json` (via the generated `cam_config.json`); routes each cam to its adapter (generated `cam_routing.json`) (run 7 default, run 9 Sherpa, run 10 newer cams); skips the hour while `training.lock` exists on the Blackwell |
| 06/08/12/16:40 | `bin/update_resort_reports.sh` — resort-published snow reports → table `resort_snow_reports` |
| 04:15 | `bin/build_forecast.py` — zone forecasts → `snowdash/dashboard/data/forecast.json` (+ `forecast_archive/`) |
| Mon 05:30 | `bin/score_forecasts.py` — model skill vs SNOTEL per zone → `data/forecast_skill.json` |
| 08:30 | `bin/build_highlights.py` — storm detection, highlight clips, chart data |
| :05 hourly | `~/snowcammeasurement/scripts/cron_measure.sh` — OpenCV measurements in the `snow-frontend` container (secondary to the model) |
| 04:20 | `~/snowcamtimelapse/cap_cron_log.sh` — trims the capture container's `out/cron.log` |
| 10:25 | `~/scripts/daily_all_repos_push.sh` — pushes committed work in every repo (this one: branch `prep-public`) |
| Sun 05:00 | `bin/rotate_logs.sh` — logrotate (`logrotate.conf`: cron logs weekly, keep 4, gzip) + delete per-run update/backfill logs >60 days |

Capture runs every 5 min inside the `snowcams` container (see `~/snowcamtimelapse/CLAUDE.md`).

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

Southern Colorado / New Mexico parsers live in `bin/south_sources.py` (added 2026-10-19). Only Wolf Creek
and Ski Santa Fe publish a storm total. MCP sites (Purgatory, Sipapu, Pajarito) remove their snow table off-season
and raise until opening; Ski Cooper posts only an image (disabled); Taos numbers appear to be OpenSnow estimates.

### Cam registry (`cams.json`, added 2026-10-21)

Every cam is defined once in `cams.json`: `prefix` (capture filename prefix), `name` (display
name; resort-report rows join on it), `region`, `lat`/`lon` (nearest SNOTEL), `zone` (forecast
zone id; zones are listed at the top), and for cams the stake-reading model runs on, `model`
(`stake_type`, prompt `label`/`note`, optional `adapter` overriding `default_adapter`).
Cams are grouped by region; that order is the order on /snowcams/.

`cam_config.json` and `cam_routing.json` are **generated** — don't edit them. After changing
`cams.json` run `python3 bin/camreg.py export` (the hourly refresh also runs it); `camreg.py
check` reports if they're stale. The site reads `cams.json` through a bind mount at
`/snowcam-config/cams.json` (picked up on change, no restart); `build_highlights.py`,
`build_forecast.py` and `score_forecasts.py` import `bin/camreg.py`.

To add a cam: add the capture line in `~/snowcamtimelapse/run_all_once.sh`, an entry in
`cams.json`, then `camreg.py export`.

### Reading history (`cam_prediction` table, moved from JSON 2026-10-21)

Every per-image reading — hourly model predictions, backfills, and hand/AI labels from
`~/snowcammeasurement/labels/labels.json` — is one row in Postgres table `cam_prediction`
(unique on prefix + image name; `rec` holds the full record as JSON). A label row is never
replaced by a model row. Use `bin/history_db.py` (`export FILE`, `upsert FILE`, `replace FILE`,
`stats`); the hourly refresh upserts, `bin/refresh_labels_in_history.py` re-syncs label rows
after a labeling sprint, and the search/accuracy pages, resort pages and `build_highlights.py`
read the table. The last JSON copy is `~/snowwork/cam_predictions_history.final-20261021.json`.

### Accuracy page (`bin/build_accuracy_demo.py`)

/snowcams/accuracy/ compares labeled truth (`~/snowcammeasurement/labels/accuracy_demo_truth_20260804.json`
plus the `*_holdout_*.json` sets) with readings from each cam's production adapter. Re-run with
`--predict` after training a new adapter or changing routing in `cams.json` (~3 min GPU);
without it, it re-bakes from `finetune/accuracy_demo_predictions.json`. 2026-10-21: 32 resorts,
126 samples, MAE 1.2 in.

### Gotchas

- `cam_predictions.json` (latest reading per cam) is bind-mounted into snowdash as a single
  file: anything that replaces it by rename leaves the site frozen until restart. Write it in
  place (`rsync --inplace`) — the refresh script does. `cams.json` is mounted the same way.
- `SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"` is required: OpenStreetMap
  tiles are blocked without a Referer (map broke 2026-10).
- Model accuracy by cam: the 2026-10-10 SNOTEL audit found Sherpa-bar cams (Snowmass, Aspen,
  Highlands, Buttermilk, Sunlight, Powderhorn) and Sundance, Powder Mountain, Loveland, Alta
  badly under-reading storms. Runs 9 (Sherpa bars) and 10 (batch-34 cams) were trained for them
  and are routed per cam; current numbers are on /snowcams/accuracy/ (MAE 1.2" over 32 resorts,
  frozen test 1.4"). See `~/snowcammeasurement/finetune/TRAINING_LOGS.MD`.
- Not in git and not backed up: the Postgres volume (forecast history, `cam_prediction`, resort
  reports — SNOTEL can be re-fetched, the rest can't), captured frames in
  `~/snowcamtimelapse/out/`, and `.env` (Alterra token, DB password).

### Zone forecasts (`bin/build_forecast.py`, added 2026-10-12)

- 16 zones (defined in `cams.json`), each a group of stake-cam resorts; each resort uses its 2
  nearest SNOTEL stations (≤40 km). For every station the latest run per model is used — run
  timestamps differ per station, so never take a global "latest run".
- Numbers are deterministic: per-model 24h / 72h / 7-day sums, ensemble mean, model range, and a
  confidence from model agreement. Snowfall comes from `forecast_hourly`; temperatures and snow
  level are fetched from Open-Meteo at build time (see Weather below).
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
  13 of the 14 zones scored then, so they are NOT applied — skill is shown as context only. ICON verified best in 13/14 zones.
- `/forecast/` has a Leaflet map of resorts colored by 7-day snow (validated ordinal blue ramp).
- Text post-processing: amounts rounded half-up to whole inches (Python round() is banker's), sub-inch → "a dusting".
