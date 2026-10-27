"""Tests for setup.py load_existing_env and write_env."""
import os
import pytest

from setup import load_existing_env, write_env


class TestLoadExistingEnv:
    def test_no_file_returns_empty(self, tmp_path):
        values, extra = load_existing_env(str(tmp_path / "nonexistent.env"))
        assert values == {}
        assert extra == []

    def test_parses_managed_keys(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("INVERTER_IP=192.168.1.100\nLOGGER_SERIAL=12345\n")
        values, extra = load_existing_env(str(env_file))
        assert values["INVERTER_IP"] == "192.168.1.100"
        assert values["LOGGER_SERIAL"] == "12345"
        assert extra == []

    def test_extra_keys_in_extra_lines(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "INVERTER_IP=1.2.3.4\n"
            "CUSTOM_KEY=custom_value\n"
        )
        values, extra = load_existing_env(str(env_file))
        assert "INVERTER_IP" in values
        assert "CUSTOM_KEY" not in values
        assert "CUSTOM_KEY=custom_value" in extra

    def test_comments_and_blanks_ignored(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "# This is a comment\n"
            "\n"
            "INVERTER_IP=1.2.3.4\n"
            "  \n"
            "# Another comment\n"
        )
        values, extra = load_existing_env(str(env_file))
        assert values == {"INVERTER_IP": "1.2.3.4"}
        assert extra == []


class TestWriteEnv:
    def test_basic_output(self, tmp_path):
        env_file = str(tmp_path / ".env")
        values = {
            "INVERTER_IP": "192.168.1.100",
            "LOGGER_SERIAL": "12345",
            "WEATHER_LATITUDE": "49.84",
            "WEATHER_LONGITUDE": "24.03",
            "OUTAGE_PROVIDER": "lvivoblenergo",
            "OUTAGE_GROUP": "4.1",
            "TELEGRAM_ENABLED": "false",
        }
        write_env(values, [], path=env_file)
        content = open(env_file).read()
        assert "INVERTER_IP=192.168.1.100" in content
        assert "LOGGER_SERIAL=12345" in content
        assert "OUTAGE_PROVIDER=lvivoblenergo" in content
        assert "OUTAGE_GROUP=4.1" in content

    def test_generator_section_when_enabled(self, tmp_path):
        env_file = str(tmp_path / ".env")
        values = {
            "INVERTER_IP": "1.2.3.4",
            "LOGGER_SERIAL": "99",
            "OUTAGE_PROVIDER": "none",
            "TELEGRAM_ENABLED": "false",
            "INVERTER_HAS_GENERATOR": "true",
            "GENERATOR_FUEL_RATE": "2.5",
        }
        write_env(values, [], path=env_file)
        content = open(env_file).read()
        assert "INVERTER_HAS_GENERATOR=true" in content
        assert "GENERATOR_FUEL_RATE=2.5" in content
        assert "# Generator" in content

    def test_no_generator_is_written(self, tmp_path):
        """"No generator" must be written: otherwise auto-detect decides, and it
        keeps a generator it found once."""
        env_file = str(tmp_path / ".env")
        values = {
            "INVERTER_IP": "1.2.3.4",
            "LOGGER_SERIAL": "99",
            "OUTAGE_PROVIDER": "none",
            "TELEGRAM_ENABLED": "false",
            "INVERTER_HAS_GENERATOR": "false",
            "GENERATOR_FUEL_RATE": "2.5",
        }
        write_env(values, [], path=env_file)
        content = open(env_file).read()
        assert "INVERTER_HAS_GENERATOR=false" in content
        assert "GENERATOR_FUEL_RATE" not in content

    def test_generator_not_set_is_left_to_auto_detect(self, tmp_path):
        env_file = str(tmp_path / ".env")
        values = {"INVERTER_IP": "1.2.3.4", "LOGGER_SERIAL": "99", "OUTAGE_PROVIDER": "none",
                  "TELEGRAM_ENABLED": "false"}
        write_env(values, [], path=env_file)
        content = open(env_file).read()
        assert "INVERTER_HAS_GENERATOR" not in content and "# Generator" not in content

    def test_extra_lines_appended(self, tmp_path):
        env_file = str(tmp_path / ".env")
        values = {
            "INVERTER_IP": "1.2.3.4",
            "LOGGER_SERIAL": "99",
            "OUTAGE_PROVIDER": "none",
            "TELEGRAM_ENABLED": "false",
        }
        extra = ["DEPLOY_HOST=server.example.com", "DEPLOY_USER=admin"]
        write_env(values, extra, path=env_file)
        content = open(env_file).read()
        assert "# Additional settings" in content
        assert "DEPLOY_HOST=server.example.com" in content
        assert "DEPLOY_USER=admin" in content


class TestInverterPhases:
    def test_written_when_chosen_omitted_for_auto(self, tmp_path):
        from setup import write_env
        path = tmp_path / ".env"
        write_env({"INVERTER_IP": "1.2.3.4", "LOGGER_SERIAL": "1", "INVERTER_PHASES": "3"}, [], str(path))
        assert "INVERTER_PHASES=3" in path.read_text()
        write_env({"INVERTER_IP": "1.2.3.4", "LOGGER_SERIAL": "1", "INVERTER_PHASES": ""}, [], str(path))
        assert "INVERTER_PHASES" not in path.read_text()  # auto-detect

    def test_validation(self):
        from setup import validate_config
        assert validate_config({"INVERTER_PHASES": "1"}) == {}
        assert validate_config({"INVERTER_PHASES": ""}) == {}
        assert "INVERTER_PHASES" in validate_config({"INVERTER_PHASES": "2"})

    def test_existing_manual_value_is_managed(self, tmp_path):
        from setup import load_existing_env
        path = tmp_path / ".env"
        path.write_text("INVERTER_IP=1.2.3.4\nINVERTER_PHASES=3\n")
        values, extra = load_existing_env(str(path))
        assert values["INVERTER_PHASES"] == "3" and extra == []


class TestDashboardTitle:
    def test_validation(self):
        from setup import validate_config
        assert validate_config({"DASHBOARD_TITLE": "Дача Іванових"}) == {}
        assert validate_config({"DASHBOARD_TITLE": ""}) == {}                 # default name
        for bad in ("x" * 41, "Home #1", 'Say "hi"', "it's", "back\\slash", "two\nlines"):
            assert "DASHBOARD_TITLE" in validate_config({"DASHBOARD_TITLE": bad}), bad

    def test_written_only_when_set(self, tmp_path):
        from setup import load_existing_env, write_env
        path = tmp_path / ".env"
        base = {"INVERTER_IP": "1.2.3.4", "LOGGER_SERIAL": "1"}
        write_env({**base, "DASHBOARD_TITLE": "Дача Іванових"}, [], str(path))
        assert load_existing_env(str(path))[0]["DASHBOARD_TITLE"] == "Дача Іванових"
        write_env({**base, "DASHBOARD_TITLE": ""}, [], str(path))
        assert "DASHBOARD_TITLE" not in path.read_text()


class TestAirAlertsSettings:
    def test_validation(self):
        from setup import validate_config
        assert validate_config({"AIR_ALERTS_PROVIDER": "none", "AIR_ALERTS_REGION": "UA-32"}) == {}
        assert validate_config({"AIR_ALERTS_REGION": ""}) == {}          # whole Ukraine
        errors = validate_config({"AIR_ALERTS_PROVIDER": "acme", "AIR_ALERTS_REGION": "Kyiv"})
        assert set(errors) == {"AIR_ALERTS_PROVIDER", "AIR_ALERTS_REGION"}

    def test_alerts_in_ua_token(self):
        from setup import validate_config
        assert validate_config({"AIR_ALERTS_PROVIDER": "alerts_in_ua",
                                "ALERTS_IN_UA_TOKEN": "a1b2c3d4e5f6a7b8c9d0e1f2"}) == {}
        assert validate_config({"ALERTS_IN_UA_TOKEN": "a1b2****e1f2"}) == {}   # masked: keep saved
        assert validate_config({"ALERTS_IN_UA_TOKEN": ""}) == {}               # optional
        for bad in ("short", "has space in it", "quote\"d-token-1234", "hash#token-12345"):
            assert "ALERTS_IN_UA_TOKEN" in validate_config({"ALERTS_IN_UA_TOKEN": bad}), bad

    def test_token_written_with_alerts_settings(self, tmp_path):
        from setup import load_existing_env, write_env
        path = tmp_path / ".env"
        base = {"INVERTER_IP": "1.2.3.4", "LOGGER_SERIAL": "1"}
        write_env({**base, "AIR_ALERTS_PROVIDER": "alerts_in_ua",
                   "ALERTS_IN_UA_TOKEN": "a1b2c3d4e5f6a7b8c9d0e1f2"}, [], str(path))
        values, extra = load_existing_env(str(path))
        assert values["ALERTS_IN_UA_TOKEN"] == "a1b2c3d4e5f6a7b8c9d0e1f2" and extra == []

    def test_written_only_when_set(self, tmp_path):
        from setup import write_env
        path = tmp_path / ".env"
        base = {"INVERTER_IP": "1.2.3.4", "LOGGER_SERIAL": "1"}
        write_env({**base, "AIR_ALERTS_PROVIDER": "neptun", "AIR_ALERTS_REGION": "UA-32"}, [], str(path))
        text = path.read_text()
        assert "AIR_ALERTS_PROVIDER=neptun" in text and "AIR_ALERTS_REGION=UA-32" in text
        write_env({**base, "AIR_ALERTS_PROVIDER": "neptun", "AIR_ALERTS_REGION": ""}, [], str(path))
        assert "AIR_ALERTS_REGION" not in path.read_text()

    def test_existing_manual_provider_is_managed(self, tmp_path):
        from setup import load_existing_env
        path = tmp_path / ".env"
        path.write_text("INVERTER_IP=1.2.3.4\nAIR_ALERTS_PROVIDER=mock\n")
        values, extra = load_existing_env(str(path))
        assert values["AIR_ALERTS_PROVIDER"] == "mock" and extra == []
