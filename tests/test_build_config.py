"""Inverter config at startup: INVERTER_* env values on top of auto-detection."""
import pytest
from unittest.mock import patch, MagicMock
from inverters import InverterConfig, DeyeInverter


class TestEnvAndDetection:
    def _build(self, env_vars, detect_result=None, detect_raises=False):
        """The config the dashboard runs with after startup (as in init_services):
        initial_inverter_config(), then full auto-detect, or only the device type
        when every capability is set in env."""
        with patch("inverters.deye.PySolarmanV5"):
            inv = DeyeInverter(ip="192.168.1.1", serial=123456)
            inv.inverter = MagicMock()
            inv.read_device_type = MagicMock(return_value=(None, "unknown"))

            if detect_raises:
                inv.detect_config = MagicMock(side_effect=Exception("timeout"))
            elif detect_result:
                inv.detect_config = MagicMock(return_value=detect_result)
            else:
                inv.detect_config = MagicMock(
                    return_value=InverterConfig(phases=3, has_battery=True, pv_strings=2, has_generator=False)
                )

            with patch.dict("os.environ", env_vars, clear=False):
                # Remove any existing env vars that might interfere
                for key in ["INVERTER_PHASES", "INVERTER_HAS_BATTERY",
                            "INVERTER_PV_STRINGS", "INVERTER_HAS_GENERATOR"]:
                    if key not in env_vars:
                        import os
                        os.environ.pop(key, None)

                import app
                config = app.initial_inverter_config()
                detect = app.refresh_device_type if app.env_config_complete() else app.refresh_inverter_config
                detect(inv, config)
                return config, inv

    def test_all_env_vars_skips_detect(self):
        env = {
            "INVERTER_PHASES": "1",
            "INVERTER_HAS_BATTERY": "true",
            "INVERTER_PV_STRINGS": "1",
            "INVERTER_HAS_GENERATOR": "false",
        }
        config, inv = self._build(env)
        assert config.phases == 1
        assert config.has_battery is True
        assert config.pv_strings == 1
        assert config.has_generator is False
        inv.detect_config.assert_not_called()

    def test_partial_env_falls_back(self):
        env = {"INVERTER_PHASES": "1"}
        detected = InverterConfig(phases=3, has_battery=True, pv_strings=2, has_generator=True)
        config, inv = self._build(env, detect_result=detected)
        # phases from env, rest from detect
        assert config.phases == 1
        assert config.has_battery is True
        assert config.pv_strings == 2
        assert config.has_generator is True
        inv.detect_config.assert_called_once()

    def test_no_env_auto_detects(self):
        detected = InverterConfig(phases=3, has_battery=True, pv_strings=2, has_generator=False)
        config, inv = self._build({}, detect_result=detected)
        assert config.phases == 3
        assert config.has_battery is True
        assert config.pv_strings == 2
        inv.detect_config.assert_called_once()

    def test_detect_failure_uses_defaults(self):
        config, inv = self._build({}, detect_raises=True)
        # Should fall back to InverterConfig() defaults
        assert config.phases == 3
        assert config.has_battery is True
        assert config.pv_strings == 2
        assert config.has_generator is False

    def test_generator_env_true(self):
        env = {
            "INVERTER_PHASES": "3",
            "INVERTER_HAS_BATTERY": "true",
            "INVERTER_PV_STRINGS": "2",
            "INVERTER_HAS_GENERATOR": "true",
        }
        config, _ = self._build(env)
        assert config.has_generator is True

    def test_generator_env_false(self):
        env = {
            "INVERTER_PHASES": "3",
            "INVERTER_HAS_BATTERY": "true",
            "INVERTER_PV_STRINGS": "2",
            "INVERTER_HAS_GENERATOR": "false",
        }
        config, _ = self._build(env)
        assert config.has_generator is False

    def test_boolean_variants(self):
        """'yes', '1', 'True' should all work as truthy."""
        for val in ("yes", "1", "True"):
            env = {
                "INVERTER_PHASES": "3",
                "INVERTER_HAS_BATTERY": val,
                "INVERTER_PV_STRINGS": "2",
                "INVERTER_HAS_GENERATOR": val,
            }
            config, _ = self._build(env)
            assert config.has_battery is True, f"Failed for {val}"
            assert config.has_generator is True, f"Failed for {val}"


class TestNonBlockingConfig:
    """initial_inverter_config() / refresh_inverter_config(): startup without blocking detection."""

    @pytest.fixture(autouse=True)
    def env(self, monkeypatch, tmp_path):
        for key in ["INVERTER_PHASES", "INVERTER_HAS_BATTERY", "INVERTER_PV_STRINGS", "INVERTER_HAS_GENERATOR"]:
            monkeypatch.delenv(key, raising=False)
        import app as app_module
        monkeypatch.setattr(app_module, "INVERTER_CONFIG_FILE", str(tmp_path / "inverter_config.json"))
        return app_module

    def test_defaults_without_cache(self, env):
        assert env.initial_inverter_config() == InverterConfig()

    def test_uses_cached_detection(self, env):
        env.atomic_write_json(env.INVERTER_CONFIG_FILE,
                              {"phases": 1, "has_battery": False, "pv_strings": 1, "has_generator": True})
        assert env.initial_inverter_config() == InverterConfig(phases=1, has_battery=False,
                                                               pv_strings=1, has_generator=True)

    def test_env_overrides_cache(self, env, monkeypatch):
        env.atomic_write_json(env.INVERTER_CONFIG_FILE,
                              {"phases": 1, "has_battery": False, "pv_strings": 1, "has_generator": False})
        monkeypatch.setenv("INVERTER_HAS_BATTERY", "true")
        assert env.initial_inverter_config().has_battery is True

    def test_refresh_updates_in_place_and_saves(self, env):
        config = InverterConfig()  # defaults: 3-phase
        inv = MagicMock()
        inv.detect_config.return_value = InverterConfig(phases=1, has_battery=True, pv_strings=1)
        env.refresh_inverter_config(inv, config)
        assert config.phases == 1 and config.pv_strings == 1  # same object, updated
        assert env.load_cached_inverter_config() == InverterConfig(phases=1, has_battery=True, pv_strings=1)

    def test_failed_detection_keeps_config_and_cache(self, env):
        env.atomic_write_json(env.INVERTER_CONFIG_FILE,
                              {"phases": 1, "has_battery": True, "pv_strings": 2, "has_generator": False})
        config = env.initial_inverter_config()
        inv = MagicMock()
        inv.detect_config.side_effect = Exception("No socket available")
        env.refresh_inverter_config(inv, config)
        assert config.phases == 1
        assert env.load_cached_inverter_config().phases == 1

    def test_invalid_cache_ignored(self, env):
        with open(env.INVERTER_CONFIG_FILE, "w") as f:
            f.write('{"phases": "three"}')
        assert env.initial_inverter_config() == InverterConfig()


class TestDeviceTypePlumbing:
    def test_env_overrides_keep_detected_family(self, monkeypatch):
        import app
        from inverters import InverterConfig
        monkeypatch.setenv("INVERTER_PHASES", "3")
        result = app.apply_env_overrides(InverterConfig(phases=1, device_type=0x0006, family="hybrid_3p_hv"))
        assert result.phases == 3 and result.family == "hybrid_3p_hv" and result.device_type == 6

    def test_cached_config_roundtrip(self, tmp_path, monkeypatch):
        import app
        from inverters import InverterConfig
        monkeypatch.setattr(app, "INVERTER_CONFIG_FILE", str(tmp_path / "c.json"))
        app.atomic_write_json(app.INVERTER_CONFIG_FILE, InverterConfig(device_type=5, family="hybrid_3p_lv").to_dict())
        cached = app.load_cached_inverter_config()
        assert (cached.device_type, cached.family) == (5, "hybrid_3p_lv")

    def test_old_cache_without_family_still_loads(self, tmp_path, monkeypatch):
        import app
        monkeypatch.setattr(app, "INVERTER_CONFIG_FILE", str(tmp_path / "c.json"))
        app.atomic_write_json(app.INVERTER_CONFIG_FILE,
                              {"phases": 3, "has_battery": True, "pv_strings": 2, "has_generator": False})
        cached = app.load_cached_inverter_config()
        assert cached.family == "unknown" and cached.device_type is None

    def test_refresh_device_type_updates_config_in_place(self):
        import app
        from unittest.mock import MagicMock
        from inverters import InverterConfig
        config = InverterConfig()
        inv = MagicMock()
        inv.read_device_type.return_value = (0x0005, "hybrid_3p_lv")
        inv.family_supported.return_value = True
        app.refresh_device_type(inv, config)
        assert (config.device_type, config.family) == (5, "hybrid_3p_lv")
