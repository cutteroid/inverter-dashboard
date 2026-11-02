"""Tests for Flask API endpoints in app.py."""
import json
import pytest
from unittest.mock import patch, MagicMock, PropertyMock
from datetime import datetime, timedelta

from inverters import InverterConfig


@pytest.fixture
def client():
    """Create a Flask test client with mocked pollers."""
    import app as app_module

    app_module.app.config["TESTING"] = True

    # Store originals
    orig_inverter_poller = app_module.inverter_poller
    orig_outage_poller = app_module.outage_poller
    orig_weather_poller = app_module.weather_poller
    orig_inverter_config = app_module.inverter_config
    orig_configured = app_module._configured

    # Create mock pollers
    mock_inv_poller = MagicMock()
    mock_weather_poller = MagicMock()

    app_module.inverter_poller = mock_inv_poller
    app_module.weather_poller = mock_weather_poller
    app_module.inverter_config = InverterConfig(phases=3, has_battery=True, pv_strings=2, has_generator=False)
    app_module._configured = True  # endpoints return 503/defaults when unconfigured

    with app_module.app.test_client() as c:
        yield c, mock_inv_poller, app_module

    # Restore originals
    app_module.inverter_poller = orig_inverter_poller
    app_module.outage_poller = orig_outage_poller
    app_module.weather_poller = orig_weather_poller
    app_module.inverter_config = orig_inverter_config
    app_module._configured = orig_configured


class TestGetData:
    def test_503_when_no_data(self, client):
        c, mock_inv, _ = client
        type(mock_inv).data = PropertyMock(return_value=None)
        resp = c.get("/api/data")
        assert resp.status_code == 503

    def test_returns_data_with_config(self, client):
        c, mock_inv, app_module = client
        type(mock_inv).data = PropertyMock(return_value={
            "pv_total_power": 500,
            "battery_soc": 75,
            "grid_power": -100,
        })
        app_module.outage_poller = None
        resp = c.get("/api/data")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["pv_total_power"] == 500
        assert "config" in data
        assert data["config"]["phases"] == 3


    def test_meter_cut_only_with_a_meter(self, client, monkeypatch):
        c, mock_inv, app_module = client
        type(mock_inv).data = PropertyMock(return_value={"grid_online": False, "grid_state": "outage"})
        app_module.outage_poller = None
        monkeypatch.setattr(app_module, "smart_meter", None)
        assert "meter_cut" not in c.get("/api/data").get_json()
        meter = MagicMock()
        meter.phases_cut.return_value = [1, 2, 3]
        monkeypatch.setattr(app_module, "smart_meter", meter)
        assert c.get("/api/data").get_json()["meter_cut"] == [1, 2, 3]


class TestGetPhaseStats:
    def test_empty_stats_returns_empty_list(self, client):
        c, _, app_module = client
        with patch.object(app_module, "load_phase_stats", return_value={}):
            resp = c.get("/api/phase-stats")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_calculates_percentages(self, client):
        c, _, app_module = client
        stats = {
            "2025-01-15": {
                "l1_wh": 1000, "l2_wh": 2000, "l3_wh": 3000,
                "samples": 100, "l1_max": 500, "l2_max": 1000, "l3_max": 1500,
            }
        }
        with patch.object(app_module, "load_phase_stats", return_value=stats):
            resp = c.get("/api/phase-stats")
        data = resp.get_json()
        assert len(data) == 1
        entry = data[0]
        assert entry["l1_pct"] == pytest.approx(16.7, abs=0.1)
        assert entry["l2_pct"] == pytest.approx(33.3, abs=0.1)
        assert entry["l3_pct"] == pytest.approx(50.0, abs=0.1)

    def test_zero_total_no_division_error(self, client):
        c, _, app_module = client
        stats = {
            "2025-01-15": {
                "l1_wh": 0, "l2_wh": 0, "l3_wh": 0,
                "samples": 1, "l1_max": 0, "l2_max": 0, "l3_max": 0,
            }
        }
        with patch.object(app_module, "load_phase_stats", return_value=stats):
            resp = c.get("/api/phase-stats")
        assert resp.status_code == 200
        entry = resp.get_json()[0]
        assert entry["l1_pct"] == 0
        assert entry["l2_pct"] == 0
        assert entry["l3_pct"] == 0


class TestGetOutageSchedule:
    def test_disabled_returns_disabled(self, client):
        c, _, app_module = client
        app_module.outage_poller = None
        resp = c.get("/api/outage_schedule")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "disabled"

    def test_active_outage_returns_times(self, client):
        c, _, app_module = client
        now = datetime.now()
        mock_poller = MagicMock()
        mock_poller.get_outage_status.return_value = {
            "status": "active",
            "start_time": now - timedelta(minutes=30),
            "end_time": now + timedelta(minutes=30),
            "remaining_minutes": 30,
        }
        mock_poller.provider.display_name = "ДТЕК"
        app_module.outage_poller = mock_poller
        resp = c.get("/api/outage_schedule")
        data = resp.get_json()
        assert data["status"] == "active"
        assert "start_time" in data
        assert "end_time" in data
        assert data["remaining_minutes"] == 30
        assert data["provider"] == "ДТЕК"

    def test_days_are_serialized(self, client):
        c, _, app_module = client
        today = datetime(2026, 10, 17)
        tomorrow = datetime(2026, 10, 18)
        mock_poller = MagicMock()
        mock_poller.get_outage_status.return_value = {
            "status": "clear",
            "days": [
                {"date": today.date(), "windows": []},
                {"date": tomorrow.date(), "windows": [
                    (tomorrow.replace(hour=8), tomorrow.replace(hour=12)),
                ]},
            ],
        }
        mock_poller.provider.display_name = "ДТЕК"
        app_module.outage_poller = mock_poller
        data = c.get("/api/outage_schedule").get_json()
        assert data["days"] == [
            {"date": "2026-10-17", "windows": []},
            {"date": "2026-10-18", "windows": [
                {"start": "2026-10-18T08:00:00", "end": "2026-10-18T12:00:00"},
            ]},
        ]

    def test_error_returns_message(self, client):
        c, _, app_module = client
        mock_poller = MagicMock()
        mock_poller.get_outage_status.return_value = {
            "status": "error",
            "error": "YASNO API returned HTTP 500",
        }
        mock_poller.provider.display_name = "YASNO"
        app_module.outage_poller = mock_poller
        resp = c.get("/api/outage_schedule")
        assert resp.get_json() == {
            "status": "error", "error": "YASNO API returned HTTP 500", "provider": "YASNO",
        }


class TestGetGenerator:
    def test_disabled_returns_false(self, client):
        c, _, app_module = client
        app_module.inverter_config = InverterConfig(has_generator=False)
        resp = c.get("/api/generator")
        data = resp.get_json()
        assert data["enabled"] is False

    def test_running_with_fuel_data(self, client):
        c, mock_inv, app_module = client
        app_module.inverter_config = InverterConfig(has_generator=True)
        type(mock_inv).data = PropertyMock(return_value={"generator_power": 3000})
        with patch.object(app_module, "load_generator_log", return_value={}), \
             patch("app.GENERATOR_FUEL_RATE", 2.5), \
             patch("app.GENERATOR_OIL_CHANGE_DATE", ""), \
             patch("app.generator_session_start", None):
            resp = c.get("/api/generator")
        data = resp.get_json()
        assert data["enabled"] is True
        assert data["running"] is True
        assert data["power"] == 3000


class TestGetWeather:
    def test_503_when_no_data(self, client):
        c, _, app_module = client
        type(app_module.weather_poller).data = PropertyMock(return_value=None)
        resp = c.get("/api/weather")
        assert resp.status_code == 503

    def test_returns_data(self, client):
        c, _, app_module = client
        type(app_module.weather_poller).data = PropertyMock(return_value={
            "temperature": 22.5,
            "weather_code": 0,
        })
        resp = c.get("/api/weather")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["temperature"] == 22.5


class TestParseWeather:
    RAW = {
        "current": {"temperature_2m": 11.4, "weather_code": 3},
        "daily": {
            "time": ["2026-10-19", "2026-10-20"],
            "sunrise": ["2026-10-19T07:21", "2026-10-20T07:23"],
            "sunset": ["2026-10-19T17:58", "2026-10-20T17:56"],
            "temperature_2m_max": [13.2, 9.6],
            "temperature_2m_min": [4.1, 2.8],
            "precipitation_sum": [0.0, 3.4],
            "weather_code": [3, 61],
        },
    }

    def test_today_and_tomorrow(self):
        from app import parse_weather
        result = parse_weather(self.RAW)
        assert result["temperature"] == 11.4 and result["temp_max"] == 13.2
        assert result["sunrise"] == "2026-10-19T07:21"
        assert result["tomorrow"] == {"date": "2026-10-20", "weather_code": 61,
                                      "temp_max": 9.6, "temp_min": 2.8, "precipitation": 3.4}

    def test_no_tomorrow_in_response(self):
        from app import parse_weather
        raw = {"current": {}, "daily": {k: v[:1] for k, v in self.RAW["daily"].items()}}
        result = parse_weather(raw)
        assert result["tomorrow"] is None and result["temp_min"] == 4.1
        assert parse_weather({})["tomorrow"] is None


class TestClearEndpoints:
    def test_clear_phase_stats(self, client):
        c, _, app_module = client
        with patch.object(app_module, "save_phase_stats") as mock_phase:
            app_module._pin_failures, app_module._pin_locked_until = 0, 0.0
            resp = c.post("/api/phase-stats/clear", headers={"X-Settings-Pin": "0000"})
        assert resp.status_code == 200
        mock_phase.assert_called_once_with({})


class TestNoCacheHeaders:
    def test_api_answers_are_never_cached(self, client):
        c, _, app_module = client
        app_module.alerts_poller = None
        assert c.get("/api/alerts").headers["Cache-Control"] == "no-store"

    def test_page_is_not_cached(self, client):
        c, _, _ = client
        assert "no-store" in c.get("/").headers["Cache-Control"]
