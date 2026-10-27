"""Tests for the smart meter's grid outage log (meter_outages.py, SmartMeter)."""
import json
from datetime import datetime, timedelta

import pytest

from meter_outages import OutageLog
from smart_meter import OFFLINE_GRACE, SmartMeter


class Clock:
    def __init__(self, start=datetime(2026, 10, 26, 12, 0, 0)):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
def clock():
    return Clock()


def volts(meter, v1, v2=None, v3=None):
    if v2 is None:
        meter.handle_message("buckler/M1/data/V", str(v1))
    else:
        for i, v in enumerate((v1, v2, v3), start=1):
            meter.handle_message(f"buckler/M1/data/V{i}", str(v))


def test_partial_outage_from_a_phase_without_voltage(clock):
    m = SmartMeter(daily_file=None, clock=clock)
    volts(m, 230, 231, 229)
    clock.advance(minutes=1)
    volts(m, 230, 0.3, 229)
    clock.advance(minutes=20)
    volts(m, 230, 231, 229)
    assert m.get_outages()["outages"] == [{
        "type": "partial", "start": "2026-10-26T12:01:00", "end": "2026-10-26T12:21:00",
        "phases": [2], "cut": []}]


def test_partial_outage_adds_up_phases_and_turns_total(clock):
    m = SmartMeter(daily_file=None, clock=clock)
    volts(m, 230, 0, 229)
    clock.advance(minutes=5)
    volts(m, 230, 0, 0)           # L3 goes too: the same partial outage
    clock.advance(minutes=5)
    volts(m, 0, 0, 0)             # all: total
    clock.advance(minutes=30)
    volts(m, 230, 231, 229)
    out = m.get_outages()["outages"]
    assert [(e["type"], e["start"][11:16], e["end"][11:16], e["phases"]) for e in out] == [
        ("total", "12:10", "12:40", []), ("partial", "12:00", "12:10", [2, 3])]


def test_phase_cut_by_the_meter_relay(clock):
    """Three-phase meter with r1..r3: a relay off is a cut phase (its voltage is still there)."""
    m = SmartMeter(daily_file=None, clock=clock)
    volts(m, 230, 256, 229)
    m.handle_message("buckler/M1/relay/r1", "100")
    m.handle_message("buckler/M1/relay/r2", "100")
    m.handle_message("buckler/M1/relay/r3", "100")
    clock.advance(minutes=1)
    m.handle_message("buckler/M1/relay/r2", "0")
    out = m.get_outages()["outages"]
    assert out == [{"type": "partial", "start": "2026-10-26T12:01:00", "end": None, "phases": [2], "cut": [2]}]


def test_six_relays_are_not_per_phase(clock):
    m = SmartMeter(daily_file=None, clock=clock)
    m.handle_message("buckler/M1/dataJson", json.dumps({"V": [230, 231, 229], "relay": [0, 100, 100, 100, 100, 100]}))
    assert m.get_outages()["outages"] == []


def test_offline_meter_is_a_total_outage_from_when_it_went_offline(clock):
    m = SmartMeter(daily_file=None, clock=clock)
    volts(m, 230, 231, 229)
    clock.advance(minutes=2)
    m.handle_message("buckler/M1/status", "offline")
    clock.advance(seconds=30)
    m.check_offline()
    assert m.get_outages()["outages"] == []         # within the grace: a reconnect, maybe
    clock.advance(seconds=OFFLINE_GRACE.total_seconds())
    out = m.get_outages()["outages"]
    assert out == [{"type": "total", "start": "2026-10-26T12:02:00", "end": None, "phases": [], "cut": []}]
    clock.advance(minutes=40)
    m.handle_message("buckler/M1/data/P1", "120")   # back: any data ends it
    assert m.get_outages()["outages"][0]["end"] == "2026-10-26T12:44:00"


def test_offline_meter_while_the_inverter_has_the_grid_is_not_an_outage(clock):
    m = SmartMeter(daily_file=None, clock=clock)
    m.grid_online_hint = lambda: True
    volts(m, 230, 231, 229)
    m.handle_message("buckler/M1/status", "offline")
    clock.advance(minutes=10)
    assert m.get_outages()["outages"] == []
    m.grid_online_hint = lambda: None                # unknown: the meter decides
    assert m.get_outages()["outages"][0]["type"] == "total"


def test_single_phase(clock):
    m = SmartMeter(daily_file=None, clock=clock)
    volts(m, 230)
    clock.advance(minutes=1)
    volts(m, 0)
    clock.advance(minutes=1)
    volts(m, 229)
    assert [e["type"] for e in m.get_outages()["outages"]] == ["total"]


def test_log_keeps_today_and_yesterday_and_survives_a_restart(tmp_path, clock):
    path = str(tmp_path / "outages.json")
    log = OutageLog(path, clock=clock)
    clock.now = datetime(2026, 10, 24, 10, 0)
    log.update("partial", missing=[1])
    clock.now = datetime(2026, 10, 24, 10, 5)
    log.update("ok")                                  # two days ago
    clock.now = datetime(2026, 10, 25, 22, 0)
    log.update("total")
    clock.now = datetime(2026, 10, 25, 22, 30)
    log.update("ok")                                  # yesterday
    clock.now = datetime(2026, 10, 26, 9, 0)
    log.update("total")                               # going on now
    again = OutageLog(path, clock=clock)
    out = again.snapshot()
    assert [(e["start"], e["end"]) for e in out] == [
        ("2026-10-26T09:00:00", None), ("2026-10-25T22:00:00", "2026-10-25T22:30:00")]
    assert json.loads(open(path).read())["open"]["type"] == "total"


def test_bad_log_file_is_ignored(tmp_path, clock):
    path = tmp_path / "outages.json"
    path.write_text(json.dumps({"events": [{"type": "x"}, "y", {"type": "total", "start": "bad", "end": "bad",
                                                             "phases": [], "cut": []}], "open": {"type": 1}}))
    assert OutageLog(str(path), clock=clock).snapshot() == []


def test_api(monkeypatch):
    import app as app_module
    app_module.app.config["TESTING"] = True
    c = app_module.app.test_client()
    monkeypatch.setattr(app_module, "smart_meter", None)
    assert c.get("/api/meter-outages").get_json() == {"status": "disabled"}
    clock = Clock()
    m = SmartMeter(daily_file=None, clock=clock)
    volts(m, 230, 0, 229)
    monkeypatch.setattr(app_module, "smart_meter", m)
    data = c.get("/api/meter-outages").get_json()
    assert data["status"] == "ok" and data["today"] == "2026-10-26" and data["outages"][0]["phases"] == [2]


def test_split_second_changes_are_not_outages(clock):
    """V1..V3 arrive one by one: 0,0,0 -> 230,0,0 -> 230,231,0 -> 230,231,229 within a second."""
    m = SmartMeter(daily_file=None, clock=clock)
    volts(m, 230, 231, 229)
    clock.advance(seconds=1)
    volts(m, 230, 0, 229)
    clock.advance(seconds=2)
    volts(m, 230, 231, 229)
    assert m.get_outages()["outages"] == []


def test_retained_readings_on_resubscribe_dont_end_an_outage(clock):
    """Seen live: during an outage the dashboard reconnected to the broker, which
    sent its stored (retained) pre-outage readings; they ended the outage at
    23:39 although the grid came back at 00:01."""
    m = SmartMeter(daily_file=None, clock=clock)
    volts(m, 230, 231, 229)
    m.handle_message("buckler/M1/status", "offline")
    clock.advance(minutes=2)
    assert m.get_outages()["outages"][0]["end"] is None
    clock.advance(minutes=36)
    m.handle_message("buckler/M1/status", "offline", retained=True)
    m.handle_message("buckler/M1/dataJson", json.dumps({"V": [230, 231, 229]}), retained=True)
    for i, v in enumerate((230, 231, 229), start=1):
        m.handle_message(f"buckler/M1/data/V{i}", str(v), retained=True)
    m.handle_message("buckler/M1/relay/r1", "100", retained=True)
    clock.advance(minutes=5)
    out = m.get_outages()["outages"]
    assert [(e["type"], e["end"]) for e in out] == [("total", None)]
    volts(m, 228, 230, 231)                           # the meter itself again: back
    assert m.get_outages()["outages"][0]["end"] == "2026-10-26T12:43:00"


def test_client_passes_the_retain_flag():
    from unittest.mock import MagicMock
    from smart_meter import SmartMeterClient
    meter = MagicMock()
    client = SmartMeterClient(meter, "localhost", interval=0)
    message = MagicMock(topic="buckler/M1/data/V1", payload=b"230", retain=1)
    client._on_message(None, None, message)
    meter.handle_message.assert_called_once_with("buckler/M1/data/V1", b"230", retained=True)


def _meter_with_relays(clock):
    m = SmartMeter(daily_file=None, clock=clock)
    volts(m, 230, 231, 229)
    for r in (1, 2, 3):
        m.handle_message(f"buckler/M1/relay/r{r}", "100")
    return m


def _boot_with_relays_off(m):
    """The meter is back after an outage: voltage on all phases, every relay still off."""
    m.handle_message("buckler/M1/dataJson", json.dumps({"V": [230, 231, 229], "relay": [0, 0, 0]}))


def _relays_on(m):
    for r in (1, 2, 3):
        m.handle_message(f"buckler/M1/relay/r{r}", "100")


def test_the_meter_waiting_to_switch_back_on_is_part_of_the_outage(clock):
    """Seen live: a total outage, then ~110 s with every phase cut before the meter
    switches the house back on. One "No grid" entry up to the switch-on."""
    m = _meter_with_relays(clock)
    m.handle_message("buckler/M1/status", "offline")
    clock.advance(minutes=60)
    m.check_offline()                                 # the 15 s check has opened the outage
    _boot_with_relays_off(m)
    clock.advance(seconds=60)
    # While it waits: still the running outage
    assert [(e["type"], e["end"]) for e in m.get_outages()["outages"]] == [("total", None)]
    clock.advance(seconds=50)
    _relays_on(m)
    out = m.get_outages()["outages"]
    assert out == [{"type": "total", "start": "2026-10-26T12:00:00", "end": "2026-10-26T13:01:50",
                    "phases": [], "cut": []}]


def test_a_long_wait_is_the_meters_protection_and_its_own_entry(clock):
    """Every phase kept off for longer than the switch-on delay (e.g. the voltage out
    of range after the outage): a partial entry after the "No grid" one."""
    m = _meter_with_relays(clock)
    m.handle_message("buckler/M1/status", "offline")
    clock.advance(minutes=60)
    m.check_offline()                                 # the 15 s check has opened the outage
    _boot_with_relays_off(m)
    clock.advance(minutes=24)
    out = m.get_outages()["outages"]
    assert [(e["type"], e["start"][11:16], e["end"] and e["end"][11:16]) for e in out] == [
        ("partial", "13:00", None), ("total", "12:00", "13:00")]
    _relays_on(m)
    out = m.get_outages()["outages"]
    assert [(e["type"], e["end"][11:16], e["cut"]) for e in out] == [
        ("partial", "13:24", [1, 2, 3]), ("total", "13:00", [])]


def test_all_phases_cut_without_an_outage_before_is_partial(clock):
    m = _meter_with_relays(clock)
    clock.advance(minutes=5)
    for r in (1, 2, 3):
        m.handle_message(f"buckler/M1/relay/r{r}", "0")
    clock.advance(minutes=2)
    _relays_on(m)
    out = m.get_outages()["outages"]
    assert [(e["type"], e["cut"]) for e in out] == [("partial", [1, 2, 3])]
    assert "restore" not in out[0]


def test_switch_on_delay_setting(clock, monkeypatch):
    """A meter set to a 10-minute delay: a 9-minute wait is still part of the outage."""
    m = SmartMeter(daily_file=None, clock=clock, switch_on_delay=600)
    volts(m, 230, 231, 229)
    for r in (1, 2, 3):
        m.handle_message(f"buckler/M1/relay/r{r}", "100")
    m.handle_message("buckler/M1/status", "offline")
    clock.advance(minutes=30)
    m.check_offline()
    _boot_with_relays_off(m)
    clock.advance(minutes=9)
    _relays_on(m)
    assert [(e["type"], e["end"][11:16]) for e in m.get_outages()["outages"]] == [("total", "12:39")]


@pytest.mark.parametrize("value, expected", [(None, 180), ("300", 360), ("abc", 180), ("-5", 60)])
def test_switch_on_delay_from_env(value, expected, tmp_path):
    from smart_meter import create_smart_meter_from_env
    env = {"SMART_METER_ENABLED": "true", "SMART_METER_DAILY_FILE": str(tmp_path / "d.json"),
           "SMART_METER_OUTAGES_FILE": str(tmp_path / "o.json")}
    if value is not None:
        env["SMART_METER_SWITCH_ON_DELAY"] = value
    meter, _ = create_smart_meter_from_env(env)
    assert meter.outages.restore_seconds == expected   # the delay plus the margin
