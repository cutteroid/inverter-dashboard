"""Tests for the Bakler smart meter MQTT integration."""
import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from smart_meter import OFFLINE_GRACE, STALE_AFTER, SmartMeter, SmartMeterClient, create_smart_meter_from_env

# Payloads from https://www.bakler.com.ua/setting-mqtt-client/
SINGLE_PHASE_JSON = {
    "ver": "1.2.0", "time": 1711730400, "posWh": 35820.0, "negWh": 0.000, "posVarh": 0.080, "negVarh": 0.000,
    "Vmin": 223.2, "V": 225.3, "Vmax": 226.4, "Imin": 0.000, "I": 1.25, "Imax": 2.0,
    "Pmin": 0.0, "Pmax": 450.0, "Qmin": 0.0, "Qmax": 0.0, "Hzmin": 49.96, "Hzmax": 50.00, "Tbox": 33.00, "relay": 1,
}
THREE_PHASE_JSON = {
    "ver": "3.2.0", "time": 1632420300,
    "posWh": [10169.896, 11000.0, 12168.816], "negWh": [0.0, 0.0, 0.0],
    "posVarh": [30.04, 31.0, 32.04], "negVarh": [40.0, 41.04, 42.0], "T": [-60.0, -60.0, -60.0],
    "Vmin": [216.5, 217.5, 215.5], "V": [218.6, 219.7, 217.6], "Vmax": [223.1, 224.2, 222.1],
    "Imin": [0.0, 0.0, 0.0], "I": [1.0, 2.0, 3.0], "Imax": [0.0, 0.0, 0.0],
    "Pmin": [0.0, 0.0, 0.0], "Pmax": [0.0, 0.0, 0.0], "Qmin": [0.0, 0.0, 0.0], "Qmax": [0.0, 0.0, 0.0],
    "Hzmin": 49.97, "Hzmax": 50.01, "Tbox": 30.75, "relay": [0, 100, 100, 100, 100, 100],
}


@pytest.fixture
def meter(tmp_path):
    return SmartMeter(daily_file=str(tmp_path / "daily.json"))


def test_waiting_until_first_message(meter):
    meter.set_broker_state(True)
    assert meter.get_status() == {"status": "waiting"}


def test_waiting_before_first_connection_attempt(meter):
    """Right after a restart the broker connection isn't up yet: not an error."""
    assert meter.get_status() == {"status": "waiting"}


def test_broker_error_reported(meter):
    meter.set_broker_state(False, "Cannot connect to MQTT broker mosquitto:1883")
    assert meter.get_status() == {"status": "error", "error": "Cannot connect to MQTT broker mosquitto:1883"}


def test_single_phase_json_and_instant_power(meter):
    meter.handle_message("buckler/772_ABC/status", b"online")
    meter.handle_message("buckler/772_ABC/dataJson", json.dumps(SINGLE_PHASE_JSON).encode())
    status = meter.get_status()
    assert status["status"] == "ok" and status["serial"] == "772_ABC" and status["phases"] == 1
    assert status["voltage"] == [225.3] and status["current"] == [1.25]
    assert status["power_w"] == 450.0  # no instantaneous P yet: falls back to Pmax
    assert status["energy_consumed_kwh"] == pytest.approx(35.82)
    assert status["frequency_max"] == 50.0
    assert status["relays"] == [1]
    meter.handle_message("buckler/772_ABC/data/P", b"312.5")
    meter.handle_message("buckler/772_ABC/data/V", b"226.1")
    status = meter.get_status()
    assert status["power_w"] == 312.5 and status["voltage"] == [226.1]


def test_three_phase(meter):
    meter.handle_message("buckler/64_XYZ/dataJson", json.dumps(THREE_PHASE_JSON))
    for i, p in enumerate((100, 200, 300), start=1):
        meter.handle_message(f"buckler/64_XYZ/data/P{i}", str(p))
    meter.handle_message("buckler/64_XYZ/relay/r1", "0")
    status = meter.get_status()
    assert status["phases"] == 3
    assert status["voltage"] == [218.6, 219.7, 217.6]
    assert status["current"] == [1.0, 2.0, 3.0]
    assert status["power_phases"] == [100.0, 200.0, 300.0] and status["power_w"] == 600.0
    assert status["energy_consumed_kwh"] == pytest.approx((10169.896 + 11000.0 + 12168.816) / 1000)
    assert status["relays"] == [0, 100, 100, 100, 100, 100]


def test_phase_status_from_voltage(meter):
    meter.handle_message("buckler/64_XYZ/dataJson", json.dumps(THREE_PHASE_JSON))
    assert meter.get_status()["phases_on"] == [True, True, True]
    meter.handle_message("buckler/64_XYZ/data/V2", "0.3")   # L2 lost supply
    assert meter.get_status()["phases_on"] == [True, False, True]


def test_phase_cut_by_meter_relay(meter):
    """3 relays on a 3-phase meter = one per phase; the meter still measures the grid side."""
    meter.handle_message("buckler/3366/dataJson", json.dumps({**THREE_PHASE_JSON, "V": [246.2, 256.4, 241.1],
                                                               "relay": [100, 0, 100]}))
    status = meter.get_status()
    assert status["phases_connected"] == [True, False, True]
    assert status["phases_on"] == [True, True, True]   # voltage alone wouldn't show it


def test_relays_not_per_phase(meter):
    meter.handle_message("buckler/64/dataJson", json.dumps(THREE_PHASE_JSON))  # 6 relays
    assert meter.get_status()["phases_connected"] is None


def test_phase_status_unknown_without_voltage(meter):
    meter.handle_message("buckler/1/data/P", "100")
    assert meter.get_status()["phases_on"] is None


def test_demo_three_phase_loses_l3(tmp_path):
    meter, feed = create_smart_meter_from_env({"SMART_METER_DEMO": "3"})
    with patch("smart_meter.threading.Thread"), patch("smart_meter.time.time", return_value=3000.0):
        feed.start()  # 3000 % 300 == 0: inside the phase-loss minute
    status = meter.get_status()
    assert status["phases_on"] == [True, True, False] and status["power_phases"][2] == 0.0


def test_today_consumption_from_counter(meter, tmp_path):
    meter.handle_message("buckler/1/dataJson", json.dumps({**SINGLE_PHASE_JSON, "posWh": 100000.0}))
    meter.handle_message("buckler/1/dataJson", json.dumps({**SINGLE_PHASE_JSON, "posWh": 101500.0}))
    # Baseline comes from the readings themselves, not from when the API is read
    assert meter.get_status()["today_consumed_kwh"] == pytest.approx(1.5)
    # The baseline survives a restart (new SmartMeter, same file)
    again = SmartMeter(daily_file=str(tmp_path / "daily.json"))
    again.handle_message("buckler/1/dataJson", json.dumps({**SINGLE_PHASE_JSON, "posWh": 102000.0}))
    assert again.get_status()["today_consumed_kwh"] == pytest.approx(2.0)


def test_last_reading_before_midnight_is_next_baseline(tmp_path):
    now = [datetime(2026, 10, 17, 23, 59)]
    meter = SmartMeter(daily_file=str(tmp_path / "daily.json"), clock=lambda: now[0])
    meter.handle_message("buckler/1/data/posWh", "200000")
    meter.handle_message("buckler/1/data/posWh", "200800")   # 23:59, last reading of the day
    now[0] = datetime(2026, 10, 18, 0, 20)
    meter.handle_message("buckler/1/data/posWh", "201100")   # first reading after midnight
    assert meter.get_status()["today_consumed_kwh"] == pytest.approx(0.3)  # 201100 - 200800


def test_counter_reset_restarts_baseline(meter):
    meter.handle_message("buckler/1/dataJson", json.dumps({**SINGLE_PHASE_JSON, "posWh": 5000.0}))
    meter.handle_message("buckler/1/dataJson", json.dumps({**SINGLE_PHASE_JSON, "posWh": 10.0}))  # new meter
    assert meter.get_status()["today_consumed_kwh"] == 0.0


@pytest.fixture
def timed(tmp_path):
    """A meter with a clock the test moves: (meter, advance(seconds))."""
    now = [datetime(2026, 10, 21, 12, 0, 0)]
    meter = SmartMeter(daily_file=str(tmp_path / "daily.json"), clock=lambda: now[0])

    def advance(seconds):
        now[0] += timedelta(seconds=seconds)
    return meter, advance


def test_stale_and_offline(timed):
    meter, advance = timed
    meter.handle_message("buckler/1/dataJson", json.dumps(SINGLE_PHASE_JSON))
    meter.handle_message("buckler/1/status", "offline")
    assert meter.get_status()["status"] == "ok"               # may just be reconnecting
    advance(OFFLINE_GRACE.total_seconds())
    status = meter.get_status()
    assert status["status"] == "offline"                      # no data since: really offline
    assert status["offline_seconds"] == status["data_age_seconds"] == 90
    advance(STALE_AFTER.total_seconds())
    assert meter.get_status()["status"] == "stale"


def test_power_recorded_for_the_sparkline_while_current(timed):
    meter, advance = timed
    meter.history = MagicMock()
    meter.record_history()                                    # no readings yet: nothing
    meter.handle_message("buckler/1/data/P", "412")
    meter.record_history()
    advance(STALE_AFTER.total_seconds() + 1)
    meter.record_history()                                    # stale: the old value isn't repeated
    assert [c.args for c in meter.history.add.call_args_list] == [({"meter_power": 412.0},)]


@pytest.mark.parametrize("topic,payload", [
    ("buckler/1/data/P", "412"),
    ("buckler/1/dataJson", json.dumps(SINGLE_PHASE_JSON)),
])
def test_short_offline_never_shows(timed, topic, payload):
    """The meter's last will "offline" comes when it drops its connection for a moment
    (and, retained, first thing after a dashboard restart); data within the grace
    period means it's online, so the card never flips to "Meter offline"."""
    meter, advance = timed
    meter.handle_message("buckler/1/status", "offline")       # retained, from earlier
    meter.handle_message(topic, payload)
    assert meter.get_status()["status"] == "ok"
    for _ in range(3):                                        # a flaky Wi-Fi link
        meter.handle_message("buckler/1/status", "offline")
        advance(OFFLINE_GRACE.total_seconds() - 1)
        assert meter.get_status()["status"] == "ok"
        meter.handle_message(topic, payload)
        assert meter.get_status()["status"] == "ok"


def test_online_status_cancels_offline(timed):
    meter, advance = timed
    meter.handle_message("buckler/1/data/P", "1")
    meter.handle_message("buckler/1/status", "offline")
    meter.handle_message("buckler/1/status", "online")
    advance(OFFLINE_GRACE.total_seconds() + 1)
    status = meter.get_status()
    assert status["status"] == "ok" and status["offline_seconds"] is None and status["data_age_seconds"] == 91


def test_interval_requested_again_after_meter_reconnects():
    meter = SmartMeter(daily_file=None)
    client = SmartMeterClient(meter, "localhost", interval=10)
    mqtt = MagicMock()

    def message(topic, payload):
        client._on_message(mqtt, None, MagicMock(topic=topic, payload=payload))

    message("buckler/1/data/P", b"1")
    message("buckler/1/data/P", b"2")
    assert mqtt.publish.call_count == 1                       # once per connection
    message("buckler/1/status", b"online")                    # the meter reconnected
    assert mqtt.publish.call_count == 2
    assert mqtt.publish.call_args[0][:2] == ("buckler/1/data/set", "10")


def test_first_meter_wins_unless_serial_configured(tmp_path):
    meter = SmartMeter(daily_file=str(tmp_path / "d.json"))
    meter.handle_message("buckler/A/data/P", "1")
    meter.handle_message("buckler/B/data/P", "2")
    assert meter.get_status()["serial"] == "A" and meter.get_status()["power_w"] == 1.0
    chosen = SmartMeter(serial_filter="B", daily_file=str(tmp_path / "e.json"))
    chosen.handle_message("buckler/A/data/P", "1")
    chosen.handle_message("buckler/B/data/P", "2")
    assert chosen.get_status()["serial"] == "B" and chosen.get_status()["power_w"] == 2.0


def test_a_gone_meters_stored_messages_dont_take_over(tmp_path):
    """The broker keeps an old meter's last messages: a live meter must win over
    one heard only from those, with its own stored messages applied first."""
    meter = SmartMeter(daily_file=str(tmp_path / "d.json"))
    meter.handle_message("buckler/OLD/status", "offline", retained=True)
    meter.handle_message("buckler/NEW/relay/r1", "0", retained=True)
    meter.handle_message("buckler/NEW/data/P", "5")
    status = meter.get_status()
    assert status["serial"] == "NEW" and status["power_w"] == 5.0 and status["status"] == "ok"
    assert meter._relays == {"r1": 0}
    # Once a meter is live, the first one still wins
    meter.handle_message("buckler/OTHER/data/P", "7")
    assert meter.get_status()["serial"] == "NEW"


def test_daily_baseline_belongs_to_one_meter(tmp_path):
    daily = str(tmp_path / "d.json")
    first = SmartMeter(daily_file=daily)
    first.handle_message("buckler/A/dataJson", json.dumps({**SINGLE_PHASE_JSON, "posWh": 100000.0}))
    # Another meter (replaced) with a higher counter: no jump in today's usage
    second = SmartMeter(daily_file=daily)
    second.handle_message("buckler/B/dataJson", json.dumps({**SINGLE_PHASE_JSON, "posWh": 500000.0}))
    second.handle_message("buckler/B/dataJson", json.dumps({**SINGLE_PHASE_JSON, "posWh": 500300.0}))
    assert second.get_status()["today_consumed_kwh"] == pytest.approx(0.3)


def test_ignores_garbage(meter):
    meter.handle_message("other/1/dataJson", "{}")
    meter.handle_message("buckler/1/dataJson", "not json")
    meter.handle_message("buckler/1/data/P", "abc")
    meter.handle_message("buckler/1/data/set", "10")  # our own interval request
    meter.set_broker_state(True)
    assert meter.get_status()["status"] == "waiting"


def test_client_requests_update_interval_once_and_never_touches_relay(meter):
    client = SmartMeterClient(meter, host="broker", interval=10)
    mqtt = MagicMock()
    for topic, payload in (("buckler/9/status", b"online"), ("buckler/9/data/P", b"5"), ("buckler/9/relay/r1", b"1")):
        client._on_message(mqtt, None, MagicMock(topic=topic, payload=payload))
    mqtt.publish.assert_called_once_with("buckler/9/data/set", "10", qos=0)
    assert all("/relay/" not in c.args[0] for c in mqtt.publish.call_args_list)


def test_env_config():
    assert create_smart_meter_from_env({}) == (None, None)
    meter, client = create_smart_meter_from_env({
        "SMART_METER_ENABLED": "true", "SMART_METER_MQTT_HOST": "10.0.0.2", "SMART_METER_MQTT_PORT": "1884",
        "SMART_METER_MQTT_USER": "meter", "SMART_METER_MQTT_PASSWORD": "secret", "SMART_METER_INTERVAL": "500",
        "SMART_METER_SERIAL": "772_ABC",
    })
    assert client.host == "10.0.0.2" and client.port == 1884 and client.username == "meter"
    assert client.interval == 60  # clamped to the meter's 1..60 s range
    assert meter.serial_filter == "772_ABC"


class TestEndpoint:
    @pytest.fixture
    def client(self):
        import app as app_module
        app_module.app.config["TESTING"] = True
        orig = app_module.smart_meter
        with app_module.app.test_client() as c:
            yield c, app_module
        app_module.smart_meter = orig

    def test_disabled(self, client):
        c, app_module = client
        app_module.smart_meter = None
        assert c.get("/api/smart-meter").get_json() == {"status": "disabled"}

    def test_readings(self, client, tmp_path):
        c, app_module = client
        meter = SmartMeter(daily_file=str(tmp_path / "d.json"))
        meter.handle_message("buckler/772_ABC/dataJson", json.dumps(SINGLE_PHASE_JSON))
        app_module.smart_meter = meter
        data = c.get("/api/smart-meter").get_json()
        assert data["status"] == "ok" and data["voltage"] == [225.3]

    def test_card_on_page(self, client):
        c, _ = client
        assert 'id="smart-meter-card"' in c.get("/").get_data(as_text=True)


@pytest.mark.parametrize("setting,phases", [("1", 1), ("true", 1), ("3", 3)])
def test_demo_meter(setting, phases):
    meter, feed = create_smart_meter_from_env({"SMART_METER_DEMO": setting})
    assert meter.daily_file is None  # demo never touches the real daily baseline file
    with patch("smart_meter.threading.Thread"):
        feed.start()
    feed._last_tick -= 60  # a minute later
    feed.tick()
    status = meter.get_status()
    assert status["status"] == "ok" and status["demo"] is True and status["phases"] == phases
    assert len(status["voltage"]) == phases and len(status["power_phases"]) == phases
    assert status["power_w"] > 0 and status["today_consumed_kwh"] >= 0
    assert status["frequency_max"] and status["relays"]


def test_real_meter_not_flagged_demo(meter):
    meter.handle_message("buckler/1/dataJson", json.dumps(SINGLE_PHASE_JSON))
    assert "demo" not in meter.get_status()


def test_energy_total_waits_for_every_phase(tmp_path):
    """data/posWh1..3 arrive one by one: L1 alone must not become the day's
    baseline (after a mid-day restart it replaced the saved one, and "today"
    then counted L2 and L3's lifetime energy)."""
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    daily = tmp_path / "daily.json"
    daily.write_text(json.dumps({"date": today, "posWh": 5_999_000.0}))
    m = SmartMeter(daily_file=str(daily))
    for i, v in enumerate((1e6, 2e6, 3e6), start=1):
        m.handle_message(f"buckler/M1/data/V{i}", "230")
        m.handle_message(f"buckler/M1/data/posWh{i}", str(v))
    status = m.get_status()
    assert json.loads(daily.read_text())["posWh"] == 5_999_000.0
    assert status["today_consumed_kwh"] == pytest.approx(1.0)
    assert status["energy_consumed_kwh"] == pytest.approx(6000.0)


def test_energy_total_is_none_while_a_phase_is_missing(tmp_path):
    m = SmartMeter(daily_file=str(tmp_path / "daily.json"))
    m.handle_message("buckler/M1/data/posWh1", "1000")
    assert m.get_status()["energy_consumed_kwh"] is None
