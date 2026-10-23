"""Tests for configuration API endpoints."""
import json
import pytest
from unittest.mock import patch, MagicMock

from inverters import InverterConfig


@pytest.fixture
def client():
    """Create a Flask test client with mocked pollers (configured mode)."""
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
    app_module.inverter_config = InverterConfig(phases=3, has_battery=True, pv_strings=2)
    app_module._configured = True
    app_module._pin_failures = 0
    app_module._pin_locked_until = 0.0

    with app_module.app.test_client() as c:
        # Settings endpoints need the PIN once configured (default 0000)
        c.environ_base["HTTP_X_SETTINGS_PIN"] = "0000"
        yield c, app_module

    # Restore originals
    app_module.inverter_poller = orig_inverter_poller
    app_module.outage_poller = orig_outage_poller
    app_module.weather_poller = orig_weather_poller
    app_module.inverter_config = orig_inverter_config
    app_module._configured = orig_configured


@pytest.fixture
def unconfigured_client():
    """Create a Flask test client in first-run (unconfigured) mode."""
    import app as app_module

    app_module.app.config["TESTING"] = True

    orig_configured = app_module._configured
    app_module._configured = False

    with app_module.app.test_client() as c:
        yield c, app_module

    app_module._configured = orig_configured


class TestConfigStatus:
    def test_configured_status(self, client):
        c, _ = client
        resp = c.get("/api/config/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["configured"] is True
        assert data["first_run"] is False

    def test_unconfigured_status(self, unconfigured_client):
        c, _ = unconfigured_client
        resp = c.get("/api/config/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["configured"] is False
        assert data["first_run"] is True

    def test_boot_id_matches_save_response(self, client):
        """The restart overlay reloads only once the status comes from a new start."""
        c, app_module = client
        boot = c.get("/api/config/status").get_json()["boot"]
        assert boot == app_module.BOOT_ID and len(boot) == 12
        with patch("setup.load_existing_env", return_value=({"INVERTER_IP": "1.2.3.4", "LOGGER_SERIAL": "1"}, [])), \
             patch("setup.write_env"), patch("threading.Timer"):
            assert c.post("/api/config", json={"INVERTER_IP": "1.2.3.5"}).get_json()["boot"] == boot

    def test_save_logs_key_names_not_values(self, client, caplog):
        c, _ = client
        with patch("setup.load_existing_env", return_value=({"INVERTER_IP": "1.2.3.4", "LOGGER_SERIAL": "1"}, [])), \
             patch("setup.write_env"), patch("threading.Timer"), caplog.at_level("INFO"):
            c.post("/api/config", json={"ALERTS_IN_UA_TOKEN": "a1b2c3d4e5f6a7b8c9d0e1f2"})
        assert "ALERTS_IN_UA_TOKEN" in caplog.text and "a1b2c3d4e5f6" not in caplog.text


class TestGetConfig:
    def test_returns_env_values(self, client):
        c, _ = client
        mock_values = {
            "INVERTER_IP": "192.168.1.100",
            "LOGGER_SERIAL": "123456",
            "WEATHER_LATITUDE": "50.4501",
        }
        with patch("setup.load_existing_env", return_value=(mock_values, [])):
            resp = c.get("/api/config")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["INVERTER_IP"] == "192.168.1.100"

    def test_masks_telegram_token(self, client):
        c, _ = client
        mock_values = {
            "TELEGRAM_BOT_TOKEN": "1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ",
        }
        with patch("setup.load_existing_env", return_value=(mock_values, [])):
            resp = c.get("/api/config")
        data = resp.get_json()
        token = data["TELEGRAM_BOT_TOKEN"]
        assert token.startswith("1234")
        assert token.endswith("wxYZ")
        assert "****" in token

    def test_short_token_fully_masked(self, client):
        c, _ = client
        mock_values = {"TELEGRAM_BOT_TOKEN": "short"}
        with patch("setup.load_existing_env", return_value=(mock_values, [])):
            resp = c.get("/api/config")
        data = resp.get_json()
        assert data["TELEGRAM_BOT_TOKEN"] == "****"

    def test_masks_alerts_in_ua_token(self, client):
        c, _ = client
        mock_values = {"ALERTS_IN_UA_TOKEN": "a1b2c3d4e5f6a7b8c9d0e1f2"}
        with patch("setup.load_existing_env", return_value=(mock_values, [])):
            data = c.get("/api/config").get_json()
        assert data["ALERTS_IN_UA_TOKEN"] == "a1b2****e1f2"


class TestSaveConfig:
    def test_saves_config_and_returns_ok(self, client):
        c, _ = client
        mock_existing = {"INVERTER_IP": "192.168.1.1", "LOGGER_SERIAL": "111"}
        with patch("setup.load_existing_env", return_value=(mock_existing, [])), \
             patch("setup.write_env") as mock_write, \
             patch("threading.Timer"):
            resp = c.post("/api/config", json={
                "INVERTER_IP": "10.0.0.5",
                "LOGGER_SERIAL": "999",
            })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["restarting"] is True
        # Check merged values
        written_values = mock_write.call_args[0][0]
        assert written_values["INVERTER_IP"] == "10.0.0.5"
        assert written_values["LOGGER_SERIAL"] == "999"

    def test_preserves_masked_token(self, client):
        c, _ = client
        mock_existing = {
            "INVERTER_IP": "192.168.1.1",
            "LOGGER_SERIAL": "2712345678",
            "TELEGRAM_BOT_TOKEN": "realtoken12345678",
        }
        with patch("setup.load_existing_env", return_value=(mock_existing, [])), \
             patch("setup.write_env") as mock_write, \
             patch("threading.Timer"):
            resp = c.post("/api/config", json={
                "INVERTER_IP": "192.168.1.1",
                "TELEGRAM_BOT_TOKEN": "real****5678",
            })
        assert resp.status_code == 200
        written_values = mock_write.call_args[0][0]
        assert written_values["TELEGRAM_BOT_TOKEN"] == "realtoken12345678"

    def test_preserves_masked_alerts_token(self, client):
        c, _ = client
        mock_existing = {"INVERTER_IP": "192.168.1.1", "LOGGER_SERIAL": "2712345678",
                         "ALERTS_IN_UA_TOKEN": "a1b2c3d4e5f6a7b8c9d0e1f2"}
        with patch("setup.load_existing_env", return_value=(mock_existing, [])), \
             patch("setup.write_env") as mock_write, \
             patch("threading.Timer"):
            resp = c.post("/api/config", json={"AIR_ALERTS_PROVIDER": "alerts_in_ua",
                                               "ALERTS_IN_UA_TOKEN": "a1b2****e1f2"})
        assert resp.status_code == 200
        written = mock_write.call_args[0][0]
        assert written["ALERTS_IN_UA_TOKEN"] == "a1b2c3d4e5f6a7b8c9d0e1f2"
        assert written["AIR_ALERTS_PROVIDER"] == "alerts_in_ua"

    def test_mask_without_saved_token_is_not_saved(self, client):
        c, _ = client
        mock_existing = {"INVERTER_IP": "192.168.1.1", "LOGGER_SERIAL": "2712345678"}
        with patch("setup.load_existing_env", return_value=(mock_existing, [])), \
             patch("setup.write_env") as mock_write, \
             patch("threading.Timer"):
            c.post("/api/config", json={"ALERTS_IN_UA_TOKEN": "a1b2****e1f2"})
        assert "ALERTS_IN_UA_TOKEN" not in mock_write.call_args[0][0]

    def test_rejects_invalid_body(self, client):
        c, _ = client
        resp = c.post("/api/config", data="not json",
                       content_type="application/json")
        assert resp.status_code == 400

    def test_preserves_extra_lines(self, client):
        c, _ = client
        extra = ["DEPLOY_HOST=pi.local", "DEPLOY_USER=pi"]
        existing = {"INVERTER_IP": "192.168.1.1", "LOGGER_SERIAL": "2712345678"}
        with patch("setup.load_existing_env", return_value=(existing, extra)), \
             patch("setup.write_env") as mock_write, \
             patch("threading.Timer"):
            resp = c.post("/api/config", json={"INVERTER_IP": "1.2.3.4"})
        assert resp.status_code == 200
        written_extra = mock_write.call_args[0][1]
        assert written_extra == extra


class TestConfigValidation:
    def _post(self, c, payload, existing=None):
        existing = existing if existing is not None else {"INVERTER_IP": "192.168.1.1", "LOGGER_SERIAL": "2712345678"}
        with patch("setup.load_existing_env", return_value=(existing, [])), \
             patch("setup.write_env") as mock_write, \
             patch("threading.Timer"):
            resp = c.post("/api/config", json=payload)
        return resp, mock_write

    def test_rejects_serial_over_32_bits(self, client):
        c, _ = client
        resp, mock_write = self._post(c, {"LOGGER_SERIAL": "23312345678"})
        assert resp.status_code == 400
        assert "LOGGER_SERIAL" in resp.get_json()["fields"]
        mock_write.assert_not_called()

    def test_rejects_line_breaks(self, client):
        """A newline would inject extra lines into .env (e.g. a new SETTINGS_PIN)."""
        c, _ = client
        resp, mock_write = self._post(c, {"OUTAGE_GROUP": "4.2\nSETTINGS_PIN=1111"})
        assert resp.status_code == 400
        mock_write.assert_not_called()

    def test_rejects_unknown_keys(self, client):
        c, _ = client
        resp, mock_write = self._post(c, {"SETTINGS_PIN": "1111"})
        assert resp.status_code == 400
        assert resp.get_json()["fields"] == {"SETTINGS_PIN": "unknown setting"}
        mock_write.assert_not_called()

    def test_rejects_bad_values(self, client):
        c, _ = client
        resp, _ = self._post(c, {"INVERTER_IP": "999.1.1.1", "WEATHER_LATITUDE": "north",
                                 "OUTAGE_PROVIDER": "acme", "GENERATOR_OIL_CHANGE_DATE": "17.10.2026"})
        assert resp.status_code == 400
        assert set(resp.get_json()["fields"]) == {
            "INVERTER_IP", "WEATHER_LATITUDE", "OUTAGE_PROVIDER", "GENERATOR_OIL_CHANGE_DATE"}

    def test_requires_ip_and_serial_after_merge(self, client):
        c, _ = client
        resp, mock_write = self._post(c, {"WEATHER_LATITUDE": "50.4"}, existing={})
        assert resp.status_code == 400
        mock_write.assert_not_called()

    def test_accepts_full_onboarding_payload(self, client):
        c, _ = client
        resp, mock_write = self._post(c, {
            "INVERTER_IP": "logger.local", "LOGGER_SERIAL": "2712345678",
            "WEATHER_LATITUDE": "50.4501", "WEATHER_LONGITUDE": "30.5234",
            "OUTAGE_PROVIDER": "dtek", "OUTAGE_DTEK_REGION": "kyiv-region", "OUTAGE_GROUP": "4.2",
            "INVERTER_HAS_GENERATOR": "true", "GENERATOR_FUEL_RATE": "2.5",
            "GENERATOR_OIL_CHANGE_DATE": "2026-01-15",
            "TELEGRAM_ENABLED": "true", "TELEGRAM_BOT_TOKEN": "123456:ABCdefGHIjklMNOpqrSTUvwx",
            "TELEGRAM_ALLOWED_USERS": "12345678, 87654321", "TELEGRAM_PUBLIC": "false",
        })
        assert resp.status_code == 200, resp.get_json()
        mock_write.assert_called_once()


class TestConfigDiscover:
    def test_returns_devices(self, client):
        c, _ = client
        mock_devices = [
            {"ip": "192.168.1.100", "model": "SUN-12K-SG04LP3"},
        ]
        with patch("discover_inverter.discover", return_value=mock_devices):
            resp = c.get("/api/config/discover")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["devices"]) == 1
        assert data["devices"][0]["ip"] == "192.168.1.100"

    def test_returns_empty_on_error(self, client):
        c, _ = client
        with patch("discover_inverter.discover", side_effect=Exception("network error")):
            resp = c.get("/api/config/discover")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["devices"] == []


class TestFirstRunGuards:
    def test_api_data_returns_503_when_unconfigured(self, unconfigured_client):
        c, _ = unconfigured_client
        resp = c.get("/api/data")
        assert resp.status_code == 503
        data = resp.get_json()
        assert data["error"] == "not configured"

    def test_api_weather_returns_503_when_unconfigured(self, unconfigured_client):
        c, _ = unconfigured_client
        resp = c.get("/api/weather")
        assert resp.status_code == 503

    def test_generator_returns_disabled_when_unconfigured(self, unconfigured_client):
        c, _ = unconfigured_client
        resp = c.get("/api/generator")
        data = resp.get_json()
        assert data["enabled"] is False

    def test_index_serves_page_when_unconfigured(self, unconfigured_client):
        c, _ = unconfigured_client
        resp = c.get("/")
        assert resp.status_code == 200


class TestIsConfigured:
    def test_unconfigured_defaults(self):
        from app import is_configured
        with patch.dict("os.environ", {"INVERTER_IP": "0.0.0.0", "LOGGER_SERIAL": "0"}):
            assert is_configured() is False

    def test_configured_values(self):
        from app import is_configured
        with patch.dict("os.environ", {"INVERTER_IP": "192.168.1.100", "LOGGER_SERIAL": "123456"}):
            assert is_configured() is True

    def test_empty_values(self):
        from app import is_configured
        with patch.dict("os.environ", {"INVERTER_IP": "", "LOGGER_SERIAL": ""}, clear=False):
            assert is_configured() is False

    def test_invalid_serial_means_setup_mode(self):
        from app import is_configured
        for serial in ("abc", "23312345678", "-5"):
            with patch.dict("os.environ", {"INVERTER_IP": "192.168.1.100", "LOGGER_SERIAL": serial}):
                assert is_configured() is False, serial

    def test_parse_logger_serial(self):
        from inverters.deye import parse_logger_serial
        assert parse_logger_serial("2712345678") == 2712345678
        assert parse_logger_serial(" 42 ") == 42
        assert parse_logger_serial("4294967296") == 0
        assert parse_logger_serial("x1") == 0
        assert parse_logger_serial(None) == 0

    def test_missing_values(self):
        from app import is_configured
        env = dict(__builtins__="") # dummy to use clear
        with patch.dict("os.environ", {}, clear=True):
            assert is_configured() is False


class TestSettingsPin:
    @pytest.fixture(autouse=True)
    def _default_pin(self, monkeypatch):
        monkeypatch.delenv("SETTINGS_PIN", raising=False)

    def _no_pin(self, c):
        c.environ_base.pop("HTTP_X_SETTINGS_PIN", None)

    def test_verify_default_pin(self, client):
        c, _ = client
        resp = c.post("/api/settings/verify-pin", json={"pin": "0000"})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"

    def test_verify_wrong_pin(self, client):
        c, _ = client
        resp = c.post("/api/settings/verify-pin", json={"pin": "1234"})
        assert resp.status_code == 401

    def test_verify_custom_pin_from_env(self, client, monkeypatch):
        c, _ = client
        monkeypatch.setenv("SETTINGS_PIN", "4821")
        assert c.post("/api/settings/verify-pin", json={"pin": "0000"}).status_code == 401
        assert c.post("/api/settings/verify-pin", json={"pin": "4821"}).status_code == 200

    def test_lockout_after_repeated_failures(self, client):
        c, app_module = client
        for _ in range(app_module.PIN_MAX_ATTEMPTS - 1):
            assert c.post("/api/settings/verify-pin", json={"pin": "9999"}).status_code == 401
        resp = c.post("/api/settings/verify-pin", json={"pin": "9999"})
        assert resp.status_code == 429
        assert resp.get_json()["retry_after"] > 0
        # Even the right PIN is refused while locked out
        assert c.post("/api/settings/verify-pin", json={"pin": "0000"}).status_code == 429

    @pytest.mark.parametrize("method,url", [
        ("get", "/api/config"),
        ("post", "/api/config"),
        ("post", "/api/config/reset"),
        ("get", "/api/config/discover"),
        ("post", "/api/phase-stats/clear"),
    ])
    def test_settings_endpoints_require_pin(self, client, method, url):
        c, _ = client
        self._no_pin(c)
        resp = getattr(c, method)(url, json={})
        assert resp.status_code == 401

    def test_wrong_pin_header_rejected(self, client):
        c, _ = client
        c.environ_base["HTTP_X_SETTINGS_PIN"] = "1111"
        assert c.get("/api/config").status_code == 401

    def test_first_run_does_not_need_pin(self, unconfigured_client):
        c, _ = unconfigured_client
        with patch("setup.load_existing_env", return_value=({}, [])):
            resp = c.get("/api/config")
        assert resp.status_code == 200


class TestRestart:
    def test_systemctl_success_does_not_exit(self):
        import app as app_module
        with patch("app.subprocess.run") as mock_run, patch("app.os._exit") as mock_exit:
            app_module._restart_now()
        assert mock_run.call_args[0][0][:2] == ["sudo", "-n"]
        mock_exit.assert_not_called()

    @pytest.mark.parametrize("env,service", [({}, "inverter-dashboard"),
                                             ({"DEPLOY_SERVICE_NAME": "deye-dashboard"}, "deye-dashboard")])
    def test_restarts_the_configured_service(self, env, service, monkeypatch):
        """deploy.sh's service name (from .env); installs that kept the old name still restart."""
        import app as app_module
        monkeypatch.delenv("DEPLOY_SERVICE_NAME", raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        with patch("app.subprocess.run") as mock_run, patch("app.os._exit"):
            app_module._restart_now()
        assert mock_run.call_args[0][0] == ["sudo", "-n", "systemctl", "restart", service]

    def test_falls_back_to_nonzero_exit(self):
        import app as app_module
        import subprocess
        for error in (FileNotFoundError("sudo"), subprocess.CalledProcessError(1, "sudo")):
            with patch("app.subprocess.run", side_effect=error), patch("app.os._exit") as mock_exit:
                app_module._restart_now()
            mock_exit.assert_called_once_with(3)
