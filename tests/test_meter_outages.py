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
