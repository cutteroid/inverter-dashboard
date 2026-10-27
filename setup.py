#!/usr/bin/env python3
"""Interactive setup script for the dashboard's .env configuration.

Guides the user through configuring inverter, weather, outage provider,
and Telegram settings. Preserves unrecognized .env lines on re-run.

Uses only Python stdlib — no external dependencies required.
"""

import os
import re
from datetime import datetime
import subprocess
import sys
import json

# ANSI color codes
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
RED = "\033[0;31m"
CYAN = "\033[0;36m"
BOLD = "\033[1m"
NC = "\033[0m"  # No Color

# Keys managed by this script (order matters for output)
MANAGED_KEYS = [
    "INVERTER_TYPE", "INVERTER_IP", "LOGGER_SERIAL", "INVERTER_PHASES",
    "WEATHER_LATITUDE", "WEATHER_LONGITUDE",
    "OUTAGE_PROVIDER", "OUTAGE_GROUP", "OUTAGE_REGION_ID", "OUTAGE_DSO_ID",
    "OUTAGE_DTEK_REGION",
    "INVERTER_HAS_GENERATOR", "GENERATOR_FUEL_RATE", "GENERATOR_OIL_CHANGE_DATE",
    "TELEGRAM_ENABLED", "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS",
    "TELEGRAM_PUBLIC",
    "AIR_ALERTS_PROVIDER", "AIR_ALERTS_REGION", "ALERTS_IN_UA_TOKEN",
    "DASHBOARD_TITLE",
]

MAX_LOGGER_SERIAL = 0xFFFFFFFF  # Solarman V5 packs the serial as an unsigned 32-bit int
# Inverter drivers (inverters.DRIVERS; this script uses the stdlib only, so it can't
# import them — tests keep both lists in sync)
INVERTER_TYPES = ("deye",)
OUTAGE_PROVIDERS = ("lvivoblenergo", "yasno", "dtek", "none")
INVERTER_PHASE_CHOICES = ("1", "3")
AIR_ALERTS_PROVIDERS = ("alerts_in_ua", "neptun", "mock", "none")
DTEK_REGIONS = ("kyiv-region", "kyiv", "dnipro", "odesa")
_BOOL_VALUES = ("true", "false", "1", "0", "yes", "no")
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")


def _digits(v):
    """Only 0-9: str.isdigit() also accepts e.g. "²", which int() rejects."""
    return v.isascii() and v.isdigit()


def _check_host(v):
    parts = v.split(".")
    if all(_digits(p) for p in parts):
        if len(parts) != 4 or any(int(p) > 255 for p in parts):
            return "must be a valid IPv4 address"
        return None
    return None if _HOSTNAME_RE.match(v) else "must be an IPv4 address or hostname"


def _check_int_range(lo, hi):
    def check(v):
        if not _digits(v) or not lo <= int(v) <= hi:
            return f"must be a whole number from {lo} to {hi}"
        return None
    return check


def _check_float_range(lo, hi):
    def check(v):
        try:
            f = float(v)
        except ValueError:
            return "must be a number"
        return None if lo <= f <= hi else f"must be between {lo} and {hi}"
    return check


def _check_choice(choices):
    return lambda v: None if v in choices else f"must be one of: {', '.join(choices)}"


def _check_regex(pattern, message):
    rx = re.compile(pattern)
    return lambda v: None if rx.match(v) else message


def _check_date(v):
    try:
        datetime.strptime(v, "%Y-%m-%d")
        return None
    except ValueError:
        return "must be a date in YYYY-MM-DD format"


def _check_title(v):
    """Dashboard name: up to 40 characters, none that would change how the .env
    line reads (# starts a comment, quotes and backslashes are parsed)."""
    if len(v) > 40:
        return "must be at most 40 characters"
    return "must not contain # \" ' or \\" if re.search(r"[#\"'\\]", v) else None


def _check_api_token(v):
    """alerts.in.ua token: one word of URL-safe characters (no spaces, quotes or #,
    which would break the .env line)."""
    if "****" in v:  # masked value echoed back by the config editor: keep existing
        return None
    return None if re.match(r"^[A-Za-z0-9._~:+/=-]{10,256}$", v) else "must be the token from alerts.in.ua"


def _check_token(v):
    if "****" in v:  # masked value echoed back by the config editor: keep existing
        return None
    return None if re.match(r"^\d+:[A-Za-z0-9_-]{20,}$", v) else "must look like 123456:ABC-DEF..."


# Validators per managed key; empty values are allowed for optional keys.
_VALIDATORS = {
    "INVERTER_TYPE": _check_choice(INVERTER_TYPES),   # empty = default driver
    "INVERTER_IP": _check_host,
    "LOGGER_SERIAL": _check_int_range(1, MAX_LOGGER_SERIAL),
    "INVERTER_PHASES": _check_choice(INVERTER_PHASE_CHOICES),  # empty = auto-detect
    "WEATHER_LATITUDE": _check_float_range(-90, 90),
    "WEATHER_LONGITUDE": _check_float_range(-180, 180),
    "OUTAGE_PROVIDER": _check_choice(OUTAGE_PROVIDERS),
    "OUTAGE_GROUP": _check_regex(r"^(GPV)?\d{1,2}\.\d{1,2}$", "must look like 4.2"),
    "OUTAGE_REGION_ID": _check_int_range(1, 100000),
    "OUTAGE_DSO_ID": _check_int_range(1, 100000),
    "OUTAGE_DTEK_REGION": _check_choice(DTEK_REGIONS),
    "INVERTER_HAS_GENERATOR": _check_choice(_BOOL_VALUES),
    "GENERATOR_FUEL_RATE": _check_float_range(0, 100),
    "GENERATOR_OIL_CHANGE_DATE": _check_date,
    "TELEGRAM_ENABLED": _check_choice(_BOOL_VALUES),
    "TELEGRAM_BOT_TOKEN": _check_token,
    "TELEGRAM_ALLOWED_USERS": _check_regex(r"^[\d,\s]*$", "must be comma-separated numeric user IDs"),
    "TELEGRAM_PUBLIC": _check_choice(_BOOL_VALUES),
    "AIR_ALERTS_PROVIDER": _check_choice(AIR_ALERTS_PROVIDERS),
    # ISO 3166-2 region shown on the alerts map; empty = whole Ukraine
    "AIR_ALERTS_REGION": _check_regex(r"^UA-\d{2}$", "must be a region code like UA-32"),
    "ALERTS_IN_UA_TOKEN": _check_api_token,
    # Name at the top of the page; empty = the translated default ("Home energy")
    "DASHBOARD_TITLE": _check_title,
}
_REQUIRED_KEYS = ("INVERTER_IP", "LOGGER_SERIAL")


def validate_config(values):
    """Validate config values (as submitted by the config editor).

    Returns {key: error message}; empty when everything is valid. Rejects unknown
    keys, non-string values and line breaks (which would inject extra .env lines).
    """
    errors = {}
    for key, value in values.items():
        if key not in _VALIDATORS:
            errors[key] = "unknown setting"
            continue
        if not isinstance(value, str):
            errors[key] = "must be a string"
            continue
        if any(c in value for c in "\r\n\0"):
            errors[key] = "must not contain line breaks"
            continue
        value = value.strip()
        if not value:
            if key in _REQUIRED_KEYS:
                errors[key] = "is required"
            continue
        message = _VALIDATORS[key](value)
        if message:
            errors[key] = message
    return errors


def ask(prompt, default=""):
    """Prompt for a value with optional default."""
    if default:
        raw = input(f"  {prompt} [{default}]: ").strip()
        return raw if raw else default
    else:
        return input(f"  {prompt}: ").strip()


def ask_choice(prompt, options, default=1):
    """Prompt the user to pick from a numbered list."""
    for i, (label, _) in enumerate(options, 1):
        print(f"  {i}) {label}")
    raw = input(f"  {prompt} [{default}]: ").strip()
    try:
        idx = int(raw) if raw else default
        if 1 <= idx <= len(options):
            return options[idx - 1][1]
    except ValueError:
        pass
    print(f"{RED}Invalid choice, using default.{NC}")
    return options[default - 1][1]


def ask_yn(prompt, default="n"):
    """Ask a yes/no question."""
    raw = input(f"  {prompt} (y/n) [{default}]: ").strip().lower()
    if not raw:
        raw = default
    return raw in ("y", "yes")


def load_existing_env(path=".env"):
    """Load existing .env file, returning (dict of known values, list of extra lines)."""
    values = {}
    extra_lines = []
    if not os.path.exists(path):
        return values, extra_lines
    with open(path, "r") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" in stripped:
                key, _, val = stripped.partition("=")
                key = key.strip()
                val = val.strip()
                if key in MANAGED_KEYS:
                    values[key] = val
                else:
                    extra_lines.append(stripped)
    return values, extra_lines


def try_discover():
    """Attempt to auto-discover inverters. Returns list of device dicts or []."""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "discover_inverter.py")
    if not os.path.exists(script):
        return []
    try:
        result = subprocess.run(
            [sys.executable, script, "--json"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception:
        pass
    return []


def section_inverter(defaults):
    """Inverter configuration section. Returns dict of values."""
    print(f"{YELLOW}Inverter Settings{NC}")

    # Try auto-discovery
    print("  Scanning local network for inverters...")
    print()
    devices = try_discover()

    if devices:
        print(f"  {GREEN}Found {len(devices)} device(s) with port 8899 open:{NC}")
        print()
        for i, dev in enumerate(devices, 1):
            model = dev.get("model") or "Unknown"
            print(f"    {CYAN}[{i}]{NC} IP: {BOLD}{dev['ip']}{NC}  |  Model: {model}")
        print(f"    {CYAN}[{len(devices) + 1}]{NC} Enter manually")
        print()

        raw = input(f"  Select device [1]: ").strip()
        try:
            choice = int(raw) if raw else 1
        except ValueError:
            choice = len(devices) + 1

        if 1 <= choice <= len(devices):
            ip = devices[choice - 1]["ip"]
            print(f"  {GREEN}Selected: {ip}{NC}")
            print()
            serial = ask("Logger serial number", defaults.get("LOGGER_SERIAL", ""))
        else:
            ip = ask("Inverter IP address", defaults.get("INVERTER_IP", ""))
            serial = ask("Logger serial number", defaults.get("LOGGER_SERIAL", ""))
    else:
        print(f"  {YELLOW}No inverters found on the local network.{NC}")
        print("  You can enter the details manually.")
        print()
        ip = ask("Inverter IP address", defaults.get("INVERTER_IP", ""))
        serial = ask("Logger serial number", defaults.get("LOGGER_SERIAL", ""))

    phases = ask("Phases: 1, 3, or empty to auto-detect", defaults.get("INVERTER_PHASES", ""))
    while phases not in ("",) + INVERTER_PHASE_CHOICES:
        phases = ask("Phases: 1, 3, or empty to auto-detect", "")

    print()
    # Only one driver so far: keep whatever type was set (empty = default)
    return {"INVERTER_TYPE": defaults.get("INVERTER_TYPE", ""), "INVERTER_IP": ip,
            "LOGGER_SERIAL": serial, "INVERTER_PHASES": phases}


def section_weather(defaults):
    """Weather configuration section. Returns dict of values."""
    print(f"{YELLOW}Weather Settings (Open-Meteo API){NC}")
    lat = ask("Latitude", defaults.get("WEATHER_LATITUDE", "50.4501"))
    lon = ask("Longitude", defaults.get("WEATHER_LONGITUDE", "30.5234"))
    print()
    return {"WEATHER_LATITUDE": lat, "WEATHER_LONGITUDE": lon}


def section_outage(defaults):
    """Outage provider configuration section. Returns dict of values."""
    print(f"{YELLOW}Outage Schedule Provider{NC}")

    prev = defaults.get("OUTAGE_PROVIDER", "lvivoblenergo")
    default_choice = {"lvivoblenergo": 1, "yasno": 2, "dtek": 3, "none": 4}.get(prev, 1)

    provider = ask_choice(
        "Choose",
        [("lvivoblenergo", "lvivoblenergo"), ("yasno", "yasno"),
         ("dtek (Kyiv oblast, Kyiv, Dnipro, Odesa)", "dtek"), ("none (disable)", "none")],
        default=default_choice,
    )

    result = {"OUTAGE_PROVIDER": provider}

    if provider == "lvivoblenergo":
        result["OUTAGE_GROUP"] = ask("Outage group (e.g. 1.1)", defaults.get("OUTAGE_GROUP", "1.1"))
    elif provider == "yasno":
        result["OUTAGE_REGION_ID"] = ask("YASNO region ID (e.g. 25 = Kyiv)", defaults.get("OUTAGE_REGION_ID", "25"))
        result["OUTAGE_DSO_ID"] = ask("YASNO DSO ID (e.g. 902 = DTEK Kyiv)", defaults.get("OUTAGE_DSO_ID", "902"))
        result["OUTAGE_GROUP"] = ask("Queue/group number (e.g. 2.1)", defaults.get("OUTAGE_GROUP", "2.1"))
    elif provider == "dtek":
        result["OUTAGE_DTEK_REGION"] = ask(
            "DTEK region (kyiv-region, kyiv, dnipro, odesa)",
            defaults.get("OUTAGE_DTEK_REGION", "kyiv-region"),
        )
        result["OUTAGE_GROUP"] = ask("Queue/group number (e.g. 4.2)", defaults.get("OUTAGE_GROUP", "1.1"))

    print()
    return result


def section_generator(defaults):
    """Generator configuration section. Returns dict of values."""
    print(f"{YELLOW}Generator (optional){NC}")

    prev = defaults.get("INVERTER_HAS_GENERATOR", "false")
    default_yn = "y" if prev.lower() in ("true", "1", "yes") else "n"

    if not ask_yn("Has generator connected to GEN/GRID2 port?", default=default_yn):
        print()
        return {"INVERTER_HAS_GENERATOR": "false"}

    result = {"INVERTER_HAS_GENERATOR": "true"}

    fuel = ask("Fuel consumption rate in L/hour (0 to skip)", defaults.get("GENERATOR_FUEL_RATE", "0"))
    if fuel and float(fuel) > 0:
        result["GENERATOR_FUEL_RATE"] = fuel

    oil_date = ask("Last oil change date YYYY-MM-DD (empty to skip)", defaults.get("GENERATOR_OIL_CHANGE_DATE", ""))
    if oil_date:
        result["GENERATOR_OIL_CHANGE_DATE"] = oil_date

    print()
    return result


def section_telegram(defaults):
    """Telegram bot configuration section. Returns dict of values."""
    print(f"{YELLOW}Telegram Bot (optional){NC}")

    prev_enabled = defaults.get("TELEGRAM_ENABLED", "false")
    default_yn = "y" if prev_enabled.lower() in ("true", "1", "yes") else "n"

    if not ask_yn("Enable Telegram bot?", default=default_yn):
        print()
        return {"TELEGRAM_ENABLED": "false"}

    result = {"TELEGRAM_ENABLED": "true"}
    prev_token = defaults.get("TELEGRAM_BOT_TOKEN", "")
    if prev_token and prev_token != "your-bot-token-here":
        masked = prev_token[:4] + "****" + prev_token[-4:]
        print(f"  Existing bot token: {masked}")
        if ask_yn("Keep existing bot token?", default="y"):
            result["TELEGRAM_BOT_TOKEN"] = prev_token
        else:
            result["TELEGRAM_BOT_TOKEN"] = ask("Bot token", "")
    else:
        result["TELEGRAM_BOT_TOKEN"] = ask("Bot token", "")

    prev_public = defaults.get("TELEGRAM_PUBLIC", "false")
    default_pub = "y" if prev_public.lower() in ("true", "1", "yes") else "n"

    if ask_yn("Public mode? (any user can query the bot)", default=default_pub):
        result["TELEGRAM_PUBLIC"] = "true"
        users = ask("Allowed user IDs for broadcasts (comma-separated, optional)",
                     defaults.get("TELEGRAM_ALLOWED_USERS", ""))
        result["TELEGRAM_ALLOWED_USERS"] = users
    else:
        result["TELEGRAM_PUBLIC"] = "false"
        result["TELEGRAM_ALLOWED_USERS"] = ask(
            "Allowed user IDs (comma-separated)",
            defaults.get("TELEGRAM_ALLOWED_USERS", ""),
        )

    print()
    return result


def write_env(values, extra_lines, path=".env"):
    """Write .env file from collected values and preserved extra lines."""
    lines = []

    lines.append("# Inverter")
    if values.get("INVERTER_TYPE"):
        lines.append(f"INVERTER_TYPE={values['INVERTER_TYPE']}")
    lines.append(f"INVERTER_IP={values.get('INVERTER_IP', '')}")
    lines.append(f"LOGGER_SERIAL={values.get('LOGGER_SERIAL', '')}")
    # Not written = auto-detected at startup
    if values.get("INVERTER_PHASES") in INVERTER_PHASE_CHOICES:
        lines.append(f"INVERTER_PHASES={values['INVERTER_PHASES']}")
    lines.append("")

    lines.append("# Weather (coordinates for Open-Meteo API)")
    # "or": an empty value (allowed, the field is optional) means the default too
    lines.append(f"WEATHER_LATITUDE={values.get('WEATHER_LATITUDE') or '50.4501'}")
    lines.append(f"WEATHER_LONGITUDE={values.get('WEATHER_LONGITUDE') or '30.5234'}")
    lines.append("")

    lines.append("# Outage Schedule Provider")
    provider = values.get("OUTAGE_PROVIDER", "lvivoblenergo")
    lines.append(f"OUTAGE_PROVIDER={provider}")
    if provider == "lvivoblenergo" and "OUTAGE_GROUP" in values:
        lines.append(f"OUTAGE_GROUP={values['OUTAGE_GROUP']}")
    elif provider == "yasno":
        if "OUTAGE_REGION_ID" in values:
            lines.append(f"OUTAGE_REGION_ID={values['OUTAGE_REGION_ID']}")
        if "OUTAGE_DSO_ID" in values:
            lines.append(f"OUTAGE_DSO_ID={values['OUTAGE_DSO_ID']}")
        if "OUTAGE_GROUP" in values:
            lines.append(f"OUTAGE_GROUP={values['OUTAGE_GROUP']}")
    elif provider == "dtek":
        if "OUTAGE_DTEK_REGION" in values:
            lines.append(f"OUTAGE_DTEK_REGION={values['OUTAGE_DTEK_REGION']}")
        if "OUTAGE_GROUP" in values:
            lines.append(f"OUTAGE_GROUP={values['OUTAGE_GROUP']}")
    lines.append("")

    # Dashboard name (only when set, so the default follows the language)
    if values.get("DASHBOARD_TITLE"):
        lines.append("# Dashboard")
        lines.append(f"DASHBOARD_TITLE={values['DASHBOARD_TITLE']}")
        lines.append("")

    # Generator: "no" is written too, or auto-detect would decide (and keeps a
    # generator it found once); not set at all: auto-detect
    gen_enabled = values.get("INVERTER_HAS_GENERATOR")
    if gen_enabled is not None:
        lines.append("# Generator")
        if gen_enabled.lower() in ("true", "1", "yes"):
            lines.append("INVERTER_HAS_GENERATOR=true")
            if "GENERATOR_FUEL_RATE" in values:
                lines.append(f"GENERATOR_FUEL_RATE={values['GENERATOR_FUEL_RATE']}")
            if "GENERATOR_OIL_CHANGE_DATE" in values:
                lines.append(f"GENERATOR_OIL_CHANGE_DATE={values['GENERATOR_OIL_CHANGE_DATE']}")
        else:
            lines.append("INVERTER_HAS_GENERATOR=false")
        lines.append("")

    lines.append("# Telegram Bot")
    telegram_enabled = values.get("TELEGRAM_ENABLED", "false")
    lines.append(f"TELEGRAM_ENABLED={telegram_enabled}")
    # Kept while the bot is switched off, so switching it back on doesn't need them again
    for key, default in (("TELEGRAM_BOT_TOKEN", ""), ("TELEGRAM_ALLOWED_USERS", ""), ("TELEGRAM_PUBLIC", "false")):
        if telegram_enabled.lower() in ("true", "1", "yes") or values.get(key):
            lines.append(f"{key}={values.get(key) or default}")

    # Air raid alerts (only what was set, so defaults stay defaults)
    alerts_lines = [f"{key}={values[key]}"
                    for key in ("AIR_ALERTS_PROVIDER", "AIR_ALERTS_REGION", "ALERTS_IN_UA_TOKEN")
                    if values.get(key)]
    if alerts_lines:
        lines.append("")
        lines.append("# Air raid alerts")
        lines.extend(alerts_lines)

    # Preserve unrecognized lines (e.g. DEPLOY_* from deploy.sh)
    if extra_lines:
        lines.append("")
        lines.append("# Additional settings")
        lines.extend(extra_lines)

    lines.append("")  # trailing newline
    _write_private_atomically(path, "\n".join(lines))


def _write_private_atomically(path, text):
    """Replace path with text: a temp file next to the real file (in Docker .env
    is a link into data/), forced to disk, then renamed over it, so a power cut
    leaves the old or the new file, never an empty one. Readable by its owner
    only: it holds the bot and alerts tokens and the settings PIN."""
    real = os.path.realpath(path)
    tmp = f"{real}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)   # O_CREAT's mode only applies to a new file
        os.replace(tmp, real)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def main():
    print()
    print(f"{CYAN}{BOLD}========================================{NC}")
    print(f"{CYAN}{BOLD}  Dashboard — Setup{NC}")
    print(f"{CYAN}{BOLD}========================================{NC}")
    print()

    defaults, extra_lines = load_existing_env()

    if defaults:
        print("Existing .env found — values shown as defaults in [brackets].")
    else:
        print("No .env file found. Let's configure your dashboard.")
    print("Press Enter to accept defaults.")
    print()

    values = {}
    values.update(section_inverter(defaults))
    values.update(section_weather(defaults))
    values.update(section_outage(defaults))
    values.update(section_generator(defaults))
    values.update(section_telegram(defaults))

    # Summary
    print(f"{CYAN}Summary:{NC}")
    print(f"  Inverter:  {values.get('INVERTER_IP', '')} (serial: {values.get('LOGGER_SERIAL', '')})")
    print(f"  Weather:   {values.get('WEATHER_LATITUDE', '')}, {values.get('WEATHER_LONGITUDE', '')}")
    print(f"  Outage:    {values.get('OUTAGE_PROVIDER', '')}")
    gen = "yes" if values.get("INVERTER_HAS_GENERATOR", "false").lower() in ("true", "1", "yes") else "no"
    print(f"  Generator: {gen}")
    tg = "enabled" if values.get("TELEGRAM_ENABLED", "false").lower() in ("true", "1", "yes") else "disabled"
    if tg == "enabled" and values.get("TELEGRAM_PUBLIC", "false").lower() in ("true", "1", "yes"):
        tg += " (public)"
    print(f"  Telegram:  {tg}")
    print()

    if not ask_yn("Write .env file?", default="y"):
        print("Setup cancelled.")
        sys.exit(0)

    write_env(values, extra_lines)
    print(f"{GREEN}.env file created successfully!{NC}")
    print()


if __name__ == "__main__":
    main()
