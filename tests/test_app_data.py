"""Tests for data recording functions in app.py."""
import json
import os
import time
import pytest
from unittest.mock import patch
from datetime import datetime, timedelta


class TestRecordGridDailyImport:
    def test_creates_new_log(self, tmp_path):
        log_file = str(tmp_path / "grid_log.json")
        with patch("app.GRID_DAILY_LOG_FILE", log_file):
            from app import record_grid_daily_import
            record_grid_daily_import(5.5)
        with open(log_file) as f:
            log = json.load(f)
        today = datetime.now().strftime("%Y-%m-%d")
        assert log[today] == 5.5

    def test_overwrites_same_day(self, tmp_path):
        log_file = str(tmp_path / "grid_log.json")
        today = datetime.now().strftime("%Y-%m-%d")
        with open(log_file, "w") as f:
            json.dump({today: 3.0}, f)
        with patch("app.GRID_DAILY_LOG_FILE", log_file):
            from app import record_grid_daily_import
            record_grid_daily_import(7.0)
        with open(log_file) as f:
            log = json.load(f)
        assert log[today] == 7.0

    def test_90_day_rotation(self, tmp_path):
        log_file = str(tmp_path / "grid_log.json")
        # Create 95 days of entries
        base = datetime.now()
        old_log = {}
        for i in range(95):
            day = (base - timedelta(days=i)).strftime("%Y-%m-%d")
            old_log[day] = float(i)
        with open(log_file, "w") as f:
            json.dump(old_log, f)
        with patch("app.GRID_DAILY_LOG_FILE", log_file):
            from app import record_grid_daily_import
            record_grid_daily_import(1.0)
        with open(log_file) as f:
            log = json.load(f)
        assert len(log) <= 90


class TestTrackGeneratorRuntime:
    def _reset_globals(self):
        """Reset global state before each test."""
        import app
        app.generator_last_running = None
        app.generator_session_start = None

    def test_off_to_on_starts_session(self, tmp_path):
        self._reset_globals()
        log_file = str(tmp_path / "gen_log.json")
        with patch("app.GENERATOR_LOG_FILE", log_file):
            import app
            app.track_generator_runtime(1000)  # power > 0 = running
        with open(log_file) as f:
            log = json.load(f)
        today = datetime.now().strftime("%Y-%m-%d")
        assert len(log[today]["sessions"]) == 1
        assert log[today]["sessions"][0]["end"] is None

    def test_on_to_off_closes_session(self, tmp_path):
        self._reset_globals()
        log_file = str(tmp_path / "gen_log.json")
        with patch("app.GENERATOR_LOG_FILE", log_file):
            import app
            # First: start running
            app.track_generator_runtime(1000)
            # Simulate 60s elapsed
            app.generator_session_start = datetime.now() - timedelta(seconds=60)
            # Then: stop running
            app.track_generator_runtime(0)
        with open(log_file) as f:
            log = json.load(f)
        today = datetime.now().strftime("%Y-%m-%d")
        assert log[today]["runtime_seconds"] >= 59
        assert log[today]["sessions"][-1]["end"] is not None

    def test_stays_off_no_session(self, tmp_path):
        self._reset_globals()
        log_file = str(tmp_path / "gen_log.json")
        with patch("app.GENERATOR_LOG_FILE", log_file):
            import app
            app.track_generator_runtime(0)
            app.track_generator_runtime(0)
        with open(log_file) as f:
            log = json.load(f)
        today = datetime.now().strftime("%Y-%m-%d")
        assert log[today]["sessions"] == []

    def test_stays_on_no_duplicate(self, tmp_path):
        self._reset_globals()
        log_file = str(tmp_path / "gen_log.json")
        with patch("app.GENERATOR_LOG_FILE", log_file):
            import app
            app.track_generator_runtime(1000)
            app.track_generator_runtime(1000)
        with open(log_file) as f:
            log = json.load(f)
        today = datetime.now().strftime("%Y-%m-%d")
        assert len(log[today]["sessions"]) == 1

    def test_90_day_rotation(self, tmp_path):
        self._reset_globals()
        log_file = str(tmp_path / "gen_log.json")
        base = datetime.now()
        old_log = {}
        for i in range(95):
            day = (base - timedelta(days=i)).strftime("%Y-%m-%d")
            old_log[day] = {"runtime_seconds": 0, "sessions": []}
        with open(log_file, "w") as f:
            json.dump(old_log, f)
        with patch("app.GENERATOR_LOG_FILE", log_file):
            import app
            app.track_generator_runtime(0)
        with open(log_file) as f:
            log = json.load(f)
        assert len(log) <= 90


class TestRecordPhaseSample:
    def _reset_globals(self):
        import app
        app.last_sample_time = None

    def test_first_sample_no_energy(self, tmp_path):
        self._reset_globals()
        stats_file = str(tmp_path / "phase_stats.json")
        with patch("app.PHASE_STATS_FILE", stats_file):
            import app
            app.record_phase_sample(100, 200, 300)
            stats = app.load_phase_stats()   # in memory: written to file every few minutes
        today = datetime.now().strftime("%Y-%m-%d")
        assert stats[today]["samples"] == 1
        # No energy accumulated on first sample (no previous time)
        assert stats[today]["l1_wh"] == 0

    def test_energy_accumulation(self, tmp_path):
        self._reset_globals()
        stats_file = str(tmp_path / "phase_stats.json")
        with patch("app.PHASE_STATS_FILE", stats_file):
            import app
            # First sample: sets last_sample_time
            app.record_phase_sample(1000, 2000, 3000)
            # Simulate 60 seconds elapsed
            app.last_sample_time = time.monotonic() - 60
            # Second sample
            app.record_phase_sample(1000, 2000, 3000)
            stats = app.load_phase_stats()   # in memory: written to file every few minutes
        today = datetime.now().strftime("%Y-%m-%d")
        # 1000W * (60s/3600) = ~16.67 Wh
        assert stats[today]["l1_wh"] > 15
        assert stats[today]["l1_wh"] < 20

    def test_large_interval_skipped(self, tmp_path):
        self._reset_globals()
        stats_file = str(tmp_path / "phase_stats.json")
        with patch("app.PHASE_STATS_FILE", stats_file):
            import app
            app.record_phase_sample(1000, 2000, 3000)
            # Simulate 10 minutes elapsed (> 5 min threshold)
            app.last_sample_time = time.monotonic() - 600
            app.record_phase_sample(1000, 2000, 3000)
            stats = app.load_phase_stats()   # in memory: written to file every few minutes
        today = datetime.now().strftime("%Y-%m-%d")
        # Energy should NOT have been accumulated (interval > 0.1 hours = 6 min)
        assert stats[today]["l1_wh"] == 0

    def test_max_tracking(self, tmp_path):
        self._reset_globals()
        stats_file = str(tmp_path / "phase_stats.json")
        with patch("app.PHASE_STATS_FILE", stats_file):
            import app
            app.record_phase_sample(100, 200, 300)
            app.last_sample_time = time.monotonic() - 30
            app.record_phase_sample(500, 600, 700)
            stats = app.load_phase_stats()   # in memory: written to file every few minutes
        today = datetime.now().strftime("%Y-%m-%d")
        assert stats[today]["l1_max"] == 500
        assert stats[today]["l2_max"] == 600
        assert stats[today]["l3_max"] == 700


class TestGeneratorMidnightAndRestart:
    def _reset(self):
        import app
        app.generator_last_running = None
        app.generator_session_start = None

    def test_session_split_at_midnight(self, tmp_path):
        self._reset()
        log_file = str(tmp_path / "gen_log.json")
        with patch("app.GENERATOR_LOG_FILE", log_file):
            import app
            app.track_generator_runtime(1000, now=datetime(2026, 10, 16, 23, 0, 0))
            app.track_generator_runtime(1000, now=datetime(2026, 10, 17, 0, 30, 0))
            app.track_generator_runtime(0, now=datetime(2026, 10, 17, 1, 0, 0))
        with open(log_file) as f:
            log = json.load(f)
        assert log["2026-10-16"]["runtime_seconds"] == 3600
        assert log["2026-10-16"]["sessions"] == [{"start": "23:00:00", "end": "24:00:00"}]
        assert log["2026-10-17"]["runtime_seconds"] == 3600
        assert log["2026-10-17"]["sessions"] == [{"start": "00:00:00", "end": "01:00:00"}]

    def test_open_session_resumed_after_restart(self, tmp_path):
        self._reset()
        log_file = str(tmp_path / "gen_log.json")
        with open(log_file, "w") as f:
            json.dump({"2026-10-17": {"runtime_seconds": 0,
                                      "sessions": [{"start": "10:00:00", "end": None}]}}, f)
        with patch("app.GENERATOR_LOG_FILE", log_file):
            import app
            # App restarted; generator still running, then stops at 11:00
            app.track_generator_runtime(1000, now=datetime(2026, 10, 17, 10, 30, 0))
            app.track_generator_runtime(0, now=datetime(2026, 10, 17, 11, 0, 0))
        with open(log_file) as f:
            log = json.load(f)
        assert log["2026-10-17"]["sessions"] == [{"start": "10:00:00", "end": "11:00:00"}]
        assert log["2026-10-17"]["runtime_seconds"] == 3600


class TestBatteryRuntime:
    """Runtime estimate: usable energy above the shutdown SOC / average discharge power."""

    DATA = {"battery_soc": 92, "battery_shutdown_soc": 10, "battery_capacity_ah": 100, "battery_voltage": 53.2}

    def test_estimate(self):
        import app
        # 100 Ah x 53.2 V = 5320 Wh; (92 - 10)% = 4362.4 Wh; at 411 W -> 636 min
        assert app.estimate_battery_runtime(self.DATA, 411) == 636

    def test_none_when_not_discharging_or_capacity_unknown(self):
        import app
        assert app.estimate_battery_runtime(self.DATA, 0) is None
        assert app.estimate_battery_runtime({**self.DATA, "battery_capacity_ah": 0}, 400) is None

    def test_at_or_below_shutdown_soc_is_zero(self):
        import app
        assert app.estimate_battery_runtime({**self.DATA, "battery_soc": 8}, 400) == 0

    def test_capacity_override(self, monkeypatch):
        import app
        monkeypatch.setattr(app, "BATTERY_CAPACITY_KWH", 10.0)
        assert app.estimate_battery_runtime({**self.DATA, "battery_shutdown_soc": 0}, 920) == 600  # 9.2 kWh / 920 W

    def test_capacity_from_nominal_voltage(self):
        """Ah x nominal voltage: a full battery's live 55.27 V would overstate it by ~8%."""
        import app
        live = {"battery_capacity_ah": 200, "battery_voltage": 55.27}
        assert app.battery_capacity_wh({**live, "battery_nominal_voltage": 51.2}) == pytest.approx(10240)
        assert app.battery_capacity_wh(live) == pytest.approx(11054)            # unknown nominal: live voltage
        assert app.battery_capacity_wh({**live, "battery_nominal_voltage": None}) == pytest.approx(11054)

    def test_drivers_report_nominal_voltage(self):
        from unittest.mock import patch
        from inverters import DeyeInverter, InverterConfig, InverterDriver
        assert InverterDriver().battery_nominal_voltage() == 51.2
        with patch("inverters.deye.PySolarmanV5"):
            inv = DeyeInverter(ip="192.168.1.1", serial=1, config=InverterConfig(phases=3, family="hybrid_3p_lv"))
            assert inv.battery_nominal_voltage() == 51.2
            inv.config = InverterConfig(phases=3, family="hybrid_3p_hv")
            assert inv.battery_nominal_voltage() is None                       # HV stacks vary

    def test_poller_averages_discharge_and_resets(self):
        import app
        poller = app.InverterPoller(inverter=None)
        for power in (-300, -500):        # average 400 W
            result = {**self.DATA, "battery_power": power}
            poller._add_runtime_estimate(result)
        assert result["battery_runtime_minutes"] == app.estimate_battery_runtime(self.DATA, 400)
        result = {**self.DATA, "battery_power": 1500}  # charging: average restarts with charge power
        poller._add_runtime_estimate(result)
        assert result["battery_runtime_minutes"] is None and poller._flow_samples == [1500]
        result = {**self.DATA, "battery_power": 0}     # idle: no estimates, nothing averaged
        poller._add_runtime_estimate(result)
        assert result["battery_charge_minutes"] is None and poller._flow_samples == []

    def test_charge_time(self):
        import app
        # 100 Ah x 53.2 V = 5320 Wh; (100 - 92)% = 425.6 Wh; at 3135 W -> 8 min
        assert app.estimate_battery_charge_time(self.DATA, 3135) == 8
        assert app.estimate_battery_charge_time({**self.DATA, "battery_soc": 100}, 3135) is None
        assert app.estimate_battery_charge_time(self.DATA, 0) is None

    def test_poller_charge_estimate_uses_average_charge_power(self):
        import app
        poller = app.InverterPoller(inverter=None)
        data = {**self.DATA, "battery_soc": 50}
        for power in (2000, 4000):        # average 3000 W
            result = {**data, "battery_power": power}
            poller._add_runtime_estimate(result)
        assert result["battery_charge_minutes"] == app.estimate_battery_charge_time(data, 3000)
        assert result["battery_runtime_minutes"] is None


class TestPollerKeepsGoodData:
    """A failed inverter poll keeps the last good readings (for up to INVERTER_CACHE_MAX_AGE)."""

    def _poller(self, results):
        import app
        from unittest.mock import MagicMock
        inv = MagicMock()
        inv.read_all_data.side_effect = results
        return app.InverterPoller(inverter=inv)

    def test_failed_poll_keeps_last_readings(self):
        poller = self._poller([{"battery_soc": 80, "battery_power": 0}, {"error": "No socket available"}])
        poller._fetch()
        poller._fetch()
        assert poller.data["battery_soc"] == 80 and "error" not in poller.data

    def test_error_shown_once_readings_are_too_old(self):
        import app
        from datetime import datetime, timedelta
        poller = self._poller([{"error": "No socket available"}])
        old = (datetime.now() - timedelta(seconds=app.INVERTER_CACHE_MAX_AGE + 5)).isoformat()
        poller._cache = {"battery_soc": 80, "last_updated": old}
        poller._fetch()
        assert poller.data["error"] == "No socket available"

    def test_error_without_previous_readings(self):
        poller = self._poller([{"error": "timeout"}])
        poller._fetch()
        assert poller.data["error"] == "timeout"

    def test_api_reports_data_age(self):
        import app
        from datetime import datetime, timedelta
        from unittest.mock import MagicMock, PropertyMock
        from inverters import InverterConfig
        app.app.config["TESTING"] = True
        orig = (app.inverter_poller, app.inverter_config, app._configured)
        try:
            poller = MagicMock()
            type(poller).data = PropertyMock(return_value={
                "battery_soc": 80, "last_updated": (datetime.now() - timedelta(seconds=42)).isoformat()})
            app.inverter_poller, app.inverter_config, app._configured = poller, InverterConfig(), True
            with app.app.test_client() as c:
                body = c.get("/api/data").get_json()
            assert 41 <= body["data_age_seconds"] <= 44
        finally:
            app.inverter_poller, app.inverter_config, app._configured = orig
