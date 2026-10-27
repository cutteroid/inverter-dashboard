"""Deye / Sunsynk hybrid inverters through a Solarman V5 Wi-Fi logger (Modbus over TCP 8899).

Register maps: single-phase hybrid (Sunsynk-style, registers ~60-300) and
three-phase hybrid (registers ~500-700; high-voltage models scale some values).
Register 0 (device type) tells the family.
"""
import logging
import queue
import time

from pysolarmanv5 import NoSocketAvailableError, PySolarmanV5

from .base import (InverterConfig, InverterDriver, grid_online, grid_phases_missing,
                   grid_state, to_signed)

MAX_LOGGER_SERIAL = 0xFFFFFFFF  # Solarman V5 packs the serial as an unsigned 32-bit int


def parse_logger_serial(raw):
    """Logger serial as int, or 0 if missing/invalid (must fit in 32 bits)."""
    try:
        serial = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0
    return serial if 0 < serial <= MAX_LOGGER_SERIAL else 0

logger = logging.getLogger(__name__)

# The logger didn't answer or the connection broke (not an error reply from the
# inverter): further reads in the same poll would only wait out the same timeout
# (pysolarmanv5 raises a bare queue.Empty on a timeout)
CONNECTION_ERRORS = (queue.Empty, OSError, NoSocketAvailableError)


def error_text(e):
    """Text for the readings' "error": never empty, since an empty error would
    read as a good poll (a timeout's queue.Empty has no message)."""
    if isinstance(e, queue.Empty):
        return "no answer from the logger (timeout)"
    return str(e) or type(e).__name__


# Register 0 ("device type") -> inverter family; codes as used by ha-solarman's
# Deye autodetection (custom_components/solarman/const.py). Each family has its
# own register map; high-voltage 3-phase models also scale some values.
DEVICE_FAMILIES = {
    0x0002: "string", 0x0200: "string",
    0x0003: "hybrid_1p", 0x0300: "hybrid_1p", 0x0103: "hybrid_1p", 0x0104: "hybrid_1p",
    0x0004: "micro", 0x0400: "micro",
    0x0005: "hybrid_3p_lv", 0x0500: "hybrid_3p_lv",
    0x0006: "hybrid_3p_hv", 0x0007: "hybrid_3p_hv", 0x0008: "hybrid_3p_hv",
    0x0600: "hybrid_3p_hv", 0x0601: "hybrid_3p_hv",
}
FAMILY_PHASES = {"hybrid_1p": 1, "hybrid_3p_lv": 3, "hybrid_3p_hv": 3}
UNSUPPORTED_FAMILIES = ("string", "micro")  # different register maps, not implemented


RELAY_GRID_BIT = 0x04  # register 552 (3-phase): bit 0 inverter, bit 2 grid, bit 3 generator relay


def relay_grid_connected(relay):
    """Grid relay state from register 552, or None when the register says nothing.

    0 means "all relays off", which is also what firmware without this register
    returns: then the voltages decide (see base.grid_online()).
    """
    return bool(relay & RELAY_GRID_BIT) if relay else None


def battery_temperature(raw):
    """Battery temperature register ((x - 1000) / 10 °C) -> °C, or None when implausible
    (0 = not reported, i.e. -100 °C)."""
    celsius = (raw - 1000) / 10
    return celsius if -40 <= celsius <= 90 else None


# Inverter health, 3-phase register map: the running state on every read; the
# temperatures, lifetime battery totals and warning / fault bits every
# HEALTH_READ_SECONDS, and on every read while the inverter reports a problem
INVERTER_STATES = {0: "standby", 1: "self_check", 2: "normal", 3: "alarm", 4: "fault"}
HEALTH_READ_SECONDS = 300
REG_STATE_3P = 500
REG_TEMPERATURES_3P = 540     # 540 DC side, 541 AC side (heatsink): (x - 1000) / 10 °C
REG_BATTERY_TOTALS_3P = 516   # 516/517 charged, 518/519 discharged: 0.1 kWh, low word first
REG_WARNINGS_3P = 553         # 553-554: warning bits -> W01..W32
REG_FAULTS_3P = 555           # 555-558: fault bits -> F01..F64 (the inverter manual's fault codes)
# 3-phase poll registers read as blocks (start, count): 586-592 battery,
# 598-600 grid voltage, 644-653 phase voltages and loads, 520-529 daily
# energy, 672-673 PV power. The rest (552, 607, 115, 667, 500) one by one.
BLOCKS_3P = ((586, 7), (598, 3), (644, 10), (520, 10), (672, 2))


def inverter_temperature(raw):
    """Inverter temperature register ((x - 1000) / 10 °C) -> °C, or None when implausible."""
    celsius = (raw - 1000) / 10
    return celsius if -40 <= celsius <= 120 else None


def bit_codes(words, prefix):
    """Set bits of consecutive 16-bit registers -> codes: bit 0 of the first = <prefix>01."""
    return [f"{prefix}{index * 16 + bit + 1:02d}"
            for index, word in enumerate(words) for bit in range(16) if word & (1 << bit)]


def total_kwh(low, high):
    """32-bit counter in 0.1 kWh (low word first) -> kWh."""
    return ((high << 16) | low) / 10


def device_family(device_type):
    """Family name for a register-0 value ("unknown" if not recognised)."""
    return DEVICE_FAMILIES.get(device_type, "unknown")


class DeyeInverter(InverterDriver):
    """Deye / Sunsynk hybrid inverter behind a Solarman V5 logger."""
    name = "deye"
    display_name = "Deye / Sunsynk (Solarman logger)"
    required_settings = ("INVERTER_IP", "LOGGER_SERIAL")

    def __init__(self, ip: str, serial: int, port: int = 8899,
                 config: InverterConfig = None):
        super().__init__(config)
        self.ip = ip
        self.serial = serial
        self.port = port
        self.inverter = None   # PySolarmanV5 connection while open
        self._health = {}           # last temperatures / battery totals / codes (3-phase)
        self._health_read_at = None  # time.monotonic() of that read
        self._prefetched = {}       # address -> value read in blocks during a poll
        self._last_read_cached = False

    @classmethod
    def from_settings(cls, settings):
        """INVERTER_IP + LOGGER_SERIAL (the Solarman logger's serial number)."""
        return cls(settings.get("INVERTER_IP", ""), parse_logger_serial(settings.get("LOGGER_SERIAL")))

    @classmethod
    def settings_problem(cls, settings):
        if str(settings.get("INVERTER_IP") or "").strip() == "0.0.0.0":
            settings = {**settings, "INVERTER_IP": ""}
        problem = super().settings_problem(settings)
        if not problem and not parse_logger_serial(settings.get("LOGGER_SERIAL")):
            problem = f"LOGGER_SERIAL must be a logger serial number (1..{MAX_LOGGER_SERIAL})"
        return problem

    @classmethod
    def discover(cls):
        """Solarman loggers on the local /24 network (port 8899 open)."""
        from discover_inverter import discover
        return discover(quiet=True)

    @property
    def high_voltage(self):
        """HV 3-phase models: battery voltage in 0.1 V, battery/PV power in 10 W."""
        return self.config.family == "hybrid_3p_hv"

    def family_supported(self, family):
        return family not in UNSUPPORTED_FAMILIES

    def battery_voltage_range(self):
        # 48 V LiFePO4 window; HV battery stacks: anything a real stack can have
        return (100.0, 1000.0) if self.high_voltage else super().battery_voltage_range()

    def battery_nominal_voltage(self):
        # HV stacks come in many sizes: unknown (the live voltage is used instead)
        return None if self.high_voltage else super().battery_nominal_voltage()

    def connect(self):
        """Establish connection to inverter."""
        self.disconnect()
        logger.info("Connecting to inverter at %s:%d (serial=%d)", self.ip, self.port, self.serial)
        self.inverter = PySolarmanV5(
            address=self.ip,
            serial=self.serial,
            port=self.port,
            mb_slave_id=1,
            verbose=False,
            socket_timeout=10
        )
        logger.info("Connected to inverter")

    def disconnect(self):
        """Close connection. Forgets the client first: if closing fails, the next
        read still opens a new connection instead of reusing a dead one."""
        client, self.inverter = self.inverter, None
        if client:
            logger.debug("Disconnecting from inverter")
            try:
                client.disconnect()
            except Exception as e:
                logger.debug("Disconnect failed: %s", e)

    def read_register(self, address: int) -> int:
        """Read a single holding register (from the poll's prefetched blocks if there)."""
        value = self._prefetched.get(address)
        if value is not None:
            self._last_read_cached = True
            return value
        self._last_read_cached = False
        return self.inverter.read_holding_registers(address, 1)[0]

    def _prefetch(self, blocks):
        """Read the poll's register blocks, one request each (the logger handles
        one request at a time, so ~9 instead of ~27 makes a poll much shorter).
        A block the inverter refuses is just left out: its registers are then read
        one by one. No answer at all ends the poll (CONNECTION_ERRORS)."""
        self._prefetched = {}
        for start, count in blocks:
            try:
                values = self.read_registers(start, count)
            except CONNECTION_ERRORS:
                raise
            except Exception as e:
                logger.debug("Block read %d+%d failed, reading one by one: %s", start, count, e)
                values = []
            if len(values) == count:
                self._prefetched.update(zip(range(start, start + count), values))
            time.sleep(0.05)

    def _pause(self):
        """The pause between requests that spares the logger; none after a value
        that came from a prefetched block."""
        if not self._last_read_cached:
            time.sleep(0.05)

    def read_registers(self, address: int, count: int) -> list:
        """Read consecutive holding registers in one request."""
        return list(self.inverter.read_holding_registers(address, count))

    def _read_health_3p(self, data):
        """inverter_state on every read, the rest of the health fields from the last
        health read (see HEALTH_READ_SECONDS). A failed health read only leaves
        them out; the readings themselves are fine."""
        state = None
        try:
            state = INVERTER_STATES.get(self.read_register(REG_STATE_3P))
            time.sleep(0.05)
            problem = state in ("alarm", "fault")
            now = time.monotonic()
            if problem or self._health_read_at is None or now - self._health_read_at >= HEALTH_READ_SECONDS:
                health = {}
                dc, ac = self.read_registers(REG_TEMPERATURES_3P, 2)
                time.sleep(0.05)
                health["inverter_temperature_dc"] = inverter_temperature(dc)
                health["inverter_temperature_ac"] = inverter_temperature(ac)
                if self.config.has_battery:
                    charged_lo, charged_hi, discharged_lo, discharged_hi = self.read_registers(REG_BATTERY_TOTALS_3P, 4)
                    time.sleep(0.05)
                    health["battery_total_charge_kwh"] = total_kwh(charged_lo, charged_hi)
                    health["battery_total_discharge_kwh"] = total_kwh(discharged_lo, discharged_hi)
                if problem:
                    health["inverter_warning_codes"] = bit_codes(self.read_registers(REG_WARNINGS_3P, 2), "W")
                    time.sleep(0.05)
                    health["inverter_fault_codes"] = bit_codes(self.read_registers(REG_FAULTS_3P, 4), "F")
                    time.sleep(0.05)
                self._health, self._health_read_at = health, now
        except Exception as e:
            logger.warning("Inverter health read failed: %s", e)
        data["inverter_state"] = state
        data.update({key: value for key, value in self._health.items() if not key.endswith("_codes")})
        # Codes only while the inverter itself reports a problem: on a normal
        # inverter nothing shows, whatever those registers hold
        problem = state in ("alarm", "fault")
        data["inverter_warning_codes"] = self._health.get("inverter_warning_codes", []) if problem else []
        data["inverter_fault_codes"] = self._health.get("inverter_fault_codes", []) if problem else []

    def read_all_data(self) -> dict:
        """Read all inverter data and return as dictionary."""
        with self.lock:
            return self._read_all_data_unlocked()

    def _read_all_data_unlocked(self) -> dict:
        """Internal: read all data (caller must hold self.lock)."""
        if self.config.phases == 1:
            return self._read_1p_data_unlocked()
        return self._read_3p_data_unlocked()

    def _read_1p_data_unlocked(self) -> dict:
        """Read all data from single-phase hybrid inverter (Sunsynk register map)."""
        data = {}

        try:
            if not self.inverter:
                self.connect()

            # Solar PV
            data["pv1_power"] = self.read_register(186)
            time.sleep(0.05)
            if self.config.pv_strings >= 2:
                data["pv2_power"] = self.read_register(187)
                time.sleep(0.05)
            else:
                data["pv2_power"] = 0
            data["pv_total_power"] = data["pv1_power"] + data["pv2_power"]

            # Battery
            if self.config.has_battery:
                data["battery_voltage"] = self.read_register(183) / 100
                time.sleep(0.05)
                raw_current = self.read_register(191)
                data["battery_current"] = -to_signed(raw_current) / 100
                time.sleep(0.05)

                raw_soc = self.read_register(184)
                time.sleep(0.05)
                data["battery_soc_raw"] = raw_soc
                data["battery_temperature"] = battery_temperature(self.read_register(182))
                time.sleep(0.05)

                data["battery_nominal_voltage"] = self.read_register(236) / 100
                time.sleep(0.05)

                # For the runtime estimate: 204 = battery capacity (Ah), 217 = shutdown SOC
                data["battery_capacity_ah"] = self.read_register(204)
                time.sleep(0.05)
                data["battery_shutdown_soc"] = self.read_register(217)
                time.sleep(0.05)

                self.smooth_battery(data)   # battery_voltage / battery_soc over the last reads
                data["battery_power"] = int(data["battery_voltage"] * data["battery_current"])
            else:
                data["battery_voltage"] = 0
                data["battery_current"] = 0
                data["battery_soc"] = 0
                data["battery_soc_raw"] = 0
                data["battery_power"] = 0

            # Grid
            data["grid_voltage"] = self.read_register(150) / 10
            data["grid_online"] = grid_online([data["grid_voltage"]])
            data["grid_state"] = "online" if data["grid_online"] else "outage"
            time.sleep(0.05)
            raw_grid_power = self.read_register(169)
            data["grid_power"] = to_signed(raw_grid_power)
            time.sleep(0.05)

            # Load
            # Loads and generator power are signed (like grid power)
            data["load_power"] = to_signed(self.read_register(178))
            time.sleep(0.05)
            data["load_l1"] = to_signed(self.read_register(176))
            time.sleep(0.05)

            # Daily stats
            data["daily_pv"] = self.read_register(108) / 10
            time.sleep(0.05)
            data["daily_grid_import"] = self.read_register(76) / 10
            time.sleep(0.05)
            data["daily_grid_export"] = self.read_register(77) / 10
            time.sleep(0.05)
            data["daily_load"] = self.read_register(84) / 10
            time.sleep(0.05)

            # Generator (GEN/GRID2 port) — 1-phase Sunsynk uses register 166
            if self.config.has_generator:
                data["generator_power"] = to_signed(self.read_register(166))
                time.sleep(0.05)
            else:
                data["generator_power"] = 0

            # Status indicators
            if self.config.has_battery:
                if data["battery_current"] > 0:
                    data["battery_status"] = "Charging"
                elif data["battery_current"] < 0:
                    data["battery_status"] = "Discharging"
                else:
                    data["battery_status"] = "Idle"
            else:
                data["battery_status"] = "N/A"

        except Exception as e:
            logger.warning("Error reading inverter data: %s", error_text(e))
            data["error"] = error_text(e)
        finally:
            self.disconnect()

        return data

    def _read_3p_data_unlocked(self) -> dict:
        """Read all data from 3-phase hybrid inverter (original register map)."""
        data = {}

        try:
            if not self.inverter:
                self.connect()
            self._prefetch(BLOCKS_3P)
            # HV models report battery voltage in 0.1 V and battery/PV power in 10 W
            hv = self.high_voltage
            power_scale = 10 if hv else 1

            # Solar PV: 672/673 = PV1/PV2 power (514/515 are today's battery
            # charge/discharge energy, not PV)
            data["pv1_power"] = self.read_register(672) * power_scale
            self._pause()
            if self.config.pv_strings >= 2:
                data["pv2_power"] = self.read_register(673) * power_scale
                self._pause()
            else:
                data["pv2_power"] = 0
            data["pv_total_power"] = data["pv1_power"] + data["pv2_power"]

            # Battery
            if self.config.has_battery:
                data["battery_voltage"] = self.read_register(587) / (10 if hv else 100)
                self._pause()
                # 590/591: battery power (W) / current (0.01 A), signed, Deye
                # convention discharge > 0; negated so charging is positive here.
                # (586 is the battery temperature, not the current.)
                data["battery_current"] = -to_signed(self.read_register(591)) / 100
                self._pause()
                battery_power = -to_signed(self.read_register(590)) * power_scale
                self._pause()

                # SOC from register 588 (BMS-reported, same as Solarman); shown as
                # the median of the last reads (smooth_battery)
                raw_soc = self.read_register(588)
                self._pause()
                data["battery_soc_raw"] = raw_soc
                data["battery_temperature"] = battery_temperature(self.read_register(586))
                self._pause()

                # For the runtime estimate: 592 = capacity measured by the BMS
                # ("corrected"), 102 = capacity set on the inverter, 115 = SOC at
                # which the inverter stops discharging
                capacity_ah = self.read_register(592)
                self._pause()
                if not capacity_ah:
                    capacity_ah = self.read_register(102)
                    self._pause()
                data["battery_capacity_ah"] = capacity_ah
                data["battery_nominal_voltage"] = self.battery_nominal_voltage()
                data["battery_shutdown_soc"] = self.read_register(115)
                self._pause()

                self.smooth_battery(data)   # battery_voltage / battery_soc over the last reads
                data["battery_power"] = battery_power
            else:
                data["battery_voltage"] = 0
                data["battery_current"] = 0
                data["battery_soc"] = 0
                data["battery_soc_raw"] = 0
                data["battery_power"] = 0

            # Grid: 598-600 = grid voltage L1-L3 (598 alone drives online/outage detection)
            data["grid_voltage"] = self.read_register(598) / 10
            self._pause()
            if self.config.phases == 3:
                data["grid_voltage_l1"] = data["grid_voltage"]
                data["grid_voltage_l2"] = self.read_register(599) / 10
                self._pause()
                data["grid_voltage_l3"] = self.read_register(600) / 10
                self._pause()
            data["grid_relay"] = self.read_register(552)
            self._pause()
            grid_volts = [data["grid_voltage"], data.get("grid_voltage_l2"), data.get("grid_voltage_l3")]
            data["grid_online"] = grid_online(grid_volts, relay_grid_connected(data["grid_relay"]))
            data["grid_state"] = grid_state(data["grid_online"], grid_volts)
            data["grid_phases_missing"] = grid_phases_missing(grid_volts)
            raw_grid_power = self.read_register(607)
            data["grid_power"] = to_signed(raw_grid_power)
            self._pause()

            # Load (loads and generator power are signed, like grid power)
            data["load_power"] = to_signed(self.read_register(653))
            self._pause()

            # Daily stats
            data["daily_pv"] = self.read_register(529) / 10  # day PV energy (502 = inverter output)
            self._pause()
            data["daily_grid_import"] = self.read_register(520) / 10
            self._pause()
            data["daily_grid_export"] = self.read_register(521) / 10
            self._pause()
            data["daily_load"] = self.read_register(526) / 10
            self._pause()

            # Phase data (3-phase system)
            if self.config.phases == 3:
                data["load_l1"] = to_signed(self.read_register(650))
                self._pause()
                data["load_l2"] = to_signed(self.read_register(651))
                self._pause()
                data["load_l3"] = to_signed(self.read_register(652))
                self._pause()

                data["voltage_l1"] = self.read_register(644) / 10
                self._pause()
                data["voltage_l2"] = self.read_register(645) / 10
                self._pause()
                data["voltage_l3"] = self.read_register(646) / 10

            # Generator (GEN/GRID2 port) — 3-phase uses register 667
            if self.config.has_generator:
                data["generator_power"] = to_signed(self.read_register(667))
                self._pause()
            else:
                data["generator_power"] = 0

            # Status indicators
            if self.config.has_battery:
                if data["battery_power"] > 0:
                    data["battery_status"] = "Charging"
                elif data["battery_power"] < 0:
                    data["battery_status"] = "Discharging"
                else:
                    data["battery_status"] = "Idle"
            else:
                data["battery_status"] = "N/A"

            self._read_health_3p(data)

        except Exception as e:
            logger.warning("Error reading inverter data: %s", error_text(e))
            data["error"] = error_text(e)
        finally:
            self._prefetched = {}
            self.disconnect()

        return data

    def detect_config(self):
        """Auto-detect inverter configuration; see _detect_config(). Always disconnects."""
        try:
            return self._detect_config()
        finally:
            with self.lock:
                self.disconnect()

    def read_device_type(self, disconnect=True):
        """Register 0 (device type) -> (code or None, family). Never raises.

        detect_config() passes disconnect=False: it reads more registers on the
        same connection and disconnects at the end itself.
        """
        try:
            with self.lock:
                try:
                    if not self.inverter:
                        self.connect()
                    code = self.read_register(0)
                finally:
                    if disconnect:
                        self.disconnect()
        except Exception as e:
            logger.info("Device type read failed: %s", e)
            return None, "unknown"
        family = device_family(code)
        logger.info("Device type 0x%04X -> %s", code, family)
        return (code or None), family

    def _detect_config(self):
        """Auto-detect inverter configuration by reading diagnostic registers.

        Two-stage detection:
        Stage 1 — Read L2/L3 voltage to determine phase count.
        Stage 2 — Read battery voltage and PV2 power using the register map
                   appropriate for the detected phase count (3-phase vs Sunsynk).
        Takes 3 samples per stage with 2s delay; any positive reading wins.
        """
        # --- Stage 0: device type tells the family (and so the phase count) ---
        device_type, family = self.read_device_type(disconnect=False)

        # --- Stage 1: detect phase count from L2/L3 voltage (unknown family only) ---
        phases_3 = False
        answered = 0   # samples the inverter answered: without any, there's nothing to go by

        for i in range(0 if family in FAMILY_PHASES else 3):
            logger.info("detect_config stage 1: taking sample %d/3 (phase detection)", i + 1)
            with self.lock:
                if not self.inverter:
                    self.connect()

                v_l2 = v_l3 = 0
                try:
                    v_l2 = self.read_register(645) / 10
                    time.sleep(0.05)
                    v_l3 = self.read_register(646) / 10
                    time.sleep(0.05)
                    answered += 1
                except Exception as e:
                    logger.info("detect_config stage 1: L2/L3 voltage read failed: %s", e)

            logger.info(
                "detect_config stage 1: sample %d — v_l2=%.1f v_l3=%.1f",
                i + 1, v_l2, v_l3,
            )

            if v_l2 > 50 or v_l3 > 50:
                phases_3 = True

            if i < 2:
                time.sleep(2)

        if family in FAMILY_PHASES:
            detected_phases = FAMILY_PHASES[family]
        elif not answered:
            # A busy logger isn't a single-phase inverter: keep the current config
            raise RuntimeError("the inverter didn't answer the phase detection reads")
        else:
            detected_phases = 3 if phases_3 else 1
        logger.info("detect_config stage 1 result: phases=%d (family %s)", detected_phases, family)

        # --- Stage 2: detect battery & PV2 using correct register map ---
        if detected_phases == 3:
            bat_reg, pv2_reg = 587, 673
        else:
            bat_reg, pv2_reg = 183, 187
        logger.info(
            "detect_config stage 2: using %s register map (battery=%d, pv2=%d)",
            "3-phase" if detected_phases == 3 else "single-phase (Sunsynk)",
            bat_reg, pv2_reg,
        )

        has_battery = False
        pv2_detected = False

        for i in range(3):
            logger.info("detect_config stage 2: taking sample %d/3 (battery & PV2)", i + 1)
            with self.lock:
                if not self.inverter:
                    self.connect()

                bat_v = 0
                try:
                    bat_v = self.read_register(bat_reg) / 100
                    time.sleep(0.05)
                except Exception as e:
                    logger.info("detect_config stage 2: battery voltage read failed: %s", e)

                pv2_w = 0
                try:
                    pv2_w = self.read_register(pv2_reg)
                except Exception as e:
                    logger.info("detect_config stage 2: PV2 power read failed: %s", e)

            logger.info(
                "detect_config stage 2: sample %d — bat_v=%.2f (reg %d) pv2_w=%d (reg %d)",
                i + 1, bat_v, bat_reg, pv2_w, pv2_reg,
            )

            if bat_v > 10:
                has_battery = True
            if pv2_w > 0:
                pv2_detected = True

            if i < 2:
                time.sleep(2)

        if not has_battery:
            logger.warning(
                "detect_config: battery voltage was 0 in all samples "
                "(may be unreliable during glitches), defaulting to has_battery=True"
            )
            has_battery = True

        if not pv2_detected:
            logger.warning(
                "detect_config: PV2 power was 0 in all samples "
                "(may be unreliable at night), defaulting to 2 strings"
            )
            pv2_detected = True

        # --- Stage 3: detect generator (GEN/GRID2 port) ---
        gen_reg = 667 if detected_phases == 3 else 166
        logger.info(
            "detect_config stage 3: using register %d for generator detection",
            gen_reg,
        )

        has_generator = False
        gen_answered = 0

        for i in range(3):
            logger.info("detect_config stage 3: taking sample %d/3 (generator)", i + 1)
            with self.lock:
                if not self.inverter:
                    self.connect()

                gen_w = 0
                try:
                    gen_w = to_signed(self.read_register(gen_reg))
                    gen_answered += 1
                except Exception as e:
                    logger.info("detect_config stage 3: generator read failed: %s", e)

            logger.info(
                "detect_config stage 3: sample %d — gen_w=%d (reg %d)",
                i + 1, gen_w, gen_reg,
            )

            if gen_w > 0:
                has_generator = True

            if i < 2:
                time.sleep(2)

        if not gen_answered:
            raise RuntimeError("the inverter didn't answer the generator detection reads")
        logger.info("detect_config stage 3 result: has_generator=%s", has_generator)

        config = InverterConfig(
            phases=detected_phases,
            has_battery=has_battery,
            pv_strings=2 if pv2_detected else 1,
            has_generator=has_generator,
            device_type=device_type,
            family=family,
            supported=self.family_supported(family),
        )
        logger.info("Auto-detected inverter config: %s", config)
        return config
