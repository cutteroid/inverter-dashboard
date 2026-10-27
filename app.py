"""Web dashboard for hybrid solar inverters (inverter drivers: see inverters/)."""
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask, render_template, jsonify, request, url_for
from inverters import DRIVERS, InverterConfig, create_inverter, driver_class
from telegram_bot import TelegramBot
from outage_providers import OutageSchedulePoller, create_outage_provider
from storage import atomic_write_json, data_path, load_json
from air_alerts import AlertsPoller, create_alerts_provider, REGION_CODES
from outage_readiness import outage_readiness
from smart_meter import create_smart_meter_from_env
from history import PowerHistory
from datetime import datetime, date, timedelta
from functools import wraps
import hashlib
import hmac
import os
import json
import logging
import subprocess
import threading
import time
import uuid
import requests

# "or": a key saved empty must not end up as "latitude=" in the request
WEATHER_LATITUDE = os.environ.get("WEATHER_LATITUDE") or "0.0"
WEATHER_LONGITUDE = os.environ.get("WEATHER_LONGITUDE") or "0.0"
OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={WEATHER_LATITUDE}&longitude={WEATHER_LONGITUDE}"
    "&current=temperature_2m,weather_code"
    "&daily=sunrise,sunset,temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code"
    "&timezone=auto&forecast_days=2"
)

logger = logging.getLogger(__name__)


def env_float(name, default):
    """Float env var; a bad value falls back to the default instead of crashing."""
    raw = os.environ.get(name, "")
    try:
        return float(raw) if raw.strip() else default
    except ValueError:
        logger.warning("Ignoring invalid %s=%r, using %s", name, raw, default)
        return default


def is_configured():
    """Are the selected inverter driver's settings complete and valid?

    Invalid settings count as not configured, so the dashboard opens the setup
    wizard (where they can be fixed) instead of failing at startup.
    """
    try:
        driver = driver_class(os.environ.get("INVERTER_TYPE"))
    except ValueError as e:
        logger.warning("%s; starting in setup mode", e)
        return False
    problem = driver.settings_problem(os.environ)
    if problem and any(os.environ.get(key) for key in driver.required_settings):
        logger.warning("Inverter settings: %s; starting in setup mode", problem)
    return not problem


def parse_weather(raw):
    """Open-Meteo response -> today's values plus a "tomorrow" summary (None if missing)."""
    current = raw.get("current") or {}
    daily = raw.get("daily") or {}

    def day(key, i):
        values = daily.get(key) or []
        return values[i] if len(values) > i else None

    result = {
        "temperature": current.get("temperature_2m"),
        "weather_code": current.get("weather_code"),
        "sunrise": day("sunrise", 0),
        "sunset": day("sunset", 0),
        "temp_max": day("temperature_2m_max", 0),
        "temp_min": day("temperature_2m_min", 0),
        "precipitation": day("precipitation_sum", 0),
        "daily_weather_code": day("weather_code", 0),
        "tomorrow": None,
        "last_updated": datetime.now().isoformat(),
    }
    if day("time", 1) is not None:
        result["tomorrow"] = {
            "date": day("time", 1),
            "weather_code": day("weather_code", 1),
            "temp_max": day("temperature_2m_max", 1),
            "temp_min": day("temperature_2m_min", 1),
            "precipitation": day("precipitation_sum", 1),
        }
    return result


class WeatherPoller:
    """Polls Open-Meteo API for current weather conditions."""

    # Without any weather yet (e.g. the NAS booted before its network was up),
    # try again after this long instead of the full poll interval
    RETRY_SECONDS = 60

    def __init__(self, poll_interval=900):
        self.poll_interval = poll_interval
        self._cache = {}
        self._lock = threading.Lock()

    def _fetch(self):
        try:
            resp = requests.get(OPEN_METEO_URL, timeout=15)
            if not resp.ok:
                logger.warning("Open-Meteo API returned %s", resp.status_code)
                return
            result = parse_weather(resp.json())
            with self._lock:
                self._cache = result
            logger.info("Weather updated: %s°C code=%s", result.get("temperature"), result.get("weather_code"))
        except Exception:
            logger.exception("Error fetching weather data")

    @property
    def data(self):
        with self._lock:
            return dict(self._cache) if self._cache else None

    def _run(self):
        while True:
            self._fetch()
            time.sleep(self.poll_interval if self.data else self.RETRY_SECONDS)

    def start(self):
        t = threading.Thread(target=self._run, daemon=True)
        t.start()


# Data files default to data/ (persistent Docker volume) — see storage.data_path()
INVERTER_CACHE_FILE = os.environ.get("INVERTER_CACHE_FILE") or data_path("inverter_cache.json")
POWER_HISTORY_FILE = os.environ.get("POWER_HISTORY_FILE") or data_path("power_history.json")
# Overrides the capacity the inverter reports (for the battery runtime estimate)
BATTERY_CAPACITY_KWH = env_float("BATTERY_CAPACITY_KWH", 0.0)
INVERTER_POLL_SECONDS = 30   # one full read; battery voltage/SOC are smoothed over the last 6 (3 min)
RUNTIME_AVERAGE_SAMPLES = 20  # polls (~10 min) of discharge power behind the estimate
LOAD_AVERAGE_SAMPLES = 60     # polls (~30 min) of home load behind the outage readiness


def battery_capacity_wh(data):
    """Full battery capacity in Wh: BATTERY_CAPACITY_KWH, else the inverter's Ah x the
    battery's nominal voltage (51.2 V for a 48 V LiFePO4 battery). The live voltage
    is only a fallback: it runs ~8% above nominal when the battery is full."""
    if BATTERY_CAPACITY_KWH > 0:
        return BATTERY_CAPACITY_KWH * 1000
    capacity_ah = data.get("battery_capacity_ah") or 0
    voltage = data.get("battery_nominal_voltage") or data.get("battery_voltage") or 0
    return capacity_ah * voltage if capacity_ah > 0 and voltage > 0 else None


def estimate_battery_runtime(data, average_discharge_w):
    """Minutes until the battery reaches its shutdown SOC at the given discharge power.

    None when it can't be estimated (not discharging, capacity unknown).
    """
    capacity = battery_capacity_wh(data)
    if not capacity or not average_discharge_w or average_discharge_w <= 0:
        return None
    soc = data.get("battery_soc") or 0
    shutdown = data.get("battery_shutdown_soc") or 0
    if not 0 <= shutdown < 100:
        shutdown = 0
    usable_wh = capacity * max(0, soc - shutdown) / 100
    return int(usable_wh / average_discharge_w * 60)


def estimate_battery_charge_time(data, average_charge_w):
    """Minutes until the battery is full at the given charge power (None if unknown/full).

    Linear, so a little optimistic near 100% where chargers taper off.
    """
    capacity = battery_capacity_wh(data)
    soc = data.get("battery_soc") or 0
    if not capacity or not average_charge_w or average_charge_w <= 0 or soc >= 100:
        return None
    return int(capacity * (100 - soc) / 100 / average_charge_w * 60)
INVERTER_CACHE_MAX_AGE = 300  # seconds – serve cached data if fresher than 5 min


class InverterPoller:
    """Polls inverter for current data in a background thread."""

    def __init__(self, inverter, poll_interval=INVERTER_POLL_SECONDS, cache_file=None, history=None):
        self.inverter = inverter
        self.poll_interval = poll_interval
        self.history = history  # PowerHistory for the sparklines (good readings only)
        self._cache = {}
        self._lock = threading.Lock()
        self.cache_file = cache_file
        self._flow_samples = []  # recent charge or discharge power (W) for the time estimates
        self._load_samples = []  # recent home load (W) for the outage readiness
        self._flow_direction = 0  # 1 charging, -1 discharging, 0 idle
        self._load_cache()

    def _load_cache(self):
        """Load last inverter data from file if fresh enough."""
        if not self.cache_file:
            return
        try:
            data = load_json(self.cache_file, {})
            if data.get("last_updated"):
                age = self._age(data)
                if age < INVERTER_CACHE_MAX_AGE:
                    with self._lock:
                        self._cache = data
                    logger.info("Loaded inverter cache (%.0fs old)", age)
                else:
                    logger.info("Inverter cache too old (%.0fs), ignoring", age)
        except Exception:
            logger.exception("Failed to load inverter cache")

    def _save_cache(self):
        """Persist latest inverter data to file."""
        if not self.cache_file:
            return
        try:
            with self._lock:
                data = dict(self._cache)
            atomic_write_json(self.cache_file, data)
        except Exception:
            logger.exception("Failed to save inverter cache")

    def _fetch(self):
        try:
            t0 = time.time()
            result = self.inverter.read_all_data()
            elapsed = time.time() - t0

            if result.get("error"):
                logger.warning("Inverter poll returned error after %.1fs: %s", elapsed, result["error"])
            else:
                logger.info("Inverter poll OK in %.1fs (PV=%dW load=%dW grid=%dW)",
                            elapsed,
                            result.get("pv_total_power", 0),
                            result.get("load_power", 0),
                            result.get("grid_power", 0))

            result["last_updated"] = datetime.now().isoformat()
            result["timestamp"] = time.time()   # for the age: unaffected by DST changes
            if result.get("error"):
                # A single failed poll (the logger is often briefly busy) keeps the
                # last good readings; the error only replaces them once they're
                # older than INVERTER_CACHE_MAX_AGE
                with self._lock:
                    previous = self._cache
                if previous and not previous.get("error") and self._age(previous) < INVERTER_CACHE_MAX_AGE:
                    return
            else:
                self._add_runtime_estimate(result)
                self._add_load_average(result)
                self._add_battery_cycles(result)
                if self.history is not None:
                    self.history.add(result)
            with self._lock:
                self._cache = result
            self._save_cache()

            # Record daily grid import for monthly totals
            if "daily_grid_import" in result:
                record_grid_daily_import(result["daily_grid_import"])

            # Track generator runtime
            if inverter_config.has_generator and "generator_power" in result:
                track_generator_runtime(result["generator_power"])

            # Record phase sample for analytics (only for 3-phase)
            if "load_l1" in result and inverter_config.phases == 3:
                record_phase_sample(
                    result.get("load_l1", 0),
                    result.get("load_l2", 0),
                    result.get("load_l3", 0),
                )
        except Exception:
            logger.exception("Error polling inverter data")

    @staticmethod
    def _age(data):
        """Seconds since the readings were taken (infinite if unknown). From the
        epoch timestamp: local time repeats an hour when the clocks go back."""
        try:
            if isinstance(data.get("timestamp"), (int, float)):
                return time.time() - data["timestamp"]
            return (datetime.now() - datetime.fromisoformat(data["last_updated"])).total_seconds()
        except (AttributeError, KeyError, TypeError, ValueError):
            return float("inf")

    def _add_runtime_estimate(self, result):
        """Battery time estimates (minutes) at the average power of recent polls:
        battery_runtime_minutes while discharging (until the shutdown SOC),
        battery_charge_minutes while charging (until 100%).

        Averaging keeps the estimates from jumping with every kettle; switching
        between charging, discharging and idle starts the average over.
        """
        power = result.get("battery_power") or 0  # > 0 charging, < 0 discharging
        direction = 1 if power > 20 else -1 if power < -20 else 0
        if direction and direction == self._flow_direction:
            self._flow_samples = (self._flow_samples + [abs(power)])[-RUNTIME_AVERAGE_SAMPLES:]
        else:
            self._flow_samples = [abs(power)] if direction else []
        self._flow_direction = direction
        average = sum(self._flow_samples) / len(self._flow_samples) if self._flow_samples else 0
        result["battery_runtime_minutes"] = estimate_battery_runtime(result, average) if direction < 0 else None
        result["battery_charge_minutes"] = estimate_battery_charge_time(result, average) if direction > 0 else None

    def _add_load_average(self, result):
        """load_average_w: home load averaged over the last ~30 minutes."""
        load = result.get("load_power")
        if isinstance(load, (int, float)) and load >= 0:
            self._load_samples = (self._load_samples + [load])[-LOAD_AVERAGE_SAMPLES:]
        if self._load_samples:
            result["load_average_w"] = round(sum(self._load_samples) / len(self._load_samples))

    @staticmethod
    def _add_battery_cycles(result):
        """battery_cycles: total energy discharged / full capacity (equivalent full cycles)."""
        discharged = result.get("battery_total_discharge_kwh")
        capacity = battery_capacity_wh(result)
        if discharged and capacity:
            result["battery_cycles"] = round(discharged * 1000 / capacity)

    @property
    def data(self):
        with self._lock:
            return dict(self._cache) if self._cache else None

    def _run(self):
        while True:
            self._fetch()
            time.sleep(self.poll_interval)

    def start(self):
        t = threading.Thread(target=self._run, daemon=True)
        t.start()


app = Flask(__name__)

# UI translations: one JSON file per language in locales/, embedded into the page
LOCALES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")
DEFAULT_LANGUAGE = "en"


def load_locales(locales_dir=LOCALES_DIR):
    """Load locales/<lang>.json files, default language first."""
    locales = {}
    names = sorted(f[:-5] for f in os.listdir(locales_dir) if f.endswith(".json"))
    names.sort(key=lambda name: name != DEFAULT_LANGUAGE)
    for name in names:
        with open(os.path.join(locales_dir, f"{name}.json"), encoding="utf-8") as f:
            locales[name] = json.load(f)
    return locales


LOCALES = load_locales()

# Static assets (static/css, static/js) get a content hash in their URL, so browsers
# load the new file after an update instead of a cached old one.
_asset_hashes = {}  # path -> (mtime_ns, short hash)


def asset_version(filename):
    """Short content hash of a file in static/, recomputed only when it changes."""
    path = os.path.join(app.static_folder, filename)
    mtime = os.stat(path).st_mtime_ns
    cached = _asset_hashes.get(filename)
    if cached and cached[0] == mtime:
        return cached[1]
    with open(path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()[:12]
    _asset_hashes[filename] = (mtime, digest)
    return digest


def asset_version_url(filename):
    """URL of a static file with its content hash, e.g. /static/css/x.css?v=ab12cd34ef56."""
    return url_for("static", filename=filename, v=asset_version(filename))


@app.context_processor
def inject_asset_url():
    return {"asset_url": asset_version_url}

# Configuration - can be overridden with environment variables
PHASE_STATS_FILE = os.environ.get("PHASE_STATS_FILE") or data_path("phase_stats.json")
GRID_DAILY_LOG_FILE = os.environ.get("GRID_DAILY_LOG_FILE") or data_path("grid_daily_log.json")
GENERATOR_LOG_FILE = os.environ.get("GENERATOR_LOG_FILE") or data_path("generator_log.json")
GENERATOR_FUEL_RATE = env_float("GENERATOR_FUEL_RATE", 0.0)
GENERATOR_OIL_CHANGE_DATE = os.environ.get("GENERATOR_OIL_CHANGE_DATE", "")


INVERTER_CONFIG_FILE = os.environ.get("INVERTER_CONFIG_FILE") or data_path("inverter_config.json")
_CONFIG_ENV_KEYS = ("INVERTER_PHASES", "INVERTER_HAS_BATTERY", "INVERTER_PV_STRINGS", "INVERTER_HAS_GENERATOR")


def _env_bool(value):
    return value.lower() in ("true", "1", "yes")


def env_config_complete():
    """True if all inverter capabilities are set in env (no detection needed)."""
    return all(os.environ.get(key) for key in _CONFIG_ENV_KEYS)


def apply_env_overrides(base):
    """Return a copy of base with any INVERTER_* env values applied on top."""
    env_phases = os.environ.get("INVERTER_PHASES")
    env_battery = os.environ.get("INVERTER_HAS_BATTERY")
    env_pv = os.environ.get("INVERTER_PV_STRINGS")
    env_generator = os.environ.get("INVERTER_HAS_GENERATOR")
    return InverterConfig(
        phases=int(env_phases) if env_phases else base.phases,
        has_battery=_env_bool(env_battery) if env_battery else base.has_battery,
        pv_strings=int(env_pv) if env_pv else base.pv_strings,
        has_generator=_env_bool(env_generator) if env_generator else base.has_generator,
        device_type=base.device_type,
        family=base.family,
        supported=base.supported,
    )


def load_cached_inverter_config():
    """Last auto-detected config saved by refresh_inverter_config(), or None."""
    data = load_json(INVERTER_CONFIG_FILE, None)
    if not isinstance(data, dict):
        return None
    try:
        device_type = data.get("device_type")
        return InverterConfig(
            phases=int(data["phases"]), has_battery=bool(data["has_battery"]),
            pv_strings=int(data["pv_strings"]), has_generator=bool(data["has_generator"]),
            device_type=int(device_type) if device_type is not None else None,
            family=str(data.get("family") or "unknown"),
            supported=bool(data.get("supported", True)),
        )
    except (KeyError, TypeError, ValueError):
        logger.warning("Ignoring invalid cached inverter config in %s", INVERTER_CONFIG_FILE)
        return None


def initial_inverter_config():
    """Config to start with immediately: env, else last detected, else defaults."""
    cached = None if env_config_complete() else load_cached_inverter_config()
    if cached:
        logger.info("Using last detected inverter config: %s", cached.to_dict())
    return apply_env_overrides(cached or InverterConfig())


def refresh_inverter_config(inv, config):
    """Auto-detect capabilities, save them for the next start and update config in place.

    Runs in the background so startup isn't blocked by detection (several
    seconds of inverter reads and sleeps). Pollers read the shared config
    object on every poll, so an update takes effect on the next read.
    """
    try:
        detected = inv.detect_config()
    except Exception as e:
        logger.warning("Auto-detect failed (%s), keeping %s", e, config.to_dict())
        return
    if detected.family == "unknown" and config.family != "unknown":
        # The model register didn't answer this time: keep the model already known
        detected.device_type, detected.family = config.device_type, config.family
        detected.supported = config.supported
    try:
        atomic_write_json(INVERTER_CONFIG_FILE, detected.to_dict())
    except OSError:
        logger.exception("Could not save detected inverter config")
    updated = apply_env_overrides(detected)
    if updated != config:
        logger.info("Inverter config updated by auto-detect: %s -> %s", config.to_dict(), updated.to_dict())
        for field in ("phases", "has_battery", "pv_strings", "has_generator", "device_type", "family", "supported"):
            setattr(config, field, getattr(updated, field))


def refresh_device_type(inv, config):
    """Read only the device type (capabilities all set in env, so no full detection)."""
    try:
        device_type, family = inv.read_device_type()
        if family != "unknown" or device_type is not None:
            config.device_type, config.family = device_type, family
            config.supported = inv.family_supported(family)
    except Exception:
        logger.exception("Device type check failed, keeping %s", config.to_dict())


# Service variables — initialised by init_services() when configured
inverter = None
inverter_config = None
outage_poller = None
weather_poller = None
inverter_poller = None
power_history = None
alerts_poller = None
smart_meter = None

_configured = is_configured()

# The app's .env (in Docker a link to data/.env)
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def settings_protected():
    """Do the settings need the PIN? Yes once there is a configuration, even an
    invalid one (it then opens the setup wizard, but its tokens and PIN still
    need protecting); only a true first start, without .env, is open."""
    return _configured or os.path.exists(ENV_FILE) or bool(os.environ.get("SETTINGS_PIN"))

# New on every start: after saving the settings, the page waits for a new value
# before reloading, so it doesn't reload from the process that is about to exit
BOOT_ID = uuid.uuid4().hex[:12]

# Settings PIN: basic protection for the settings panel and the endpoints behind it.
DEFAULT_SETTINGS_PIN = "0000"
PIN_MAX_ATTEMPTS = 5
PIN_LOCKOUT_SECONDS = 60
_pin_lock = threading.Lock()
_pin_failures = 0
_pin_locked_until = 0.0


def check_settings_pin(pin):
    """Check a settings PIN, locking out further tries after repeated failures.

    Returns (ok, retry_after_seconds); retry_after is set while locked out.
    """
    global _pin_failures, _pin_locked_until
    expected = os.environ.get("SETTINGS_PIN") or DEFAULT_SETTINGS_PIN
    with _pin_lock:
        now = time.time()
        if now < _pin_locked_until:
            return False, int(_pin_locked_until - now) + 1
        if hmac.compare_digest(str(pin or "").encode(), expected.encode()):
            _pin_failures = 0
            return True, None
        _pin_failures += 1
        if _pin_failures >= PIN_MAX_ATTEMPTS:
            _pin_failures = 0
            _pin_locked_until = now + PIN_LOCKOUT_SECONDS
            logger.warning("Settings PIN: too many wrong attempts, locked for %ds",
                           PIN_LOCKOUT_SECONDS)
            return False, PIN_LOCKOUT_SECONDS
        return False, None


def _pin_error(retry_after):
    if retry_after:
        return jsonify({"status": "error", "error": "Too many attempts", "pin_required": True,
                        "retry_after": retry_after}), 429
    return jsonify({"status": "error", "error": "Invalid PIN", "pin_required": True}), 401


def require_settings_pin(view):
    """Require a valid X-Settings-Pin header once there is a configuration
    (settings_protected). A true first start stays open: nothing to protect yet.
    """
    @wraps(view)
    def wrapper(*args, **kwargs):
        if settings_protected():
            ok, retry_after = check_settings_pin(request.headers.get("X-Settings-Pin"))
            if not ok:
                return _pin_error(retry_after)
        return view(*args, **kwargs)
    return wrapper


def init_services():
    """Initialise all background services (inverter, pollers, etc.)."""
    global inverter, inverter_config
    global outage_poller, weather_poller, inverter_poller, power_history
    global alerts_poller, smart_meter

    inverter = create_inverter(os.environ)
    inverter_config = initial_inverter_config()
    inverter.config = inverter_config
    # Background: full auto-detect, or (all capabilities set in env) just the
    # device type, which selects HV scaling and flags unsupported inverters
    detect = refresh_device_type if env_config_complete() else refresh_inverter_config
    threading.Thread(target=detect, args=(inverter, inverter_config),
                     name="inverter-detect", daemon=True).start()

    # Outage schedule provider
    outage_provider_name = os.environ.get("OUTAGE_PROVIDER", "lvivoblenergo")
    outage_group = os.environ.get("OUTAGE_GROUP")
    outage_prov = create_outage_provider(
        outage_provider_name,
        group=outage_group,
        region_id=os.environ.get("OUTAGE_REGION_ID", "25"),
        dso_id=os.environ.get("OUTAGE_DSO_ID", "902"),
        dtek_region=os.environ.get("OUTAGE_DTEK_REGION"),
    )
    if outage_prov is not None:
        outage_poller = OutageSchedulePoller(provider=outage_prov)
        outage_poller.start()
    else:
        logger.info("Outage schedule disabled (OUTAGE_PROVIDER=none)")

    # Smart electricity meter over MQTT (SMART_METER_ENABLED=true)
    smart_meter, smart_meter_client = create_smart_meter_from_env()
    if smart_meter_client is not None:
        smart_meter_client.start()
        logger.info("Smart meter: %s", smart_meter_client.description)

    # Air raid alerts: alerts.in.ua with a token, else NEPTUN (no key needed);
    # "mock" for demo data, "none" to hide
    alerts_provider = create_alerts_provider(os.environ.get("AIR_ALERTS_PROVIDER"),
                                             os.environ.get("ALERTS_IN_UA_TOKEN"))
    if alerts_provider is not None:
        logger.info("Air raid alerts: %s", alerts_provider.display_name)
        alerts_poller = AlertsPoller(alerts_provider)
        alerts_poller.start()
    else:
        logger.info("Air raid alerts disabled (AIR_ALERTS_PROVIDER=none)")

    weather_poller = WeatherPoller()
    weather_poller.start()
    power_history = PowerHistory(POWER_HISTORY_FILE)
    inverter_poller = InverterPoller(inverter, cache_file=INVERTER_CACHE_FILE, history=power_history)
    inverter_poller.start()
    if smart_meter is not None:
        smart_meter.grid_online_hint = inverter_grid_online



# Read-modify-write of the data files happens from the poller thread and from
# request threads (e.g. "clear"); this lock keeps those updates from interleaving.
_data_lock = threading.RLock()

# Phase data collection: time.monotonic() of the last sample (the energy
# between two samples needs a clock that doesn't go back an hour in October)
last_sample_time = None
PHASE_SAMPLE_MAX_GAP_SECONDS = 360


def load_grid_daily_log():
    """Load grid daily import log from file."""
    return load_json(GRID_DAILY_LOG_FILE, {})


def save_grid_daily_log(log):
    """Save grid daily import log to file."""
    atomic_write_json(GRID_DAILY_LOG_FILE, log, indent=2)


# The inverter's daily import counter resets at the inverter's own midnight,
# which drifts from this machine's clock. A drop of the counter within
# GRID_DAY_DRIFT of midnight is that reset: the reading before it is the
# finished day's total, the readings after it count for the next day, on
# whichever side of our midnight it happened. Without a reset seen (no import
# all day, or the app just started) a reading counts for our own date; within
# GRID_DAY_DRIFT of midnight that's a guess, so it may only raise a day's
# value. A drop at any other time is a glitch and skipped.
GRID_DAY_DRIFT = timedelta(hours=1)
_grid_counter = {"last": None, "day": None}   # last reading, the date it counts for


def record_grid_daily_import(daily_kwh, now=None):
    with _data_lock:
        return _record_grid_daily_import(daily_kwh, now)


def _record_grid_daily_import(daily_kwh, now=None):
    """Record the inverter's daily grid import counter (kWh) for the day it counts."""
    now = now or datetime.now()
    last, day = _grid_counter["last"], _grid_counter["day"]
    midnight = datetime.combine((now + GRID_DAY_DRIFT).date(), datetime.min.time())
    near_midnight = abs(now - midnight) <= GRID_DAY_DRIFT
    log = load_grid_daily_log()
    sure = not near_midnight
    if last is not None and daily_kwh < last:
        if not near_midnight:
            return  # a glitch, not the daily reset
        ended = midnight.date() - timedelta(days=1)
        log[ended.isoformat()] = last
        day, sure = midnight.date(), True
    if day is None or (day < now.date() and not near_midnight):
        day = now.date()
    _grid_counter["last"], _grid_counter["day"] = daily_kwh, day
    key = day.isoformat()
    log[key] = daily_kwh if sure else max(log.get(key) or 0, daily_kwh)
    # Keep only last 90 days
    sorted_dates = sorted(log.keys(), reverse=True)
    if len(sorted_dates) > 90:
        for old_date in sorted_dates[90:]:
            del log[old_date]
    save_grid_daily_log(log)


def load_generator_log():
    """Load generator runtime log from file."""
    return load_json(GENERATOR_LOG_FILE, {})


def save_generator_log(log):
    """Save generator runtime log to file."""
    atomic_write_json(GENERATOR_LOG_FILE, log, indent=2)


# Generator runtime tracking state
generator_last_running = None
generator_session_start = None
# An open session whose last reading ("last_seen") is older than this when the
# app starts again ended while the app was down: it's closed at that reading
GENERATOR_RESUME_GAP = timedelta(minutes=5)


def track_generator_runtime(generator_power, now=None):
    with _data_lock:
        return _track_generator_runtime(generator_power, now)


def _parse_time(text):
    """ISO date-time string -> datetime, None if missing or invalid."""
    try:
        return datetime.fromisoformat(text) if text else None
    except (TypeError, ValueError):
        return None


def _find_open_generator_session(log):
    """(date_key, session) of the last session without an end, or (None, None)."""
    for day in sorted(log.keys(), reverse=True):
        sessions = log[day].get("sessions", [])
        if sessions and sessions[-1].get("end") is None:
            return day, sessions[-1]
    return None, None


def _track_generator_runtime(generator_power, now=None):
    """Track generator runtime based on power readings.

    Sessions are stored per day; a session running past midnight is split at
    midnight so each day gets its own runtime. The open session lives in the log
    (end=None), so its start is recovered after an app restart.
    """
    global generator_last_running, generator_session_start

    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    running = generator_power > 0

    log = load_generator_log()

    if today not in log:
        log[today] = {"runtime_seconds": 0, "sessions": []}

    if generator_last_running is None:
        # First reading since start: resume a session left open by a restart
        day, session = _find_open_generator_session(log)
        if session:
            start = datetime.strptime(f"{day} {session['start']}", "%Y-%m-%d %H:%M:%S")
            last_seen = _parse_time(session.get("last_seen"))
            if last_seen and now - last_seen > GENERATOR_RESUME_GAP:
                # The app was down: what the generator did meanwhile is unknown, so
                # the session counts up to its last reading (not up to now)
                log[day]["runtime_seconds"] += int((last_seen - start).total_seconds())
                session["end"] = last_seen.strftime("%H:%M:%S")
                session.pop("last_seen", None)
                logger.info("Generator session %s - %s closed (app was down since)", start, last_seen)
            else:
                generator_session_start = start
                generator_last_running = True
                logger.info("Resuming generator session started %s", generator_session_start)

    # Split a session that crossed midnight: close it at midnight, continue today
    while generator_last_running and generator_session_start and generator_session_start.date() < now.date():
        session_day = generator_session_start.strftime("%Y-%m-%d")
        midnight = datetime.combine(generator_session_start.date() + timedelta(days=1), datetime.min.time())
        day_log = log.setdefault(session_day, {"runtime_seconds": 0, "sessions": []})
        day_log["runtime_seconds"] += int((midnight - generator_session_start).total_seconds())
        if day_log["sessions"] and day_log["sessions"][-1]["end"] is None:
            day_log["sessions"][-1]["end"] = "24:00:00"
            day_log["sessions"][-1].pop("last_seen", None)
        next_day = log.setdefault(midnight.strftime("%Y-%m-%d"), {"runtime_seconds": 0, "sessions": []})
        next_day["sessions"].append({"start": "00:00:00", "end": None})
        generator_session_start = midnight

    if running and not generator_last_running:
        # Transition: off → on — start new session
        generator_session_start = now
        log[today]["sessions"].append({
            "start": now.strftime("%H:%M:%S"),
            "end": None,
        })
    elif not running and generator_last_running:
        # Transition: on → off — close session
        if generator_session_start:
            elapsed = (now - generator_session_start).total_seconds()
            log[today]["runtime_seconds"] += int(elapsed)
            # Close the last open session
            if log[today]["sessions"] and log[today]["sessions"][-1]["end"] is None:
                log[today]["sessions"][-1]["end"] = now.strftime("%H:%M:%S")
                log[today]["sessions"][-1].pop("last_seen", None)
        generator_session_start = None
    if running and log[today]["sessions"] and log[today]["sessions"][-1]["end"] is None:
        # The latest reading of the open session, for a restart (see above)
        log[today]["sessions"][-1]["last_seen"] = now.isoformat(timespec="seconds")

    generator_last_running = running

    # Keep only last 90 days
    sorted_dates = sorted(log.keys(), reverse=True)
    if len(sorted_dates) > 90:
        for old_date in sorted_dates[90:]:
            del log[old_date]

    save_generator_log(log)


def load_phase_stats():
    """Load phase statistics from file."""
    return load_json(PHASE_STATS_FILE, {})


def save_phase_stats(stats):
    """Save phase statistics to file."""
    atomic_write_json(PHASE_STATS_FILE, stats, indent=2)


def record_phase_sample(load_l1, load_l2, load_l3):
    with _data_lock:
        return _record_phase_sample(load_l1, load_l2, load_l3)


def _record_phase_sample(load_l1, load_l2, load_l3):
    """Record a phase power sample and accumulate energy."""
    global last_sample_time

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")

    # Load existing stats
    stats = load_phase_stats()

    # Initialize today's entry if needed
    if today not in stats:
        stats[today] = {
            "l1_wh": 0,
            "l2_wh": 0,
            "l3_wh": 0,
            "samples": 0,
            "l1_max": 0,
            "l2_max": 0,
            "l3_max": 0
        }

    # Calculate energy (Wh) from power (W) and time interval
    sample_time = time.monotonic()
    if last_sample_time is not None:
        interval = sample_time - last_sample_time
        # Only accumulate over a reasonable interval (no gap after downtime)
        if 0 < interval < PHASE_SAMPLE_MAX_GAP_SECONDS:
            interval_hours = interval / 3600
            stats[today]["l1_wh"] += load_l1 * interval_hours
            stats[today]["l2_wh"] += load_l2 * interval_hours
            stats[today]["l3_wh"] += load_l3 * interval_hours

    # Update max values
    stats[today]["l1_max"] = max(stats[today]["l1_max"], load_l1)
    stats[today]["l2_max"] = max(stats[today]["l2_max"], load_l2)
    stats[today]["l3_max"] = max(stats[today]["l3_max"], load_l3)
    stats[today]["samples"] += 1

    last_sample_time = sample_time

    # Keep only last 30 days
    sorted_dates = sorted(stats.keys(), reverse=True)
    if len(sorted_dates) > 30:
        for old_date in sorted_dates[30:]:
            del stats[old_date]

    save_phase_stats(stats)


@app.after_request
def add_no_cache_headers(response):
    """Prevent browsers from serving stale cached pages or API answers."""
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif response.content_type and "text/html" in response.content_type:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.route("/favicon.ico")
def favicon():
    """Browsers ask for /favicon.ico on their own (the page links the static icons)."""
    return app.send_static_file("favicon.ico")


@app.route("/")
def index():
    """Serve the dashboard page."""
    region = os.environ.get("AIR_ALERTS_REGION", "").strip()
    # Read by dashboard.js as window.DASHBOARD_CONFIG (embedded as JSON)
    title = os.environ.get("DASHBOARD_TITLE", "").strip()
    dashboard_config = {
        "title": title,   # empty: the translated default
        "locales": LOCALES,
        "defaultLanguage": DEFAULT_LANGUAGE,
        "firstRun": not _configured,
        # First run over an existing (invalid) .env: the wizard asks for the PIN first
        "settingsLocked": not _configured and settings_protected(),
        "alertsMapUrl": asset_version_url("data/ukraine-regions.json"),
        "alertsRegion": region if region in REGION_CODES else "",
        "alertsRegionCodes": list(REGION_CODES),
        # Inverter type list for the settings; with one driver it isn't shown
        "inverterTypes": [{"id": d.name, "name": d.display_name} for d in DRIVERS.values()]
                         if len(DRIVERS) > 1 else [],
    }
    return render_template("index.html", first_run=not _configured, dashboard_config=dashboard_config,
                           title=title)


@app.route("/api/data")
def get_data():
    """API endpoint to get current inverter data (cached)."""
    if not _configured:
        return jsonify({"error": "not configured", "config": None}), 503
    data = inverter_poller.data
    if not data:
        return jsonify({"error": "not yet available"}), 503
    config = inverter_config.to_dict()
    config["has_outage_schedule"] = outage_poller is not None
    data["config"] = config
    # How old the readings are (independent of the viewer's clock / time zone)
    age = InverterPoller._age(data)
    if age != float("inf"):
        data["data_age_seconds"] = max(0, int(age))
    return jsonify(data)


@app.route("/api/history")
def get_history():
    """Battery charge, solar and load over the last 24 hours, 5-minute averages (history.py)
    for the cards' sparklines. Empty lists until the first readings."""
    if power_history is None:
        return jsonify(PowerHistory().series())
    return jsonify(power_history.series())


@app.route("/api/phase-stats")
def get_phase_stats():
    """Get phase statistics."""
    stats = load_phase_stats()

    # Calculate daily totals and format for frontend
    result = []
    for day, data in sorted(stats.items(), reverse=True)[:14]:  # Last 14 days
        total_wh = data["l1_wh"] + data["l2_wh"] + data["l3_wh"]
        result.append({
            "date": day,
            "l1_kwh": round(data["l1_wh"] / 1000, 2),
            "l2_kwh": round(data["l2_wh"] / 1000, 2),
            "l3_kwh": round(data["l3_wh"] / 1000, 2),
            "total_kwh": round(total_wh / 1000, 2),
            "l1_max": data["l1_max"],
            "l2_max": data["l2_max"],
            "l3_max": data["l3_max"],
            "l1_pct": round(data["l1_wh"] / total_wh * 100, 1) if total_wh > 0 else 0,
            "l2_pct": round(data["l2_wh"] / total_wh * 100, 1) if total_wh > 0 else 0,
            "l3_pct": round(data["l3_wh"] / total_wh * 100, 1) if total_wh > 0 else 0,
        })

    return jsonify(result)


@app.route("/api/phase-stats/clear", methods=["POST"])
@require_settings_pin
def clear_phase_stats():
    """Clear phase statistics."""
    with _data_lock:
        save_phase_stats({})
    return jsonify({"status": "ok"})


@app.route("/api/outage_schedule")
def get_outage_schedule():
    """Get current outage schedule status from the poller."""
    if outage_poller is None:
        return jsonify({"status": "disabled"})
    status = outage_poller.get_outage_status()
    result = {"status": status["status"], "provider": outage_poller.provider.display_name}
    if status["status"] == "error":
        result["error"] = status["error"]
    elif status["status"] == "active":
        result["start_time"] = status["start_time"].isoformat()
        result["end_time"] = status["end_time"].isoformat()
        result["remaining_minutes"] = status["remaining_minutes"]
    elif status["status"] == "upcoming":
        result["upcoming_windows"] = [
            {"start": s.isoformat(), "end": e.isoformat()}
            for s, e in status["upcoming_windows"]
        ]
        if "electricity_start" in status:
            result["electricity_start"] = status["electricity_start"].isoformat()
    if "days" in status:
        result["days"] = [
            {
                "date": day["date"].isoformat(),
                "windows": [{"start": s.isoformat(), "end": e.isoformat()}
                            for s, e in day["windows"]],
            }
            for day in status["days"]
        ]
    return jsonify(result)


def current_outage_readiness():
    """Will the battery last through the next planned outage? (see outage_readiness.py)
    For the card and the Telegram bot's outage message alike."""
    if outage_poller is None or inverter_config is None or not inverter_config.has_battery:
        return {"status": "disabled"}
    status = outage_poller.get_outage_status()
    if status["status"] in ("unknown", "error"):
        return {"status": "no_schedule"}
    data = inverter_poller.data if inverter_poller else None
    if not data or data.get("error"):
        return {"status": "no_data"}
    return outage_readiness(data, status.get("days", []), datetime.now(),
                            data.get("load_average_w"), battery_capacity_wh(data))


@app.route("/api/outage-readiness")
def get_outage_readiness():
    return jsonify(current_outage_readiness())


@app.route("/api/smart-meter")
def get_smart_meter():
    """Latest smart meter readings (read-only)."""
    if smart_meter is None:
        return jsonify({"status": "disabled"})
    return jsonify(smart_meter.get_status())


@app.route("/api/meter-outages")
def get_meter_outages():
    """Grid outages the smart meter saw today and yesterday (meter_outages.py)."""
    if smart_meter is None:
        return jsonify({"status": "disabled"})
    return jsonify(smart_meter.get_outages())


def inverter_grid_online():
    """Does the inverter have the grid now (fresh readings only)? None: unknown."""
    data = inverter_poller.data if inverter_poller else None
    if not data or data.get("error") or InverterPoller._age(data) > 120:
        return None
    online = data.get("grid_online")
    return online if isinstance(online, bool) else None


@app.route("/api/alerts")
def get_alerts():
    """Air raid alert status per region (ISO 3166-2 codes)."""
    if alerts_poller is None:
        return jsonify({"status": "disabled"})
    return jsonify(alerts_poller.get_status())


@app.route("/api/weather")
def get_weather():
    """Get current weather conditions."""
    if not _configured:
        return jsonify({"error": "not configured"}), 503
    data = weather_poller.data
    if not data:
        return jsonify({"error": "not yet available"}), 503
    return jsonify(data)


@app.route("/api/generator")
def get_generator():
    """Get generator status and runtime data."""
    if not _configured or not inverter_config.has_generator:
        return jsonify({"enabled": False})

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    log = load_generator_log()

    # Current power from inverter poller
    inv_data = inverter_poller.data
    power = inv_data.get("generator_power", 0) if inv_data else 0
    running = power > 0

    # Today's runtime (account for currently-running session)
    today_entry = log.get(today, {"runtime_seconds": 0, "sessions": []})
    today_seconds = today_entry["runtime_seconds"]
    if running and generator_session_start:
        today_seconds += int((now - generator_session_start).total_seconds())
    today_hours = round(today_seconds / 3600, 2)

    # Monthly runtime
    month_prefix = now.strftime("%Y-%m")
    monthly_seconds = 0
    for day_key, day_data in log.items():
        if day_key.startswith(month_prefix):
            monthly_seconds += day_data.get("runtime_seconds", 0)
    if running and generator_session_start:
        monthly_seconds += int((now - generator_session_start).total_seconds())
    monthly_hours = round(monthly_seconds / 3600, 2)

    result = {
        "enabled": True,
        "running": running,
        "power": power,
        "today_runtime_hours": today_hours,
        "today_sessions": today_entry.get("sessions", []),
        "monthly_runtime_hours": monthly_hours,
    }

    # Fuel estimates
    if GENERATOR_FUEL_RATE > 0:
        result["fuel_rate"] = GENERATOR_FUEL_RATE
        result["fuel_today_liters"] = round(today_hours * GENERATOR_FUEL_RATE, 2)
        result["fuel_monthly_liters"] = round(monthly_hours * GENERATOR_FUEL_RATE, 2)
    else:
        result["fuel_rate"] = None
        result["fuel_today_liters"] = None
        result["fuel_monthly_liters"] = None

    # Oil change tracking
    if GENERATOR_OIL_CHANGE_DATE:
        result["oil_change_date"] = GENERATOR_OIL_CHANGE_DATE
        try:
            oil_date = datetime.strptime(GENERATOR_OIL_CHANGE_DATE, "%Y-%m-%d")
            # Sum all runtime hours since oil change date
            oil_hours = 0
            for day_key, day_data in log.items():
                if day_key >= GENERATOR_OIL_CHANGE_DATE:
                    oil_hours += day_data.get("runtime_seconds", 0) / 3600
            if running and generator_session_start:
                oil_hours += (now - generator_session_start).total_seconds() / 3600
            result["oil_change_hours_since"] = round(oil_hours, 1)
        except ValueError:
            result["oil_change_hours_since"] = None
    else:
        result["oil_change_date"] = None
        result["oil_change_hours_since"] = None

    return jsonify(result)


@app.route("/api/settings/verify-pin", methods=["POST"])
def verify_settings_pin():
    """Check the PIN entered to open the settings panel."""
    body = request.get_json(silent=True)
    ok, retry_after = check_settings_pin(body.get("pin") if isinstance(body, dict) else None)
    if not ok:
        return _pin_error(retry_after)
    return jsonify({"status": "ok"})


@app.route("/api/config/status")
def config_status():
    """Return whether the dashboard is configured (and which start of the server answers)."""
    return jsonify({"configured": _configured, "first_run": not _configured, "boot": BOOT_ID})


# Secrets in .env: the config editor only gets them masked, and a masked value
# sent back means "keep the saved one"
SECRET_CONFIG_KEYS = ("TELEGRAM_BOT_TOKEN", "ALERTS_IN_UA_TOKEN")
SECRET_MASK = "****"


def mask_secret(value):
    """"abcd****wxyz" (enough to recognise it); short values entirely hidden."""
    return value[:4] + SECRET_MASK + value[-4:] if len(value) > 12 else SECRET_MASK


@app.route("/api/config", methods=["GET"])
@require_settings_pin
def get_config():
    """Return current configuration values from .env (secrets masked)."""
    from setup import load_existing_env
    values, _ = load_existing_env()
    for key in SECRET_CONFIG_KEYS:
        if values.get(key):
            values[key] = mask_secret(values[key])
    return jsonify(values)


# systemd service deploy.sh installs (DEPLOY_SERVICE_NAME comes from .env, its EnvironmentFile)
DEFAULT_SERVICE_NAME = "inverter-dashboard"


def _restart_now():
    """Restart via systemd if possible, otherwise exit for the supervisor to restart us."""
    service = os.environ.get("DEPLOY_SERVICE_NAME", "").strip() or DEFAULT_SERVICE_NAME
    try:
        # -n: fail instead of waiting for a password prompt
        subprocess.run(["sudo", "-n", "systemctl", "restart", service],
                       check=True, capture_output=True, timeout=15)
        return  # systemd stops this process
    except Exception as e:
        logger.warning("systemctl restart unavailable (%s); exiting for the supervisor to restart", e)
    # Non-zero so systemd (Restart=always/on-failure) and Docker restart policies both restart
    os._exit(3)


def schedule_restart(delay=2):
    """Restart shortly, after the HTTP response has been sent."""
    threading.Timer(delay, _restart_now).start()


@app.route("/api/config", methods=["POST"])
@require_settings_pin
def save_config():
    """Save configuration values to .env and restart the service."""
    from setup import load_existing_env, write_env, validate_config
    new_values = request.get_json(silent=True)
    if not new_values or not isinstance(new_values, dict):
        return jsonify({"status": "error", "error": "Invalid request body"}), 400

    errors = validate_config(new_values)
    if errors:
        details = "; ".join(f"{key}: {msg}" for key, msg in sorted(errors.items()))
        return jsonify({"status": "error", "error": f"Invalid configuration — {details}",
                        "fields": errors}), 400
    new_values = {key: value.strip() for key, value in new_values.items()}

    # Load existing env to preserve extra lines
    existing, extra_lines = load_existing_env()

    # A masked secret is the saved one echoed back: keep it (never save the mask)
    for key in SECRET_CONFIG_KEYS:
        if SECRET_MASK in new_values.get(key, ""):
            if key in existing:
                new_values[key] = existing[key]
            else:
                del new_values[key]

    # Merge — new values override existing
    existing.update(new_values)
    try:
        problem = driver_class(existing.get("INVERTER_TYPE")).settings_problem(existing)
    except ValueError as e:
        problem = str(e)
    if problem:
        return jsonify({"status": "error", "error": f"Invalid configuration — {problem}"}), 400
    write_env(existing, extra_lines)
    # Names only, never values (tokens, passwords)
    logger.info("Configuration saved from the settings (%s); restarting", ", ".join(sorted(new_values)))

    # Schedule a service restart after 2 seconds
    schedule_restart()
    return jsonify({"status": "ok", "restarting": True, "boot": BOOT_ID})


@app.route("/api/config/reset", methods=["POST"])
@require_settings_pin
def reset_config():
    """Delete .env and restart the service to enter onboarding mode."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        # realpath: in Docker .env is a symlink into the data volume
        os.remove(os.path.realpath(env_path))
        logger.info("Deleted .env — entering first-run mode on restart")
    else:
        logger.info(".env not found — already in first-run mode")

    schedule_restart()
    return jsonify({"status": "ok", "restarting": True})


@app.route("/api/config/discover")
@require_settings_pin
def config_discover():
    """Discover inverters on the local network with retries (the selected driver's scan)."""
    try:
        driver = driver_class(request.args.get("type") or os.environ.get("INVERTER_TYPE"))
    except ValueError as e:
        return jsonify({"devices": [], "error": str(e)}), 400
    max_attempts = 3
    devices = []
    for attempt in range(1, max_attempts + 1):
        try:
            devices = driver.discover()
        except Exception:
            logger.exception("Discovery attempt %d failed", attempt)
        if devices:
            logger.info("Discovery found %d device(s) on attempt %d", len(devices), attempt)
            break
        if attempt < max_attempts:
            logger.info("Discovery attempt %d found nothing, retrying...", attempt)
            time.sleep(2)
    return jsonify({"devices": devices})


def start_telegram_bot():
    """Start the Telegram bot in a background thread if configured."""
    if os.environ.get("TELEGRAM_ENABLED", "true").lower() == "false":
        logging.info("Telegram bot disabled via TELEGRAM_ENABLED=false")
        return None

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    allowed = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
    is_public = os.environ.get("TELEGRAM_PUBLIC", "false").lower() in ("true", "1", "yes")

    if not token:
        logging.info("TELEGRAM_BOT_TOKEN not set, Telegram bot disabled")
        return None

    user_ids = set()
    for uid in allowed.split(","):
        uid = uid.strip()
        if uid.isascii() and uid.isdigit():
            user_ids.add(int(uid))

    state_file = os.environ.get("BOT_STATE_FILE") or data_path("bot_state.json")
    bot = TelegramBot(
        token=token, allowed_users=user_ids, inverter=inverter,
        outage_poller=outage_poller,
        state_file=state_file,
        grid_daily_log_file=GRID_DAILY_LOG_FILE,
        weather_poller=weather_poller,
        is_public=is_public,
        inverter_poller=inverter_poller,
        readiness=current_outage_readiness,
    )
    bot.start(inverter_interval=120)
    logging.info("Telegram bot started with %d allowed users (public=%s)", len(user_ids), is_public)
    return bot


def main():
    """Configure logging, start background services once, then serve the app."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    # Log startup configuration
    logger.info("=== Dashboard starting ===")
    try:
        driver = driver_class(os.environ.get("INVERTER_TYPE"))
        logger.info("Inverter driver: %s  %s", driver.name,
                    "  ".join(f"{key}={os.environ.get(key, '')}" for key in driver.required_settings))
    except ValueError as e:
        logger.warning("%s", e)

    if _configured:
        init_services()
        logger.info("Inverter config: %s", inverter_config.to_dict())
        logger.info("Generator: has_generator=%s fuel_rate=%s oil_change=%s",
                    inverter_config.has_generator, GENERATOR_FUEL_RATE, GENERATOR_OIL_CHANGE_DATE or "N/A")
        outage_prov_name = os.environ.get("OUTAGE_PROVIDER", "lvivoblenergo")
        outage_grp = os.environ.get("OUTAGE_GROUP")
        logger.info("OUTAGE_PROVIDER=%s  OUTAGE_GROUP=%s", outage_prov_name, outage_grp)
        logger.info("WEATHER coords: lat=%s lon=%s", WEATHER_LATITUDE, WEATHER_LONGITUDE)
        telegram_enabled = os.environ.get("TELEGRAM_ENABLED", "true").lower() != "false"
        telegram_token_set = bool(os.environ.get("TELEGRAM_BOT_TOKEN"))
        logger.info("TELEGRAM: enabled=%s token_set=%s", telegram_enabled, telegram_token_set)

        start_telegram_bot()
    else:
        logger.info("First-run mode — serving onboarding wizard")

    port = int(os.environ.get("PORT", 8080))
    if os.environ.get("DASHBOARD_DEV", "").lower() in ("1", "true", "yes"):
        # Local development only: Flask debugger, no reloader (a reloader would
        # start a second process and duplicate every background poller).
        app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)
    else:
        from waitress import serve
        logger.info("Serving on http://0.0.0.0:%d (waitress)", port)
        serve(app, host="0.0.0.0", port=port, threads=8)


if __name__ == "__main__":
    main()
