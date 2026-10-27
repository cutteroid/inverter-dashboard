"""Tests for TelegramBot robustness: error isolation, cached data, alerts, retries."""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

import telegram_bot
from inverters import InverterConfig
from telegram_bot import TelegramBot


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram_bot, "BATTERY_REPORT_DIR", str(tmp_path / "reports"))
    inverter = MagicMock()
    inverter.config = InverterConfig(phases=3, has_battery=True)
    poller = MagicMock()
    b = TelegramBot(token="123:abc", allowed_users={1}, inverter=inverter,
                    state_file=str(tmp_path / "state.json"), inverter_poller=poller)
    b.broadcast = MagicMock()
    b.send_message = MagicMock(return_value=True)
    return b


def _poller_data(bot, age_seconds=10, **values):
    data = {"last_updated": (datetime.now() - timedelta(seconds=age_seconds)).isoformat(),
            "battery_voltage": 52.0, "battery_soc": 80, "grid_voltage": 230}
    data.update(values)
    type(bot.inverter_poller).data = PropertyMock(return_value=data)


class TestErrorIsolation:
    def test_failing_step_does_not_raise(self, bot):
        def boom():
            raise RuntimeError("handler bug")
        boom.__name__ = "boom"
        bot._run_step(boom)  # must not raise

    def test_failing_handler_does_not_block_other_updates(self, bot):
        updates = [
            {"update_id": 10, "message": {"text": "/battery", "chat": {"id": 1}, "from": {"id": 1}}},
            {"update_id": 11, "message": {"text": "/start", "chat": {"id": 1}, "from": {"id": 1}}},
        ]
        resp = MagicMock(ok=True)
        resp.json.return_value = {"result": updates}
        bot._handle_battery = MagicMock(side_effect=RuntimeError("bug"))
        bot._handle_start = MagicMock()
        with patch("telegram_bot.requests.get", return_value=resp):
            bot.poll_commands()
        bot._handle_start.assert_called_once()
        assert bot.last_update_id == 11  # crashing update is not replayed


class TestCachedInverterData:
    def test_uses_poller_without_reading_inverter(self, bot):
        _poller_data(bot, battery_soc=55)
        assert bot._get_inverter_data()["battery_soc"] == 55
        bot.inverter.read_all_data.assert_not_called()

    def test_stale_data_is_an_error(self, bot):
        _poller_data(bot, age_seconds=telegram_bot.INVERTER_DATA_MAX_AGE + 60)
        assert bot._get_inverter_data()["error"]

    def test_no_data_yet(self, bot):
        type(bot.inverter_poller).data = PropertyMock(return_value=None)
        assert bot._get_inverter_data()["error"]


class TestLowBatteryHysteresis:
    def test_alert_once_until_rearm_level(self, bot):
        for soc, expected_alerts in [(29, 1), (30, 1), (29, 1), (34, 1), (35, 1), (28, 2)]:
            _poller_data(bot, battery_soc=soc)
            bot.check_inverter()
            assert bot.broadcast.call_count == expected_alerts, f"SOC {soc}"


class TestReportRotation:
    def test_keeps_newest_reports(self, bot, monkeypatch):
        monkeypatch.setattr(telegram_bot, "BATTERY_REPORTS_KEEP", 3)
        import os
        os.makedirs(telegram_bot.BATTERY_REPORT_DIR)
        for i in range(5):
            open(os.path.join(telegram_bot.BATTERY_REPORT_DIR, f"2026-10-17_10-00-0{i}_x.json"), "w").close()
        bot._prune_battery_reports()
        assert sorted(os.listdir(telegram_bot.BATTERY_REPORT_DIR)) == [
            "2026-10-17_10-00-02_x.json", "2026-10-17_10-00-03_x.json", "2026-10-17_10-00-04_x.json"]


class TestSendRetries:
    @pytest.fixture
    def real_send(self, tmp_path):
        return TelegramBot(token="123:abc", allowed_users={1}, inverter=MagicMock(),
                           state_file=str(tmp_path / "s.json"))

    def test_client_error_is_not_retried(self, real_send):
        resp = MagicMock(ok=False, status_code=403, text="Forbidden: bot was blocked by the user")
        with patch("telegram_bot.requests.post", return_value=resp) as post, \
             patch("telegram_bot.time.sleep") as sleep:
            assert real_send.send_message(1, "hi") is False
        assert post.call_count == 1
        sleep.assert_not_called()

    def test_rate_limit_waits_retry_after(self, real_send):
        limited = MagicMock(ok=False, status_code=429, text="Too Many Requests")
        limited.json.return_value = {"parameters": {"retry_after": 7}}
        ok = MagicMock(ok=True)
        with patch("telegram_bot.requests.post", side_effect=[limited, ok]), \
             patch("telegram_bot.time.sleep") as sleep:
            assert real_send.send_message(1, "hi") is True
        sleep.assert_called_once_with(7)

    def test_server_error_is_retried(self, real_send):
        err = MagicMock(ok=False, status_code=502, text="Bad Gateway")
        ok = MagicMock(ok=True)
        with patch("telegram_bot.requests.post", side_effect=[err, ok]) as post, \
             patch("telegram_bot.time.sleep"):
            assert real_send.send_message(1, "hi") is True
        assert post.call_count == 2


class TestStateWrites:
    def test_state_written_only_on_change(self, bot):
        with patch("telegram_bot.atomic_write_json") as write:
            bot._save_state()
            bot._save_state()
            assert write.call_count == 1
            bot.battery_low_notified = True
            bot._save_state()
            assert write.call_count == 2


class TestGridRelay:
    """grid_online (inverter's grid relay) wins over the L1 voltage."""

    def test_relay_open_with_voltage_counts_as_down(self, bot):
        _poller_data(bot, grid_voltage=240, grid_online=False)  # e.g. a phase cut by the smart meter
        bot._broadcast_grid_down = MagicMock()
        with patch("telegram_bot.time.time", return_value=1000.0):
            bot.check_inverter()
        with patch("telegram_bot.time.time", return_value=1000.0 + 121):
            bot.check_inverter()
        bot._broadcast_grid_down.assert_called_once()

    def test_without_grid_online_falls_back_to_voltage(self, bot):
        _poller_data(bot, grid_voltage=230)
        bot._broadcast_grid_down = MagicMock()
        with patch("telegram_bot.time.time", return_value=1000.0):
            bot.check_inverter()
        assert bot.grid_down_since is None


class TestOutageForecast:
    """/outage before a planned outage: the battery verdict is the dashboard's
    (outage_readiness), not a guess from a fixed 16 kWh battery."""

    def _upcoming(self, bot):
        start = datetime.now().replace(second=0, microsecond=0) + timedelta(hours=2)
        bot.outage_poller = MagicMock()
        bot.outage_poller.get_outage_status.return_value = {
            "status": "upcoming", "upcoming_windows": [(start, start + timedelta(hours=3))]}
        bot.outage_poller.provider = MagicMock(display_name="ДТЕК")
        bot.allowed_users = {1}

    @pytest.mark.parametrize("verdict, messages", [
        ("enough", telegram_bot.MESSAGES_UPCOMING_BATTERY_OK),
        ("tight", telegram_bot.MESSAGES_UPCOMING_BATTERY_TIGHT),
        ("short", telegram_bot.MESSAGES_UPCOMING_BATTERY_LOW),
    ])
    def test_uses_the_dashboards_verdict(self, bot, verdict, messages):
        self._upcoming(bot)
        bot.readiness = lambda: {"status": "ok", "verdict": verdict, "soc": 64, "average_load_w": 812}
        bot._format_poem = lambda: ""
        bot._handle_outage(1, 1)
        text = bot.send_message.call_args[0][1]
        firsts = [m.split("<b>")[0] for m in messages]
        assert any(text.startswith(f) for f in firsts)
        assert "64%" in text

    def test_without_a_verdict_only_the_facts(self, bot):
        self._upcoming(bot)
        _poller_data(bot, battery_soc=55)
        bot.readiness = lambda: {"status": "no_data"}
        bot._format_poem = lambda: ""
        bot._handle_outage(1, 1)
        text = bot.send_message.call_args[0][1]
        assert text.startswith("Увага! Відключення заплановано") and "55%" in text
