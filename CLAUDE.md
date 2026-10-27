# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Web dashboard for hybrid solar inverters, read through inverter drivers (`inverters/`; so far: Deye / Sunsynk via a Solarman V5 logger). Displays real-time solar production, battery status, grid power, home consumption, weather, and power outage schedules. Includes a Telegram bot for notifications in literary Ukrainian style.

## Development Commands

```bash
# Setup
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Run dashboard (http://localhost:8080), served by waitress
python app.py
DASHBOARD_DEV=1 python app.py   # Flask debugger, no reloader (dev only)

# Tests (tests/ only, see pytest.ini; ~5s)
python -m pytest

# Run locally with Telegram disabled
./deploy_local.sh

# Deploy to remote server (interactive setup on first run)
./deploy.sh

# Image bundle for machines without the source (e.g. Synology): dist/extended-dashboard-<arch>.tar.gz
tools/export_images.sh [arm64|amd64]

# Update a Docker host (e.g. the Synology NAS): send build files over SSH, rebuild there
tools/deploy_nas.sh [--setup]

# Docker Hub image (cutteroid/inverter-dashboard, amd64 + arm64): .github/workflows/docker-publish.yml
# builds and pushes it on every published GitHub release (or by hand: Actions -> Run workflow)

# Test inverter connection
python test_connection.py

# Quick inverter health check
python check_inverter.py

# Diagnostic register scans
python scan_registers.py    # all registers
python scan_battery.py      # battery registers
python scan_phases.py       # phase registers
```

## Architecture

### Core Components

- **`app.py`** — Flask web server. Contains the main application, API routes, and background poller classes (`InverterPoller`, `WeatherPoller`). Serves the single-page dashboard at `/` and JSON API at `/api/*`. Manages phase stats and grid daily logs as JSON files.

- **`inverters/`** — inverter drivers, selected by `INVERTER_TYPE` (default `deye`):
  - `base.py` — brand-neutral: `InverterConfig` (capabilities: phases, battery, PV strings, generator, model family, `supported`), the `InverterDriver` interface (`from_settings`, `settings_problem`/`required_settings`, `read_all_data`, `detect_config`, `read_device_type`, `discover`), the **reading fields every driver returns** (module docstring — the rest of the app uses only those, never registers), shared grid logic (`grid_online`, `grid_state`, `grid_phases_missing`) and `BatterySmoother`: each driver's `read_all_data()` calls `smooth_battery(data)`, which shows battery voltage (average) and SOC (median) over the last 6 reads (3 min at 30 s), dropping implausible values (the logger sometimes returns 0 V) — no separate battery reads.
  - `deye.py` — `DeyeInverter` (3-phase health: running state 500 on every read; temperatures 540/541, battery totals 516-519, warning 553-554 / fault 555-558 bits every `HEALTH_READ_SECONDS` and while the state is alarm/fault; codes reported only then): Modbus over Solarman V5 (`pysolarmanv5`), both register maps, device-type families, HV scaling, grid relay, capability detection, Solarman discovery (`discover_inverter.py`). 50 ms between register reads to spare the logger.
  - `__init__.py` — `DRIVERS` registry, `driver_class()`, `create_inverter(settings)`. **New driver:** subclass `InverterDriver`, return the documented fields, register it in `DRIVERS`, add its name to `setup.INVERTER_TYPES` (stdlib-only script; a test keeps the lists in sync) and its settings to the wizard (step 1). The wizard's "Inverter type" list appears by itself once `DRIVERS` has more than one entry.
  - UI text never names a brand (tests check page and locales); brand-specific wording belongs to the driver (`display_name`).

- **`telegram_bot.py`** — Telegram bot with commands (`/battery`, `/outage`, `/grid`, `/test`). Its upcoming-outage battery verdict comes from the dashboard's `current_outage_readiness()` (`readiness=`), not its own estimate. Sends notifications for low battery and grid restore events. Messages are written in 1800s literary Ukrainian style. Uses `poems.py` for weather-themed poetry excerpts.

- **`templates/index.html`** — Single-page dashboard markup (cards, banners, settings panel, onboarding wizard). Passes server values to the JS via a small inline `window.DASHBOARD_CONFIG` (locales, default language, first-run flag).

- **`static/css/dashboard.css`** / **`static/js/dashboard.js`** — All styles and frontend logic (auto-refresh every 5 seconds, phase analytics, planned outages, fullscreen kiosk mode). Plain files, no build step; they must not contain Jinja syntax. Linked via `asset_url()`, which appends a content hash (`?v=…`) so browsers pick up changes after updates. Every colour is a token in the `:root` block at the top of `dashboard.css` (palette, `--text-*`, `--surface`, `--border`; see-through colours as `--name-rgb` used via `rgba(var(--name-rgb), alpha)`) — rules, inline styles and the JS use only `var(--…)`, never a literal (`tests/test_static_assets.py` checks it). Add a token for a new colour. Per-device view settings live in localStorage: card order (`dashboard-cards-order`), switched-off and wide cards (`dashboard-cards-hidden` / `-wide`, Settings → Cards, `applyCardLayout()`; the "Right now" strip `#flow-strip` is in the grid but not a `.card`, so ordering leaves it first). Tapping a sparkline opens its 24 hours as a larger chart (`openHistoryDetails()`), which the fullscreen return-to-top also closes.

- **`locales/*.json`** — UI translations (`en` default, `uk`, `ru`), embedded into the page by `app.py`. Never hardcode user-facing text in `index.html` / `dashboard.js`: static markup uses `data-i18n="key"` / `data-i18n-attr="title:key"`, JS uses `t('key', {param})`, and text on elements that also carry a static key must go through `setI18nText()` / `setPlainText()` so language switches re-translate it. Add every new key to all locale files (`tests/test_i18n.py` enforces matching keys and placeholders).

### Outage Providers

`outage_providers/` is a provider pattern for power outage schedules:
- `base.py` — `OutageProvider` base class, `OutageSchedulePoller` (background thread), and `create_outage_provider()` factory
- `lvivoblenergo.py` — Lvivoblenergo schedule provider
- `yasno.py` — Yasno (DTEK) schedule provider
- `dtek.py` — DTEK regions (Kyiv oblast, Kyiv, Dnipro, Odesa) via the outage-data-ua community feed

Providers implement `fetch_days()` (today, plus tomorrow once published) and raise `OutageFetchError` on failure — an empty window list means "no outages", so never return `[]` for an error. That includes network/JSON errors, a Lvivoblenergo schedule that lists other groups but not ours (a wrong group), and YASNO still sending yesterday as "today" just after midnight. The poller ignores a result without an entry for today (fetched just before midnight) and keeps the last schedule.

### Air Raid Alerts

`air_alerts.py` follows the outage-provider pattern: `AlertsProvider.fetch()` → `{"regions": {ISO code: {"status": "alert"|"threat"|"partial"|"none", "since"}}, "districts": {OCHA pcode: {...}}}`, polled every 30s by `AlertsPoller` (district alerts make a region "partial", district threats make a quiet region "threat") and served at `/api/alerts` (`status`: ok/unknown/error/disabled, `provider`, `demo` flag; `districts` lists only alerting ones). `AlertsInUaProvider` (`alerts_in_ua`, default when `ALERTS_IN_UA_TOKEN` is set) reads `GET https://api.alerts.in.ua/v2/iot/active_air_raid_alerts.json`: one string, a status char per location UID (`R` alert, `Y` threat, `P` partial, `N` none, ` ` unused — never strip it, positions are UIDs); UIDs of regions/districts are `alerts_uid` in the map file. The token goes only in the `Authorization` header — never in URLs, logs, error texts or the page; `/api/config` returns it masked (`SECRET_CONFIG_KEYS` in `app.py`; a masked value sent back keeps the saved one). The API answers only from Ukraine (403 elsewhere). `NeptunProvider` (default without a token, no key) reads `GET https://neptun.in.ua/api/v1/alerts` (`level` red → alert, yellow → threat) and matches entries by normalized Ukrainian name (`normalize_name`, `RAION_ALIASES` for pre-2024 raion names). NEPTUN's terms require the visible data link + "not an official alert system" note on the card (`[data-alerts-credit]`, the link matching `provider` via `data-alerts-source`; shown for alerts.in.ua too) — keep them. `MockAlertsProvider` (`AIR_ALERTS_PROVIDER=mock`) gives demo data. `DISTRICT_PART_OF` gives map areas drawn separately the status of the raion they belong to (Chornobyl Exclusion Zone `UA3200` → Vyshhorodskyi `UA3210`); sources only report the raion. `AIR_ALERTS_REGION` (settings wizard step 4, with the provider on/off) zooms every map to one region: `applyAlertsRegion()` sets `data-alerts-regions` (UA-32 adds UA-30, UA-43 adds UA-40); the whole country is still drawn, the viewBox is fitted to those outlines plus a 15% margin (`alertsFitViewBox`, from the path data, so it works while hidden), neighbours are dimmed (`data-context`) and the region gets a focus outline.

Map: `static/data/ukraine-regions.json` (regions by ISO 3166-2 code, districts by OCHA pcode with Ukrainian/English names and `alerts_uid`) is generated by `tools/build_ukraine_map.py` from OCHA COD-AB (CC BY-IGO — keep the attribution) + the alerts.in.ua location list (CSV). The frontend draws district fills + oblast outlines into every `[data-alerts-map]` (dashboard card + fullscreen banner).

### Smart Meter

`smart_meter.py` reads a Bakler Wi-Fi electricity meter over MQTT (paho-mqtt). The meter publishes to a broker — docker-compose runs `mosquitto` (`mosquitto/entrypoint.sh` builds its config from `SMART_METER_MQTT_USER`/`PASSWORD`, read from `data/.env` mounted read-only — no `env_file`, to stay compatible with older Compose on Synology; without them it listens on 127.0.0.1 only). Topics: `buckler/<serial>/status`, `dataJson` (every minute; three-phase meters send `[L1, L2, L3]` arrays), `data/<field>` (single values, incl. instantaneous `P`; three-phase `V1..V3` etc.), `relay/rN`. `SmartMeter` keeps the latest values and today's starting energy counter (`smart_meter_daily.json`, updated on every reading); `SmartMeterClient` subscribes and publishes only `data/set` (update interval). **Never publish to `relay/*`** — the relay is display-only. Retained messages (the broker's stored copies, sent on every (re)subscribe) are not new readings: `handle_message(..., retained=True)` skips retained data and only stores a retained relay state, so a dashboard restart or broker reconnect during an outage doesn't count as the meter being back. Served at `/api/smart-meter` (`status`: disabled/waiting/error/ok/offline/stale; "offline" comes from the `status` topic but shows only after `OFFLINE_GRACE` (90 s) without data, and any data message counts as online again — the broker's "offline" last will is retained and also arrives when the meter drops its connection for a moment; an "online" status re-sends the `data/set` interval, since a reconnected meter may be back on once a minute); the card is hidden when disabled (`SMART_METER_ENABLED`). `phases_on` is derived from per-phase voltage (`PHASE_ON_MIN_VOLTAGE`, 100 V). `phases_connected` comes from the relays when a 3-phase meter has exactly 3 (`r1..r3` = L1..L3, seen live: the meter cut L2 at 256 V and kept measuring its grid-side voltage); other relay layouts (e.g. 6 relays) aren't per phase → `None`. The card highlights a cut / no-voltage phase only while the meter is `ok` and another phase works. Outage history: `meter_outages.py` (`OutageLog`, today + yesterday, `meter_outages.json`; open outage saved too; < 10 s dropped; the meter's wait before switching the house back on — every phase cut with voltage on all, right after a total outage, ≤ the meter's switch-on delay (`SMART_METER_SWITCH_ON_DELAY`, default 120 s; not in its MQTT data) + `RESTORE_MARGIN_SECONDS` — is joined to that outage) is fed by `SmartMeter._check_grid()` on every voltage/relay update (all phases < 100 V → total; some dead or relay-cut → partial, `phases` + `cut`) and `check_offline()` (offline ≥ `OFFLINE_GRACE` → total from the offline time, unless `grid_online_hint` — the inverter, set by `app.py` — says the grid is there; checked every 15 s by `SmartMeterClient` and on `/api/meter-outages`). `SMART_METER_DEMO=1|3` swaps the MQTT client for `DemoMeterFeed` (simulated readings through `handle_message`; the three-phase demo drops L3 for one minute in five, `demo: true` in the API, "Demo data" badge, no daily file).

### Threading Model

Background services start once in `main()` (`init_services()`), not at import — importing `app` (tests, WSGI) starts nothing. Do not reintroduce a reloader/multi-process server: every poller would run in each process.

The app runs several daemon threads:
- `InverterPoller` — one full inverter read every 30s (`INVERTER_POLL_SECONDS`; also feeds the battery smoothing and the sparkline history), caches to JSON file every `CACHE_SAVE_SECONDS` (2 min)
- `inverter-detect` — one-off background auto-detect; updates the shared `InverterConfig` in place and saves it to `inverter_config.json` for the next start
- `WeatherPoller` — fetches Open-Meteo weather every 15 min
- `OutageSchedulePoller` — fetches outage schedule every 60s
- `TelegramBot` — runs Telegram polling loop (long polling, `LONG_POLL_SECONDS`, over one `requests.Session`)
- `AlertsPoller` — fetches air raid alerts every 30s
- paho-mqtt network loop (`SmartMeterClient`, when the smart meter is enabled) — handles meter messages as they arrive

Transient failures never replace good data: `InverterPoller` keeps the last good readings through a failed poll (until they're older than `INVERTER_CACHE_MAX_AGE`, 5 min; ages come from the readings' epoch `timestamp`, not local time, which repeats an hour in October; `/api/data` reports `data_age_seconds`), and the frontend keeps every card's last data through failed requests, reporting a problem only after `STALE_ERROR_MS` (60 s) without an answer (`noDataFor()`). Error texts must name the real cause: a failed request from the browser is "Connection failed" / "No connection", never e.g. "MQTT broker unavailable".

All pollers use thread locks for safe cache access. The inverter connection (the driver's `lock`) is shared between `InverterPoller` and auto-detect (one logger connection per poll: its few TCP slots are the scarce resource). The Telegram bot reads `InverterPoller.data` (no direct inverter reads).

Data files (stats, logs, caches, bot state) default to `storage.data_path(name)` = `data/<name>` when `data/` exists (the Docker volume, so they survive deploys), else next to the app; tests redirect them all to a temp folder (autouse fixture in `tests/conftest.py`). They go through `storage.py`: `atomic_write_json()` (temp file per process/thread + fsync + rename, safe across power cuts and concurrent saves) and `load_json()` (missing/corrupt file → default; corrupt files are moved to `*.corrupt`). Read-modify-write updates in `app.py` hold `_data_lock`. Disk writes are kept rare on the NAS: phase stats live in memory (written every `PHASE_STATS_SAVE_SECONDS`, on a new day and when cleared), the grid and generator logs are written only when they change (a running generator's `last_seen` every minute).

The container starts as root only for `docker-entrypoint.sh`: it chowns to uid 1000 whatever in `data/` that user can't write (never `data/mosquitto/`), then runs the app as uid 1000 via `setpriv`. Keep the app itself non-root.

Config saved from the UI is validated by `setup.validate_config()` (types, ranges, no line breaks, no unknown keys) before `.env` is written — atomically and owner-only (0600; in Docker `.env` is a link into `data/`, the real file is replaced). Intervals and ages use `time.monotonic()` / epoch time, never naive local time (DST): phase stats, inverter data age, the bot loop; the meter and its outage log keep times with their UTC offset (`meter_outages.local_now`). The grid daily import log follows the inverter's own counter reset near midnight (`GRID_DAY_DRIFT`), and an open generator session stores `last_seen`, so app downtime isn't counted as runtime. Auto-detect raises (the current config is kept) when the inverter doesn't answer its reads, and never replaces a known model with "unknown".

## Deye Driver (`inverters/deye.py`)

- Uses `pysolarmanv5` library for Modbus over TCP
- Port 8899 (Solarman V5 protocol)
- **Use holding registers** (`read_holding_registers`), not input registers
- Slave ID: 1
- Connection is opened per-poll and closed after each read cycle (see `read_all_data` → `disconnect()` in finally block)
- 50ms sleep between register requests to reduce logger connection pressure; the 3-phase poll reads neighbouring registers in blocks first (`BLOCKS_3P`, `_prefetch()`: ~11 requests instead of ~27; `read_register()` answers from the block, a failed block falls back to single reads)
- Configuration via environment variables: `INVERTER_IP`, `LOGGER_SERIAL` (`INVERTER_TYPE=deye` or unset)
- Inverter capabilities (phases, battery, PV strings) auto-detected at startup via `detect_config()`
- Register 0 = device type → family (`DEVICE_FAMILIES` in `inverters/deye.py`, codes from ha-solarman's Deye autodetection): `hybrid_1p` (Sunsynk-style map), `hybrid_3p_lv`, `hybrid_3p_hv` (same map as LV, but battery voltage ×0.1 V and battery/PV power ×10 W — `DeyeInverter.high_voltage`), `string` / `micro` (other maps, not implemented: `supported: false` → warning on the dashboard). A known family sets the phase count without L2/L3 voltage sampling; unknown codes fall back to it. Read even when all `INVERTER_*` capabilities are set in env (`refresh_device_type`)

## Key Registers (Holding) — Deye 3-Phase Hybrid

| Register | Description | Scale |
|----------|-------------|-------|
| 672, 673 | PV1/PV2 Power (514/515 are day battery charge/discharge, not PV!) | W |
| 586 | Battery Temperature (not current!) | (x−1000)/10 °C |
| 587 | Battery Voltage | /100 V |
| 588 | Battery SOC | % |
| 590 | Battery Power (signed, discharge > 0; negated in code) | W |
| 591 | Battery Current (signed, discharge > 0; negated in code) | /100 A |
| 598-600 | Grid Voltage L1-L3 | /10 V |
| 552 | Relay status bits: 0 inverter, 2 grid, 3 generator → `grid_online` (0 = unsupported/all off → all phases ≥ 100 V). Dashboard, battery runtime and Telegram outage alerts use `grid_online`, not the L1 voltage. When offline, `grid_state` says why: `partial` (some phases < 100 V, listed in `grid_phases_missing` → amber "L2 DOWN"), `out_of_range` (all phases present, inverter still disconnected → "ON BATTERY"), `outage` (no supply → red "OUTAGE") | bits |
| 592 / 102 | Battery capacity: BMS-measured ("corrected") / inverter setting — runtime estimate | Ah |
| 115 | Battery shutdown SOC — runtime estimate counts down to it | % |
| 607 | Grid Power (signed) | W |
| 644-646 | Phase Voltages L1-L3 | /10 V |
| 650-653 | Phase Loads L1-L3, Total Load | W |
| 667 | Generator Power (GEN/GRID2 port) | W |
| 529, 520, 521, 526 | Daily PV/Import/Export/Load (502 = inverter output, not PV) | /10 kWh |
| 514, 515 | Daily Battery Charge/Discharge | /10 kWh |

## Key Registers (Holding) — Deye 1-Phase Hybrid (Sunsynk)

| Register | Description | Scale |
|----------|-------------|-------|
| 186, 187 | PV1/PV2 Power | W |
| 191 | Battery Current (signed, negate) | /100 A |
| 183 | Battery Voltage | /100 V |
| 184 | Battery SOC | % |
| 150 | Grid Voltage | /10 V |
| 169 | Grid Power (signed) | W |
| 166 | Generator Power (GEN/GRID2 port) | W |
| 176, 178 | Load L1, Total Load | W |
| 108, 76, 77, 84 | Daily PV/Import/Export/Load | /10 kWh |

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/data` | GET | Current inverter readings |
| `/api/weather` | GET | Current weather and forecast |
| `/api/outage_schedule` | GET | Outage status and today's/tomorrow's planned windows |
| `/api/phase-stats` | GET | Daily phase statistics |
| `/api/generator` | GET | Generator status and runtime |
| `/api/phase-stats/clear` | POST | Clear phase statistics |
| `/api/alerts` | GET | Air raid alert statuses by region/district |
| `/api/smart-meter` | GET | Smart meter readings (`status: disabled` when off) |
| `/api/meter-outages` | GET | Grid outages the smart meter saw today and yesterday, newest first (`type` total/partial, `start`, `end` null while running, `phases`, `cut`; `status: disabled` without the meter) |
| `/api/history` | GET | Last 24 h of battery SOC, PV and load in 5-minute averages for the card sparklines (`history.py`: `PowerHistory`, fed by `InverterPoller` with good readings only, saved to `power_history.json` when a bucket closes; parallel lists `t`/`soc`/`pv`/`load`, `null` = no reading, plus `bucket_seconds` and the server's `now`) |
| `/api/outage-readiness` | GET | Will the battery last through the next planned outage (`outage_readiness.py`: recent average load × window vs charge above the shutdown SOC; `status`: disabled/no_schedule/no_data/no_outage/ok, `verdict`: enough/tight/short) |

## Configuration

All via environment variables (`.env` file). See `.env.example` for full template.

Required: `INVERTER_IP`, `LOGGER_SERIAL`

Optional: `SETTINGS_PIN` (settings panel PIN, default `0000`; endpoints behind it use `@require_settings_pin`, required once a `.env` exists — `settings_protected()`, even when it's invalid and the wizard opens; only a true first start is open), `WEATHER_LATITUDE/LONGITUDE`, `OUTAGE_PROVIDER` (lvivoblenergo/yasno/dtek/none), `TELEGRAM_ENABLED`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`
