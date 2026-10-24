"""Tests for history.py (5-minute averages for the sparklines) and /api/history."""
import json

import pytest

from history import PowerHistory

T0 = 1_800_000_000  # a bucket start (divisible by 300)


def reading(soc=50, pv=1000, load=400):
    return {"battery_soc": soc, "pv_total_power": pv, "load_power": load}


def test_polls_in_a_bucket_are_averaged():
    h = PowerHistory()
    h.add(reading(soc=50, pv=1000, load=400), now=T0 + 10)
    h.add(reading(soc=52, pv=2000, load=600), now=T0 + 40)
    s = h.series(now=T0 + 60)
    assert s["t"] == [T0]
    assert s["soc"] == [51.0] and s["pv"] == [1500] and s["load"] == [500]
    assert s["bucket_seconds"] == 300 and s["now"] == T0 + 60


def test_a_new_bucket_closes_the_last_one_and_saves(tmp_path):
    path = tmp_path / "history.json"
    h = PowerHistory(str(path))
    h.add(reading(pv=1000), now=T0 + 10)
    assert not path.exists()  # written when a bucket closes, not on every poll
    h.add(reading(pv=3000), now=T0 + 310)
    saved = json.loads(path.read_text())
    assert saved == {"bucket_seconds": 300, "buckets": [[T0, 50.0, 1000, 400]]}
    assert h.series(now=T0 + 320)["pv"] == [1000, 3000]


def test_history_survives_a_restart(tmp_path, monkeypatch):
    path = str(tmp_path / "history.json")
    h = PowerHistory(path)
    h.add(reading(soc=60), now=T0 + 10)
    h.add(reading(soc=61), now=T0 + 310)
    monkeypatch.setattr("history.time.time", lambda: T0 + 400)
    again = PowerHistory(path)
    assert again.series()["soc"] == [60.0]


def test_only_the_last_24_hours_are_kept(tmp_path):
    h = PowerHistory(str(tmp_path / "history.json"))
    for i in range(300):  # 25 hours of buckets
        h.add(reading(pv=i), now=T0 + i * 300)
    s = h.series(now=T0 + 299 * 300 + 10)
    assert len(s["t"]) == 288
    assert s["t"][0] > T0 + 299 * 300 - 24 * 3600


def test_missing_readings_are_none():
    """No battery (or a driver without the field): that series has gaps, the others don't."""
    h = PowerHistory()
    h.add({"pv_total_power": 800, "load_power": 300, "battery_soc": None}, now=T0)
    s = h.series(now=T0 + 1)
    assert s["soc"] == [None] and s["pv"] == [800]


def test_bad_files_are_ignored(tmp_path):
    path = tmp_path / "history.json"
    path.write_text(json.dumps({"bucket_seconds": 60, "buckets": [[T0, 1, 2, 3]]}))
    assert PowerHistory(str(path)).series(now=T0)["t"] == []  # other bucket size
    path.write_text(json.dumps({"bucket_seconds": 300, "buckets": [[T0, "x", 2, 3], [T0 + 300, 1, 2], "y"]}))
    assert PowerHistory(str(path)).series(now=T0 + 400)["t"] == []


@pytest.fixture
def client():
    import app as app_module
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c, app_module


def test_api_history(client, monkeypatch):
    c, app_module = client
    monkeypatch.setattr(app_module, "power_history", None)
    empty = c.get("/api/history").get_json()
    assert empty["t"] == [] and empty["soc"] == [] and empty["bucket_seconds"] == 300

    h = PowerHistory()
    h.add(reading(soc=70, pv=1200, load=500))
    monkeypatch.setattr(app_module, "power_history", h)
    data = c.get("/api/history").get_json()
    assert data["soc"] == [70.0] and data["pv"] == [1200] and data["load"] == [500]


def test_poller_adds_good_readings_only():
    import app as app_module

    class FakeInverter:
        def __init__(self, result):
            self.result = result

        def read_all_data(self):
            return dict(self.result)

    h = PowerHistory()
    poller = app_module.InverterPoller(FakeInverter(reading(pv=900)), history=h)
    poller._fetch()
    poller.inverter = FakeInverter({"error": "timeout"})
    poller._fetch()
    assert h.series()["pv"] == [900]
