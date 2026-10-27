"""Bakler smart Wi-Fi electricity meter over MQTT.

The meter is an MQTT client: it connects to a broker (Mosquitto from
docker-compose, or any other) and publishes under buckler/<serial>/...
(https://www.bakler.com.ua/setting-mqtt-client/):

  buckler/<serial>/status          "online" / offline. The broker's "offline" (the
                                   meter's last will) can be old: it's kept as a
                                   retained message, and it also comes when the
                                   meter drops its connection for a moment and
                                   reconnects. So any data message counts as
                                   online, and "offline" shows only after
                                   OFFLINE_GRACE without data.
  buckler/<serial>/dataJson        JSON every minute: posWh/negWh (consumed/generated
                                   active energy, Wh), V/Vmin/Vmax, I/Imin/Imax,
                                   Pmin/Pmax, Hzmin/Hzmax, Tbox, relay. Three-phase
                                   meters send [L1, L2, L3] arrays.
  buckler/<serial>/data/<field>    the same values one per topic, plus instantaneous
                                   P/Q (single-phase: V, I, P; three-phase: V1..V3 ...)
  buckler/<serial>/data/set        update interval for data/* in seconds (1-60)
  buckler/<serial>/relay/r<N>      relay state: 0 = off, 1..100 = on

The dashboard only reads data (and sets the update interval); it never switches
the relay.
"""
import json
import logging
import math
import os
import random
import re
import threading
import time
from datetime import datetime, timedelta

from meter_outages import DEFAULT_SWITCH_ON_DELAY, PARTIAL, TOTAL, OutageLog, local_now
from storage import atomic_write_json, data_path, load_json

logger = logging.getLogger(__name__)

TOPIC_ROOT = "buckler"
# No update for this long: show the data as stale
STALE_AFTER = timedelta(minutes=5)
# "offline" from the status topic shows only when no data came for this long
# after it: the meter often reconnects within seconds (and keeps sending data)
OFFLINE_GRACE = timedelta(seconds=90)
# A phase below this voltage has no supply (real readings are ~230 V or ~0 V)
PHASE_ON_MIN_VOLTAGE = 100

_PHASE_FIELD = re.compile(r"^([A-Za-z]+?)([123])$")  # V1, posWh2, ...
_NUMERIC = (int, float)


def _number(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN -> None


def _complete_sum(values):
    """Sum of per-phase values, or None until every phase has one: a three-phase
    meter's data/posWh1..3 arrive one by one, and L1 alone isn't the total."""
    if not values or any(v is None for v in values):
        return None
    return sum(values)


def _as_list(value):
    """Scalar (single-phase) or [L1, L2, L3] (three-phase) -> list of numbers."""
    if isinstance(value, list):
        return [_number(v) for v in value]
    n = _number(value)
    return None if n is None else [n]


class SmartMeter:
    """Latest readings of one meter, built from its MQTT messages.

    Thread-safe: MQTT callbacks update it, Flask requests read it.
    """

    ENERGY_KEYS = ("posWh", "negWh")

    def __init__(self, serial_filter=None, daily_file="smart_meter_daily.json", clock=local_now, demo=False,
                 outages_file=None, switch_on_delay=DEFAULT_SWITCH_ON_DELAY):
        self.serial_filter = serial_filter or None
        self.daily_file = daily_file  # None: keep today's baseline in memory only
        self.clock = clock
        self.demo = demo
        # Grid outages it sees (today and yesterday); outages_file None: in memory only
        # switch_on_delay: the meter's own setting (seconds), see meter_outages.py
        self.outages = OutageLog(outages_file, clock=clock, switch_on_delay=switch_on_delay)
        # () -> True / False / None: does the inverter have the grid now? Set by the
        # app; tells a real outage from the meter only losing its Wi-Fi
        self.grid_online_hint = None
        self._lock = threading.Lock()
        # Energy counters at the start of today: {"date": "YYYY-MM-DD", "posWh": .., "negWh": ..}
        daily = load_json(daily_file, {}) if daily_file else {}
        self._daily = daily if isinstance(daily, dict) and "date" in daily else {}
        self._last_totals = {}  # latest counter totals, for the next day's baseline
        self._serial = None
        self._serial_live = False  # _serial sent a live message (else only retained ones so far)
        self._retained = {}     # serial -> {topic: payload}: other meters' stored messages, see handle_message
        self._online = None     # from the status topic; any data message sets it True
        self._offline_at = None  # when it last reported offline (and sent no data since)
        self._values = {}       # field -> list of numbers (len 1 or 3)
        self._relays = {}       # "r1" -> int
        self._updated = None
        self._broker_connected = False
        self._broker_error = None

    # ----- MQTT message handling -----

    def handle_message(self, topic, payload, retained=False):
        """Apply one MQTT message (topic string, payload bytes/str).

        retained: the broker's stored copy, sent on (re)subscribing, not a new
        message. A retained reading is from whenever the meter last sent it (maybe
        before an outage it's still in): it must not count as the meter being
        back, nor as the grid's state now. So retained readings are skipped and a
        retained relay state is only stored.
        """
        parts = topic.split("/")
        if len(parts) < 3 or parts[0] != TOPIC_ROOT:
            return
        serial = parts[1]
        replay = {}
        with self._lock:
            if self.serial_filter and serial != self.serial_filter:
                return
            if self._serial is None:
                self._serial = serial
                logger.info("Smart meter: receiving data from %s", serial)
            elif serial != self._serial:
                if self._serial_live:
                    return  # first meter wins; set SMART_METER_SERIAL to pick another
                if retained:
                    # Kept in case this one turns out to be the live meter
                    self._retained.setdefault(serial, {})[topic] = payload
                    return
                # The broker keeps a meter's last messages, also of one that is
                # gone: a meter heard only from those gives way to a live one,
                # with this one's stored messages applied first
                logger.info("Smart meter: %s is live, %s only had stored messages: using %s",
                            serial, self._serial, serial)
                self._serial = serial
                self._online = self._offline_at = None
                self._relays = {}
                replay = self._retained.get(serial, {})
            if not retained:
                self._serial_live = True
                self._retained = {}
        for stored_topic, stored_payload in replay.items():
            self.handle_message(stored_topic, stored_payload, retained=True)
        text = payload.decode("utf-8", "replace") if isinstance(payload, (bytes, bytearray)) else str(payload)
        kind = parts[2]
        if kind == "status" and len(parts) == 3:
            online = text.strip().lower() == "online"
            with self._lock:
                if not online and self._online is not False:
                    self._offline_at = self.clock()
                    logger.info("Smart meter: %s reported offline", serial)
                elif online:
                    self._offline_at = None
                self._online = online
        elif retained and kind in ("dataJson", "data"):
            return
        elif kind == "dataJson" and len(parts) == 3:
            self._apply_json(text)
        elif kind == "data" and len(parts) == 4 and parts[3] not in ("set", "get"):
            self._apply_field(parts[3], text)
        elif kind == "relay" and len(parts) == 4 and parts[3].startswith("r"):
            value = _number(text)
            if value is not None:
                with self._lock:
                    self._relays[parts[3]] = int(value)
                    if not retained:
                        self._check_grid()

    def _apply_json(self, text):
        try:
            data = json.loads(text)
        except ValueError:
            logger.warning("Smart meter: invalid dataJson payload")
            return
        if not isinstance(data, dict):
            return
        updates = {}
        for key, value in data.items():
            if key in ("ver", "time", "relay"):
                continue
            values = _as_list(value)
            if values is not None:
                updates[key] = values
        relay = data.get("relay")
        relays = {}
        if relay is not None:
            for i, v in enumerate(relay if isinstance(relay, list) else [relay], start=1):
                n = _number(v)
                if n is not None:
                    relays[f"r{i}"] = int(n)
        with self._lock:
            self._values.update(updates)
            self._relays.update(relays)
            self._updated = self.clock()
            self._mark_online()
            self._track_energy()
            if "V" in updates or relays:
                self._check_grid()

    def _apply_field(self, field, text):
        value = _number(text)
        if value is None:
            return
        m = _PHASE_FIELD.match(field)
        with self._lock:
            if m and m.group(1) in ("posWh", "negWh", "posVarh", "negVarh", "V", "I", "P", "Q", "T"):
                name, index = m.group(1), int(m.group(2)) - 1
                current = list(self._values.get(name) or [])
                if len(current) < 3:
                    current = (current + [None, None, None])[:3]
                current[index] = value
                self._values[name] = current
            else:
                self._values[field] = [value]
            self._updated = self.clock()
            self._mark_online()
            if field.startswith(self.ENERGY_KEYS):
                self._track_energy()
            if (m and m.group(1) == "V") or field == "V":
                self._check_grid()

    def _mark_online(self):
        """It just sent data, whatever the last status said (caller holds the lock)."""
        if self._online is False and self._offline_at is not None:
            logger.info("Smart meter: sending data again %.0fs after reporting offline",
                        (self.clock() - self._offline_at).total_seconds())
        if self._online is False and self.outages.open_type == TOTAL:
            # Back after an outage it was offline for; its voltages (next readings)
            # tell whether a phase is still missing
            self.outages.update("ok")
        self._online = True
        self._offline_at = None

    # ----- outages -----

    def _check_grid(self):
        """Log the grid condition by the latest voltages and relays (caller holds
        the lock). All phases without voltage: total; some without voltage, or cut
        by the meter's relay (three-phase meters with r1..r3 = L1..L3): partial."""
        volts = self._values.get("V") or []
        if not volts or any(v is None for v in volts):
            return  # not all phases read yet
        dead = [i + 1 for i, v in enumerate(volts) if v < PHASE_ON_MIN_VOLTAGE]
        relays = [self._relays.get(f"r{i + 1}") for i in range(len(volts))]
        cut = []
        if len(volts) == 1 or len(self._relays) == 3:
            cut = [i + 1 for i, r in enumerate(relays) if r == 0 and i + 1 not in dead]
        if len(dead) == len(volts):
            self.outages.update(TOTAL)
        elif dead or cut:
            # Voltage on every phase but all cut: typically the meter's wait before
            # switching the house back on after an outage (OutageLog joins it)
            self.outages.update(PARTIAL, missing=dead + cut, cut=cut, restore=not dead and len(cut) == len(volts))
        else:
            self.outages.update("ok")

    def check_offline(self):
        """Offline for OFFLINE_GRACE: the meter (powered by the grid) has no grid,
        so a total outage from when it went offline. Unless the inverter still has
        the grid: then the meter only lost its connection. Runs every few seconds
        (SmartMeterClient) and on each outage log request."""
        with self._lock:
            offline_at = self._offline_at
            offline = (self._online is False and offline_at is not None
                       and self.clock() - offline_at >= OFFLINE_GRACE)
        if not offline or self.outages.open_type == TOTAL:
            return
        hint = self.grid_online_hint() if self.grid_online_hint else None
        if hint is True:
            return
        self.outages.update(TOTAL, at=offline_at)

    def get_outages(self):
        """Outage log for the API: today's and yesterday's outages, newest first."""
        self.check_offline()
        result = {"status": "ok", "now": self.clock().isoformat(timespec="seconds"),
                  "today": self.clock().strftime("%Y-%m-%d"), "outages": self.outages.snapshot()}
        if self.demo:
            result["demo"] = True
        return result

    def set_broker_state(self, connected, error=None):
        with self._lock:
            self._broker_connected = connected
            self._broker_error = error

    # ----- daily consumption -----

    def _track_energy(self):
        """Keep today's baseline of the energy counters (caller holds the lock).

        Runs on every reading, so the baseline doesn't depend on anyone viewing
        the dashboard. On a new day the last reading before midnight becomes the
        baseline; it is saved, so a restart during the day keeps counting.
        """
        totals = {}
        for key in self.ENERGY_KEYS:
            total = _complete_sum(self._values.get(key))
            if total is not None:
                totals[key] = total
        if not totals:
            return
        today = self.clock().strftime("%Y-%m-%d")
        changed = False
        other_meter = self._daily.get("serial") not in (None, self._serial)
        if self._daily.get("date") != today or other_meter:
            # Another meter's counters aren't a baseline: start from this reading
            previous = {} if other_meter else self._last_totals  # last reading of the previous day, if any
            self._daily = {"date": today, **{k: previous.get(k, v) for k, v in totals.items()}}
            changed = True
        if self._daily.get("serial") != self._serial:
            self._daily["serial"] = self._serial
            changed = True
        for key, total in totals.items():
            if key not in self._daily or total < self._daily[key]:  # new counter / reset meter
                self._daily[key] = total
                changed = True
        self._last_totals = totals
        if changed and self.daily_file:
            try:
                atomic_write_json(self.daily_file, self._daily)
            except OSError:
                logger.exception("Smart meter: could not save daily baseline")

    def _today(self, key, total_wh):
        if total_wh is None or self._daily.get("date") != self.clock().strftime("%Y-%m-%d"):
            return None
        return max(0.0, total_wh - self._daily.get(key, total_wh))

    # ----- status for the API -----

    def get_status(self):
        with self._lock:
            serial = self._serial
            online = self._online
            offline_at = self._offline_at
            values = {k: list(v) for k, v in self._values.items()}
            relays = dict(self._relays)
            updated = self._updated
            broker_connected = self._broker_connected
            broker_error = self._broker_error
            today_consumed = self._today("posWh", self._last_totals.get("posWh"))
            today_generated = self._today("negWh", self._last_totals.get("negWh"))

        if updated is None:
            # "error" only after a failed connection attempt; before the first
            # attempt has finished (e.g. right after a restart) it's just waiting
            status = "error" if not broker_connected and broker_error else "waiting"
            result = {"status": status}
            if not broker_connected and broker_error:
                result["error"] = broker_error
            if self.demo:
                result["demo"] = True
            return result

        def phase_list(key):
            vals = values.get(key)
            return [v for v in vals] if vals else None

        def total(key):
            vals = [v for v in (values.get(key) or []) if v is not None]
            return sum(vals) if vals else None

        phases = 3 if any(len(values.get(k) or []) == 3 for k in ("V", "I", "P", "posWh")) else 1
        now = self.clock()
        if now - updated > STALE_AFTER:
            status = "stale"
        elif online is False and (offline_at is None or now - offline_at >= OFFLINE_GRACE):
            status = "offline"
        else:
            status = "ok"

        consumed = _complete_sum(values.get("posWh"))
        generated = _complete_sum(values.get("negWh"))
        result = {
            "status": status,
            "serial": serial,
            "phases": phases,
            "updated": updated.isoformat(),
            # For checking the status: seconds since the last reading, and since the
            # meter reported offline (None while it's online)
            "data_age_seconds": round((now - updated).total_seconds()),
            "offline_seconds": None if online is not False or offline_at is None
                               else round((now - offline_at).total_seconds()),
            "power_w": total("P"),
            "power_phases": phase_list("P"),
            "voltage": phase_list("V"),
            "phases_on": [None if v is None else v >= PHASE_ON_MIN_VOLTAGE for v in values["V"]]
                         if values.get("V") else None,
            "current": phase_list("I"),
            "frequency_min": total("Hzmin"),
            "frequency_max": total("Hzmax"),
            "energy_consumed_kwh": None if consumed is None else consumed / 1000,
            "energy_generated_kwh": None if generated is None else generated / 1000,
            "today_consumed_kwh": None if today_consumed is None else today_consumed / 1000,
            "today_generated_kwh": None if today_generated is None else today_generated / 1000,
            "relays": [relays[k] for k in sorted(relays, key=lambda r: int(r[1:]) if r[1:].isdigit() else 0)],
        }
        # Three-phase meters with one relay per phase (r1..r3 = L1..L3): a phase
        # with its relay off is cut by the meter (it still measures the grid-side
        # voltage, e.g. the overvoltage that made it trip)
        relay_list = result["relays"]
        result["phases_connected"] = ([r > 0 for r in relay_list]
                                      if phases == 3 and len(relay_list) == 3 else None)
        # Without instantaneous P (only dataJson received so far), fall back to the
        # minute's max power so the card isn't empty
        if result["power_w"] is None and total("Pmax") is not None:
            result["power_w"] = total("Pmax")
            result["power_phases"] = phase_list("Pmax")
        if self.demo:
            result["demo"] = True
        return result


class SmartMeterClient:
    """MQTT connection for SmartMeter (paho-mqtt, background network thread)."""

    def __init__(self, meter, host, port=1883, username=None, password=None, interval=10):
        self.meter = meter
        self.host = host
        self.port = port
        self.username = username or None
        self.password = password or None
        self.interval = interval
        self.description = f"MQTT broker {host}:{port}"
        self._client = None
        self._interval_sent = set()

    def start(self):
        import paho.mqtt.client as mqtt

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="kiosk-dashboard",
                             clean_session=True)
        if self.username:
            client.username_pw_set(self.username, self.password)
        client.reconnect_delay_set(min_delay=2, max_delay=60)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        self._client = client
        try:
            client.connect_async(self.host, self.port, keepalive=60)
        except Exception as e:  # e.g. host name can't be resolved yet
            logger.warning("Smart meter: MQTT connect to %s:%s failed: %s", self.host, self.port, e)
            self.meter.set_broker_state(False, f"Cannot connect to MQTT broker {self.host}:{self.port}")
        client.loop_start()  # handles reconnects
        threading.Thread(target=self._watch_offline, name="smart-meter-outages", daemon=True).start()

    def _watch_offline(self):
        """An outage the meter goes offline for sends nothing more: notice it."""
        while True:
            time.sleep(15)
            try:
                self.meter.check_offline()
            except Exception:
                logger.exception("Smart meter: outage check failed")

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            msg = f"MQTT broker refused connection: {reason_code}"
            logger.warning("Smart meter: %s", msg)
            self.meter.set_broker_state(False, msg)
            return
        logger.info("Smart meter: connected to MQTT broker %s:%s", self.host, self.port)
        self.meter.set_broker_state(True)
        self._interval_sent.clear()
        client.subscribe([(f"{TOPIC_ROOT}/+/status", 0), (f"{TOPIC_ROOT}/+/dataJson", 0),
                          (f"{TOPIC_ROOT}/+/data/+", 0), (f"{TOPIC_ROOT}/+/relay/+", 0)])

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            logger.warning("Smart meter: disconnected from MQTT broker (%s), reconnecting", reason_code)
        self.meter.set_broker_state(False, f"Disconnected from MQTT broker {self.host}:{self.port}")

    def _on_message(self, client, userdata, message):
        try:
            self.meter.handle_message(message.topic, message.payload, retained=bool(message.retain))
            parts = message.topic.split("/")
            if len(parts) == 3 and parts[2] == "status" and message.payload.strip().lower() == b"online":
                # The meter (re)connected: it may be back on its default once-a-minute
                # updates, so ask for the faster interval again
                self._interval_sent.discard(parts[1])
            self._maybe_set_interval(client, message.topic)
        except Exception:
            logger.exception("Smart meter: error handling %s", message.topic)

    def _maybe_set_interval(self, client, topic):
        """Ask the meter for faster data/* updates (default is once a minute)."""
        if not self.interval:
            return
        parts = topic.split("/")
        serial = parts[1] if len(parts) > 1 else None
        if serial and serial == self.meter._serial and serial not in self._interval_sent:
            self._interval_sent.add(serial)
            client.publish(f"{TOPIC_ROOT}/{serial}/data/set", str(self.interval), qos=0)
            logger.info("Smart meter: requested %ss update interval from %s", self.interval, serial)


class DemoMeterFeed:
    """Simulated meter for checking the card without hardware (SMART_METER_DEMO).

    Feeds made-up readings through SmartMeter.handle_message, the same path as
    real MQTT messages: data/* every few seconds, dataJson once a minute.
    """

    SERIAL = "DEMO"
    UPDATE_SECONDS = 5
    # Three-phase demo: L3 loses supply for the first minute of every 5 minutes
    PHASE_LOSS_PERIOD = 300
    PHASE_LOSS_SECONDS = 60

    def __init__(self, meter, phases=1):
        self.meter = meter
        self.phases = 3 if phases == 3 else 1
        self.description = f"demo data ({self.phases}-phase)"
        self._energy_wh = [4_215_300.0, 3_980_150.0, 4_402_800.0][:self.phases]
        self._last_tick = None
        self._last_json = None

    def start(self):
        self.meter.set_broker_state(True)
        # Pretend the day so far used ~0.45 kWh per hour, so "today" isn't 0 at start
        now = self.meter.clock()
        hours = now.hour + now.minute / 60
        self.meter._daily = {"date": now.strftime("%Y-%m-%d"),
                             "posWh": sum(self._energy_wh) - hours * 450, "negWh": 0.0}
        self.tick()
        threading.Thread(target=self._run, name="smart-meter-demo", daemon=True).start()

    def _run(self):
        while True:
            time.sleep(self.UPDATE_SECONDS)
            try:
                self.tick()
            except Exception:
                logger.exception("Smart meter demo: tick failed")

    def _readings(self):
        t = time.time()
        powers, volts, amps = [], [], []
        for i in range(self.phases):
            base = (320, 180, 540)[i] if self.phases == 3 else 450
            # slow swing (a kettle, a fridge compressor ...) plus noise
            p = base + base * 0.6 * math.sin(t / 90 + i * 2.1) + random.uniform(-25, 25)
            v = 229 + 3 * math.sin(t / 300 + i) + random.uniform(-0.8, 0.8)
            if self.phases == 3 and i == 2 and t % self.PHASE_LOSS_PERIOD < self.PHASE_LOSS_SECONDS:
                p, v = 0.0, random.uniform(0, 0.4)
            powers.append(round(max(p, 0.0), 1))
            volts.append(round(v, 1))
            amps.append(round(powers[-1] / v, 2) if v >= PHASE_ON_MIN_VOLTAGE else 0.0)
        return powers, volts, amps

    def tick(self):
        now = time.monotonic()
        powers, volts, amps = self._readings()
        if self._last_tick is not None:
            hours = (now - self._last_tick) / 3600
            self._energy_wh = [e + p * hours for e, p in zip(self._energy_wh, powers)]
        self._last_tick = now

        topic = f"{TOPIC_ROOT}/{self.SERIAL}"
        handle = self.meter.handle_message
        if self._last_json is None:
            handle(f"{topic}/status", "online")
        if self.phases == 1:
            for field, value in (("P", powers[0]), ("V", volts[0]), ("I", amps[0])):
                handle(f"{topic}/data/{field}", str(value))
        else:
            for i in range(3):
                for field, values in (("P", powers), ("V", volts), ("I", amps)):
                    handle(f"{topic}/data/{field}{i + 1}", str(values[i]))
        if self._last_json is None or now - self._last_json >= 60:
            self._last_json = now
            one = (lambda v: v[0]) if self.phases == 1 else (lambda v: v)
            handle(f"{topic}/dataJson", json.dumps({
                "ver": "demo", "posWh": one([round(e, 3) for e in self._energy_wh]),
                "negWh": one([0.0] * self.phases),
                "Vmin": one([max(v - 2, 0.0) for v in volts]), "V": one(volts),
                "Vmax": one([v + 2 if v else 0.0 for v in volts]),
                "I": one(amps), "Pmax": one([p * 1.3 for p in powers]),
                "Hzmin": round(49.97 + random.uniform(-0.02, 0.01), 2),
                "Hzmax": round(50.01 + random.uniform(-0.01, 0.02), 2),
                "relay": 1 if self.phases == 1 else [1, 100, 100, 100, 100, 100],
            }))


def default_broker_host():
    """In docker-compose the broker is the "mosquitto" service; otherwise localhost."""
    return "mosquitto" if os.path.exists("/.dockerenv") else "localhost"


def default_daily_file():
    """Keep today's baseline in data/ when it exists (the Docker volume survives rebuilds)."""
    return data_path("smart_meter_daily.json")


def default_outages_file():
    return data_path("meter_outages.json")


def create_smart_meter_from_env(environ=os.environ):
    """(SmartMeter, SmartMeterClient) when SMART_METER_ENABLED, else (None, None).

    SMART_METER_DEMO=1 / 3 (or true) returns a simulated single-/three-phase meter
    (DemoMeterFeed in place of the client) instead; no broker needed.
    """
    demo = environ.get("SMART_METER_DEMO", "").strip().lower()
    if demo in ("1", "3", "true", "yes"):
        meter = SmartMeter(daily_file=None, demo=True)
        return meter, DemoMeterFeed(meter, phases=3 if demo == "3" else 1)
    if environ.get("SMART_METER_ENABLED", "false").lower() not in ("true", "1", "yes"):
        return None, None
    try:
        port = int(environ.get("SMART_METER_MQTT_PORT", "1883"))
    except ValueError:
        port = 1883
    try:
        interval = max(0, min(60, int(environ.get("SMART_METER_INTERVAL", "10"))))
    except ValueError:
        interval = 10
    try:
        switch_on_delay = max(0, min(3600, int(environ.get("SMART_METER_SWITCH_ON_DELAY", DEFAULT_SWITCH_ON_DELAY))))
    except ValueError:
        switch_on_delay = DEFAULT_SWITCH_ON_DELAY
    meter = SmartMeter(serial_filter=environ.get("SMART_METER_SERIAL") or None,
                       daily_file=environ.get("SMART_METER_DAILY_FILE") or default_daily_file(),
                       outages_file=environ.get("SMART_METER_OUTAGES_FILE") or default_outages_file(),
                       switch_on_delay=switch_on_delay)
    client = SmartMeterClient(
        meter,
        host=environ.get("SMART_METER_MQTT_HOST") or default_broker_host(),
        port=port,
        username=environ.get("SMART_METER_MQTT_USER"),
        password=environ.get("SMART_METER_MQTT_PASSWORD"),
        interval=interval,
    )
    return meter, client
