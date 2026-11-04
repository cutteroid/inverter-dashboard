"""Outage readiness: will the battery last through the next planned outage?"""
from datetime import datetime
from unittest.mock import patch

import pytest

from outage_readiness import BATTERY_LOSS_FACTOR, merged_windows, next_outage, outage_readiness

NOW = datetime(2026, 10, 21, 12, 0)
CAPACITY_WH = 10240                      # 10.24 kWh


def _at(h, m=0, day=21):
    return datetime(2026, 10, day, h, m)


def _days(*windows):
    return [{"windows": list(windows)}]


def _data(soc=80, shutdown=20, charge_minutes=None):
    return {"battery_soc": soc, "battery_shutdown_soc": shutdown, "battery_charge_minutes": charge_minutes}


def test_touching_windows_are_one_outage():
    days = [{"windows": [(_at(16), _at(18)), (_at(14), _at(16))]},
            {"windows": [(_at(0, day=22), _at(4, day=22))]}]
    assert merged_windows(days) == [(_at(14), _at(18)), (_at(0, day=22), _at(4, day=22))]
    assert next_outage(days, NOW) == (_at(14), _at(18))
    assert next_outage(days, _at(19)) == (_at(0, day=22), _at(4, day=22))   # tomorrow's
    assert next_outage(days, _at(5, day=22)) is None


def test_no_outage():
    assert outage_readiness(_data(), _days((_at(8), _at(10))), NOW, 400, CAPACITY_WH) == {"status": "no_outage"}


def test_enough():
    # 14:00-18:00 at 400 W: 4 h x 440 W = 1.76 kWh; 80% -> 20% of 10.24 kWh = 6.14 kWh
    r = outage_readiness(_data(), _days((_at(14), _at(18))), NOW, 400, CAPACITY_WH)
    assert r["status"] == "ok" and r["verdict"] == "enough" and r["during"] is False
    assert r["starts_in_minutes"] == 120 and r["outage_minutes"] == 240
    assert r["needed_kwh"] == pytest.approx(1.76) and r["available_kwh"] == pytest.approx(6.14, abs=0.01)
    assert r["lasts_minutes"] == round(6144 / (400 * BATTERY_LOSS_FACTOR) * 60)
    assert r["short_minutes"] is None


def test_tight_and_short():
    window = _days((_at(14), _at(22)))                     # 8 h
    tight = outage_readiness(_data(soc=55), window, NOW, 400, CAPACITY_WH)   # 3.58 vs 3.52 kWh
    assert tight["verdict"] == "tight"
    short = outage_readiness(_data(soc=40), window, NOW, 400, CAPACITY_WH)   # 2.05 vs 3.52 kWh
    assert short["verdict"] == "short"
    assert short["short_minutes"] == short["outage_minutes"] - short["lasts_minutes"] > 0


def test_during_an_outage_counts_the_rest():
    r = outage_readiness(_data(soc=50), _days((_at(10), _at(14))), NOW, 400, CAPACITY_WH)
    assert r["during"] is True and r["starts_in_minutes"] is None
    assert r["outage_minutes"] == 120 and r["needed_kwh"] == pytest.approx(0.88)


def test_charging_before_the_outage():
    # 50% now, full in 4 h; the outage starts in 2 h -> ~75% at its start
    r = outage_readiness(_data(soc=50, charge_minutes=240), _days((_at(14), _at(18))), NOW, 400, CAPACITY_WH)
    assert r["soc"] == 50 and r["soc_at_start"] == 75


def test_on_battery_before_the_outage():
    """Seen live: on battery at 05:14 (34%, the battery giving ~344 W, load average
    277 W), the planned outage at 06:30-14:00. The battery's own draw counts, and
    what it gives until 06:30 is gone by the start."""
    now = datetime(2026, 11, 4, 5, 15)
    data = {**_data(soc=34, shutdown=15), "grid_online": False, "battery_discharge_average_w": 344}
    days = _days((datetime(2026, 11, 4, 6, 30), datetime(2026, 11, 4, 14, 0)))
    r = outage_readiness(data, days, now, 277, CAPACITY_WH)
    assert r["basis"] == "battery" and r["draw_w"] == 344
    # 75 min x 344 W = 430 Wh = 4.2% of 10.24 kWh
    assert r["soc"] == 34 and r["soc_at_start"] == 30
    assert r["available_kwh"] == pytest.approx(CAPACITY_WH * (34 - 4.2 - 15) / 100 / 1000, abs=0.01)
    assert r["lasts_minutes"] == pytest.approx(r["available_kwh"] * 1000 / 344 * 60, abs=2)   # ~4h 25m
    assert r["verdict"] == "short" and r["short_minutes"] == 450 - r["lasts_minutes"]


def test_on_battery_during_the_outage_uses_the_battery_draw():
    data = {**_data(soc=50), "grid_online": False, "battery_discharge_average_w": 500}
    r = outage_readiness(data, _days((_at(10), _at(14))), NOW, 400, CAPACITY_WH)
    assert r["basis"] == "battery" and r["soc_at_start"] == 50
    assert r["needed_kwh"] == pytest.approx(1.0)                       # 2 h x 500 W


def test_with_the_grid_on_the_load_estimate_counts():
    """Discharging with the grid there (e.g. a time-of-use setting): not on battery
    because of an outage, so the load estimate and today's charge, as before."""
    data = {**_data(soc=80), "grid_online": True, "battery_discharge_average_w": 900}
    r = outage_readiness(data, _days((_at(14), _at(18))), NOW, 400, CAPACITY_WH)
    assert r["basis"] == "load" and r["draw_w"] == 440 and r["soc_at_start"] == 80


def test_unknown_capacity():
    r = outage_readiness(_data(), _days((_at(14), _at(18))), NOW, 400, None)
    assert r["status"] == "no_data" and r["start"] == _at(14).isoformat()


class TestEndpoint:
    @pytest.fixture
    def client(self):
        import app as app_module
        app_module.app.config["TESTING"] = True
        saved = (app_module.outage_poller, app_module.inverter_poller, app_module.inverter_config)
        with app_module.app.test_client() as c:
            yield c, app_module
        app_module.outage_poller, app_module.inverter_poller, app_module.inverter_config = saved

    def test_disabled_without_schedule(self, client):
        c, app_module = client
        app_module.outage_poller = None
        assert c.get("/api/outage-readiness").get_json() == {"status": "disabled"}

    def test_readiness(self, client):
        from unittest.mock import MagicMock
        from inverters import InverterConfig
        c, app_module = client
        app_module.inverter_config = InverterConfig(phases=3, has_battery=True)
        app_module.outage_poller = MagicMock()
        app_module.outage_poller.get_outage_status.return_value = {
            "status": "upcoming", "days": [{"date": NOW.date(), "windows": [(_at(14), _at(18))]}]}
        app_module.inverter_poller = MagicMock(data={"battery_soc": 80, "battery_shutdown_soc": 20,
                                                     "load_average_w": 400, "battery_capacity_ah": 200,
                                                     "battery_voltage": 51.2})
        with patch("app.datetime") as fake_datetime:
            fake_datetime.now.return_value = NOW
            r = c.get("/api/outage-readiness").get_json()
        assert r["verdict"] == "enough" and r["average_load_w"] == 400
        assert r["available_kwh"] == pytest.approx(6.14, abs=0.01)    # 200 Ah x 51.2 V x 60%
