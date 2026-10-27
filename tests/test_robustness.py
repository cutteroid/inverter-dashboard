"""Edge cases from the code review: clock changes, restarts, partial failures."""
import json
import os
import stat
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

import app
from inverters import InverterConfig


# ----- auto-detect keeps what it can't confirm -----

def test_failed_detection_keeps_the_config():
    config = InverterConfig(phases=3, family="hybrid_3p_lv", device_type=0x0500)
    inv = MagicMock()
    inv.detect_config.side_effect = RuntimeError("the inverter didn't answer the phase detection reads")
    app.refresh_inverter_config(inv, config)
    assert config.phases == 3 and config.family == "hybrid_3p_lv"
    assert not os.path.exists(app.INVERTER_CONFIG_FILE)


def test_unknown_model_this_time_keeps_the_known_one():
    config = InverterConfig(phases=3, family="hybrid_3p_hv", device_type=0x0600, supported=True)
    inv = MagicMock()
    inv.detect_config.return_value = InverterConfig(phases=3, family="unknown", device_type=None)
    app.refresh_inverter_config(inv, config)
    assert config.family == "hybrid_3p_hv" and config.device_type == 0x0600
    with open(app.INVERTER_CONFIG_FILE) as f:
        assert json.load(f)["family"] == "hybrid_3p_hv"


def test_device_type_check_failure_doesnt_raise():
    config = InverterConfig(family="hybrid_3p_lv")
    inv = MagicMock()
    inv.read_device_type.side_effect = OSError("boom")
    app.refresh_device_type(inv, config)
    assert config.family == "hybrid_3p_lv"


# ----- settings PIN -----

@pytest.fixture
def first_run_client(monkeypatch, tmp_path):
    """Settings invalid (setup wizard), PIN-less request."""
    monkeypatch.setattr(app, "_configured", False)
    monkeypatch.setattr(app, "ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.delenv("SETTINGS_PIN", raising=False)
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        yield c, tmp_path / ".env"


def test_true_first_start_is_open(first_run_client):
    c, _ = first_run_client
    assert c.get("/api/config").status_code == 200
    assert c.get("/").status_code == 200


def test_existing_invalid_config_still_needs_the_pin(first_run_client):
    c, env = first_run_client
    env.write_text("INVERTER_IP=\nTELEGRAM_BOT_TOKEN=123:secret\n")
    resp = c.get("/api/config")
    assert resp.status_code == 401 and resp.get_json()["pin_required"] is True
    assert c.post("/api/config/reset").status_code == 401
    assert c.get("/api/config", headers={"X-Settings-Pin": "0000"}).status_code == 200


def test_page_tells_the_wizard_to_ask_for_the_pin(first_run_client):
    from tests.conftest import page_config
    c, env = first_run_client
    assert page_config(c.get("/").get_data(as_text=True))["settingsLocked"] is False
    env.write_text("INVERTER_IP=\n")
    assert page_config(c.get("/").get_data(as_text=True))["settingsLocked"] is True


@pytest.mark.parametrize("body", ["[1]", '"x"', "null", "not json"])
def test_verify_pin_with_a_strange_body_is_a_wrong_pin(body, monkeypatch):
    monkeypatch.setattr(app, "_pin_failures", 0)
    monkeypatch.setattr(app, "_pin_locked_until", 0.0)
    with app.app.test_client() as c:
        resp = c.post("/api/settings/verify-pin", data=body, content_type="application/json")
    assert resp.status_code == 401


# ----- .env written safely -----

def _write(tmp_path, values, path=None):
    from setup import write_env
    path = path or str(tmp_path / ".env")
    write_env(values, [], path=path)
    return path


def test_env_file_is_private_and_replaced_whole(tmp_path):
    path = _write(tmp_path, {"INVERTER_IP": "10.0.0.5", "LOGGER_SERIAL": "123"})
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert "INVERTER_IP=10.0.0.5" in open(path).read()
    assert [p.name for p in tmp_path.iterdir()] == [".env"]   # no temp file left


def test_env_link_is_followed(tmp_path):
    """In Docker .env is a link into data/: the file there is replaced, the link stays."""
    (tmp_path / "data").mkdir()
    link = tmp_path / ".env"
    link.symlink_to(tmp_path / "data" / ".env")
    _write(tmp_path, {"INVERTER_IP": "10.0.0.6", "LOGGER_SERIAL": "1"}, path=str(link))
    assert link.is_symlink()
    assert "INVERTER_IP=10.0.0.6" in (tmp_path / "data" / ".env").read_text()


def test_bot_switched_off_keeps_its_token(tmp_path):
    path = _write(tmp_path, {"INVERTER_IP": "1.2.3.4", "LOGGER_SERIAL": "1", "TELEGRAM_ENABLED": "false",
                             "TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_ALLOWED_USERS": "42"})
    text = open(path).read()
    assert "TELEGRAM_ENABLED=false" in text and "TELEGRAM_BOT_TOKEN=123:abc" in text
    assert "TELEGRAM_ALLOWED_USERS=42" in text


def test_empty_coordinates_get_the_defaults(tmp_path):
    path = _write(tmp_path, {"INVERTER_IP": "1.2.3.4", "LOGGER_SERIAL": "1",
                             "WEATHER_LATITUDE": "", "WEATHER_LONGITUDE": ""})
    text = open(path).read()
    assert "WEATHER_LATITUDE=50.4501" in text and "WEATHER_LONGITUDE=30.5234" in text


@pytest.mark.parametrize("value", ["²", "١٢٣", "1²"])
def test_non_ascii_digits_are_rejected_not_crashing(value):
    from setup import validate_config
    errors = validate_config({"INVERTER_IP": "10.0.0.1", "LOGGER_SERIAL": value})
    assert "LOGGER_SERIAL" in errors
    assert "INVERTER_IP" in validate_config({"INVERTER_IP": f"10.0.0.{value}", "LOGGER_SERIAL": "1"})


# ----- clocks -----

def test_phase_energy_never_goes_negative(tmp_path, monkeypatch):
    """The clocks going back must not subtract energy (monotonic interval)."""
    monkeypatch.setattr(app, "last_sample_time", time.monotonic() + 3600)   # "later" than now
    app.record_phase_sample(500, 500, 500)
    with open(app.PHASE_STATS_FILE) as f:
        day = next(iter(json.load(f).values()))
    assert day["l1_wh"] == 0


def test_inverter_data_age_uses_the_epoch_timestamp():
    data = {"last_updated": datetime.now().isoformat(), "timestamp": time.time() - 400}
    assert 399 < app.InverterPoller._age(data) < 402
    # Older cache files without the timestamp still work
    data = {"last_updated": (datetime.now() - timedelta(seconds=30)).isoformat()}
    assert 29 < app.InverterPoller._age(data) < 32


def test_weather_retries_soon_until_it_has_data(monkeypatch):
    poller = app.WeatherPoller()
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        raise StopIteration

    monkeypatch.setattr(poller, "_fetch", lambda: None)
    monkeypatch.setattr(app.time, "sleep", fake_sleep)
    with pytest.raises(StopIteration):
        poller._run()
    poller._cache = {"temperature": 5}
    with pytest.raises(StopIteration):
        poller._run()
    assert sleeps == [app.WeatherPoller.RETRY_SECONDS, 900]


# ----- generator: downtime isn't runtime -----

@pytest.fixture
def generator(monkeypatch):
    monkeypatch.setattr(app, "generator_last_running", None)
    monkeypatch.setattr(app, "generator_session_start", None)


def _gen_log():
    with open(app.GENERATOR_LOG_FILE) as f:
        return json.load(f)


def test_open_session_stores_its_last_reading(generator):
    app.track_generator_runtime(1000, now=datetime(2026, 10, 17, 10, 0, 0))
    app.track_generator_runtime(1000, now=datetime(2026, 10, 17, 10, 0, 30))   # not written: < 60 s
    assert _gen_log()["2026-10-17"]["sessions"][0]["last_seen"] == "2026-10-17T10:00:00"
    app.track_generator_runtime(1000, now=datetime(2026, 10, 17, 10, 1, 0))
    assert _gen_log()["2026-10-17"]["sessions"] == [
        {"start": "10:00:00", "end": None, "last_seen": "2026-10-17T10:01:00"}]


def test_app_down_session_ends_at_its_last_reading(generator):
    """Generator ran 10:00-11:00 as far as we know; the app was down 11:00-19:00."""
    with open(app.GENERATOR_LOG_FILE, "w") as f:
        json.dump({"2026-10-17": {"runtime_seconds": 0, "sessions": [
            {"start": "10:00:00", "end": None, "last_seen": "2026-10-17T11:00:00"}]}}, f)
    app.track_generator_runtime(0, now=datetime(2026, 10, 17, 19, 0, 0))
    day = _gen_log()["2026-10-17"]
    assert day["runtime_seconds"] == 3600
    assert day["sessions"] == [{"start": "10:00:00", "end": "11:00:00"}]


def test_app_down_but_generator_running_starts_a_new_session(generator):
    with open(app.GENERATOR_LOG_FILE, "w") as f:
        json.dump({"2026-10-17": {"runtime_seconds": 0, "sessions": [
            {"start": "10:00:00", "end": None, "last_seen": "2026-10-17T11:00:00"}]}}, f)
    app.track_generator_runtime(1000, now=datetime(2026, 10, 17, 19, 0, 0))
    day = _gen_log()["2026-10-17"]
    assert day["runtime_seconds"] == 3600
    assert day["sessions"][0] == {"start": "10:00:00", "end": "11:00:00"}
    assert day["sessions"][1]["start"] == "19:00:00" and day["sessions"][1]["end"] is None


def test_quick_restart_resumes_the_session(generator):
    with open(app.GENERATOR_LOG_FILE, "w") as f:
        json.dump({"2026-10-17": {"runtime_seconds": 0, "sessions": [
            {"start": "10:00:00", "end": None, "last_seen": "2026-10-17T10:58:00"}]}}, f)
    app.track_generator_runtime(1000, now=datetime(2026, 10, 17, 11, 0, 0))
    app.track_generator_runtime(0, now=datetime(2026, 10, 17, 11, 30, 0))
    day = _gen_log()["2026-10-17"]
    assert day["sessions"] == [{"start": "10:00:00", "end": "11:30:00"}]
    assert day["runtime_seconds"] == 5400


# ----- grid daily import follows the inverter's counter reset -----

def _grid(readings):
    for when, kwh in readings:
        app.record_grid_daily_import(kwh, now=when)
    with open(app.GRID_DAILY_LOG_FILE) as f:
        return json.load(f)


def test_inverter_resets_before_our_midnight():
    log = _grid([(datetime(2026, 10, 26, 23, 50), 12.3),
                 (datetime(2026, 10, 26, 23, 56), 0.0),     # inverter's midnight
                 (datetime(2026, 10, 26, 23, 59), 0.1),
                 (datetime(2026, 10, 27, 0, 30), 0.4)])
    assert log == {"2026-10-26": 12.3, "2026-10-27": 0.4}


def test_inverter_resets_after_our_midnight():
    log = _grid([(datetime(2026, 10, 26, 23, 50), 12.3),
                 (datetime(2026, 10, 27, 0, 2), 12.4),      # still yesterday's counter
                 (datetime(2026, 10, 27, 0, 4), 0.0),       # inverter's midnight
                 (datetime(2026, 10, 27, 2, 0), 0.6)])
    assert log == {"2026-10-26": 12.4, "2026-10-27": 0.6}


def test_glitch_during_the_day_is_skipped():
    log = _grid([(datetime(2026, 10, 27, 14, 0), 5.0),
                 (datetime(2026, 10, 27, 14, 1), 0.0),
                 (datetime(2026, 10, 27, 14, 2), 5.1)])
    assert log == {"2026-10-27": 5.1}


def test_no_import_all_day_still_moves_to_the_new_date():
    log = _grid([(datetime(2026, 10, 26, 22, 0), 0.0),
                 (datetime(2026, 10, 27, 3, 0), 0.0)])
    assert log == {"2026-10-26": 0.0, "2026-10-27": 0.0}


def test_start_just_before_midnight_after_the_reset_keeps_the_day_total():
    with open(app.GRID_DAILY_LOG_FILE, "w") as f:
        json.dump({"2026-10-26": 12.3}, f)
    log = _grid([(datetime(2026, 10, 26, 23, 58), 0.1)])   # counter already reset
    assert log == {"2026-10-26": 12.3}


# ----- atomic writes from several threads -----

def test_atomic_writes_use_a_temp_file_per_thread(tmp_path):
    import threading
    from storage import atomic_write_json
    path = str(tmp_path / "x.json")
    errors = []

    def write(n):
        try:
            for _ in range(50):
                atomic_write_json(path, {"n": n})
        except Exception as e:   # pragma: no cover - the failure being tested
            errors.append(e)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert json.load(open(path))["n"] in range(4)
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


# ----- meter outages keep their UTC offset -----

def test_outage_over_the_clock_change_keeps_its_length(tmp_path):
    from datetime import timezone
    from meter_outages import OutageLog
    summer, winter = timezone(timedelta(hours=3)), timezone(timedelta(hours=2))
    now = [datetime(2026, 10, 25, 3, 50, tzinfo=summer)]
    log = OutageLog(str(tmp_path / "o.json"), clock=lambda: now[0])
    log.update("total")
    now[0] = datetime(2026, 10, 25, 3, 10, tzinfo=winter)   # 20 minutes later, clocks went back
    log.update("ok")
    (outage,) = log.snapshot()
    start, end = (datetime.fromisoformat(outage[k]) for k in ("start", "end"))
    assert end - start == timedelta(minutes=20)


def test_old_entries_without_offset_are_read_as_local_time(tmp_path):
    from meter_outages import OutageLog, local_now
    path = tmp_path / "o.json"
    yesterday = (datetime.now() - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    path.write_text(json.dumps({"events": [{"type": "total", "start": yesterday.isoformat(),
                                            "end": (yesterday + timedelta(hours=1)).isoformat(),
                                            "phases": [], "cut": []}], "open": None}))
    (outage,) = OutageLog(str(path), clock=local_now).snapshot()
    assert datetime.fromisoformat(outage["start"]).tzinfo is not None



# ----- fewer disk writes (NAS load) -----

def test_phase_stats_are_written_every_few_minutes(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(app, "_phase_stats", {"path": None, "stats": None, "saved_at": None})
    app.record_phase_sample(100, 100, 100)          # first: written
    first = os.path.getmtime(app.PHASE_STATS_FILE), open(app.PHASE_STATS_FILE).read()
    clock[0] += 30
    app.record_phase_sample(100, 100, 100)          # 30 s later: memory only
    assert open(app.PHASE_STATS_FILE).read() == first[1]
    assert next(iter(app.load_phase_stats().values()))["samples"] == 2
    clock[0] += app.PHASE_STATS_SAVE_SECONDS
    app.record_phase_sample(100, 100, 100)          # 5 min later: written
    assert next(iter(json.load(open(app.PHASE_STATS_FILE)).values()))["samples"] == 3


def test_grid_log_written_only_when_it_changes(monkeypatch):
    saves, save = [], app.save_grid_daily_log
    monkeypatch.setattr(app, "save_grid_daily_log", lambda log: (saves.append(dict(log)), save(log)))
    when = datetime(2026, 10, 27, 14, 0)
    for minute, kwh in ((0, 3.2), (1, 3.2), (2, 3.2), (3, 3.3)):
        app.record_grid_daily_import(kwh, now=when + timedelta(minutes=minute))
    assert len(saves) == 2


def test_generator_log_not_written_while_it_stays_off(monkeypatch):
    monkeypatch.setattr(app, "generator_last_running", None)
    monkeypatch.setattr(app, "generator_session_start", None)
    saves, save = [], app.save_generator_log
    monkeypatch.setattr(app, "save_generator_log", lambda log: (saves.append(1), save(log)))
    for minute in range(5):
        app.track_generator_runtime(0, now=datetime(2026, 10, 17, 10, minute))
    assert len(saves) == 1   # today's empty entry, once


def test_inverter_cache_written_every_two_minutes(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: clock[0])
    poller = app.InverterPoller(MagicMock(), cache_file=app.INVERTER_CACHE_FILE)
    writes = []
    monkeypatch.setattr(app, "atomic_write_json", lambda path, data, **kw: writes.append(path))
    for _ in range(5):                               # 5 polls, 30 s apart
        poller._save_cache()
        clock[0] += 30
    assert len(writes) == 2                          # at 0 s and 120 s
