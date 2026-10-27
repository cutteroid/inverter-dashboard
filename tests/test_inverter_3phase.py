"""Register mapping of the 3-phase read path (_read_3p_data_unlocked)."""
from unittest.mock import patch

import pytest

from inverters import DeyeInverter, InverterConfig


def u16(value):
    """Signed value as the unsigned 16-bit word the inverter returns."""
    return value & 0xFFFF


# Live readings from a SG04LP3-style inverter (no PV connected) while the battery charged at ~3.3 kW
REGISTERS = {
    514: 24, 515: 4,       # today's battery charge / discharge (0.1 kWh), NOT PV power
    529: 0,                # PV energy today
    672: 0, 673: 0,        # PV1 / PV2 power: no panels connected
    676: 5, 678: 21,       # residual PV voltages on unconnected inputs
    586: 1270,             # battery temperature: (1270 - 1000) / 10 = 27.0 °C
    587: 5473,             # 54.73 V
    588: 96,
    590: u16(-3300),       # battery power, Deye sign: negative = charging
    591: u16(-6030),       # battery current, -60.30 A
    598: 2276, 599: 2251, 600: 2154,
    607: 5454, 653: 2171,
    650: 252, 651: 113, 652: 1806,
    644: 2275, 645: 2251, 646: 2154,
}


@pytest.fixture
def inverter():
    with patch("inverters.deye.PySolarmanV5"), patch("inverters.deye.time.sleep"):
        inv = DeyeInverter(ip="192.168.1.1", serial=123456, config=InverterConfig(phases=3))
        with patch.object(inv, "read_register", side_effect=lambda addr: REGISTERS.get(addr, 0)):
            yield inv


def test_battery_charging_from_power_and_current_registers(inverter):
    data = inverter._read_3p_data_unlocked()
    assert data["battery_power"] == 3300           # charging is positive in the API
    assert data["battery_current"] == pytest.approx(60.3)
    assert data["battery_status"] == "Charging"
    assert data["battery_voltage"] == pytest.approx(54.73)


def test_battery_discharging(inverter):
    REGISTERS_DIS = {**REGISTERS, 590: 1200, 591: 2200}
    with patch.object(inverter, "read_register", side_effect=lambda addr: REGISTERS_DIS.get(addr, 0)):
        data = inverter._read_3p_data_unlocked()
    assert data["battery_power"] == -1200 and data["battery_status"] == "Discharging"


def test_pv_from_power_registers_not_battery_counters(inverter):
    data = inverter._read_3p_data_unlocked()
    assert (data["pv1_power"], data["pv2_power"], data["pv_total_power"]) == (0, 0, 0)
    assert data["daily_pv"] == 0
    REGISTERS_SUN = {**REGISTERS, 672: 2100, 673: 1850, 529: 123}
    with patch.object(inverter, "read_register", side_effect=lambda addr: REGISTERS_SUN.get(addr, 0)):
        data = inverter._read_3p_data_unlocked()
    assert data["pv_total_power"] == 3950 and data["daily_pv"] == pytest.approx(12.3)


def test_grid_voltage_per_phase(inverter):
    data = inverter._read_3p_data_unlocked()
    assert data["grid_voltage"] == pytest.approx(227.6)
    assert (data["grid_voltage_l1"], data["grid_voltage_l2"], data["grid_voltage_l3"]) == \
        pytest.approx((227.6, 225.1, 215.4))


def test_high_voltage_model_scaling():
    """HV 3-phase: battery voltage in 0.1 V, battery and PV power in 10 W."""
    regs = {**REGISTERS, 587: 3125, 590: u16(-330), 672: 210, 673: 185}
    with patch("inverters.deye.PySolarmanV5"), patch("inverters.deye.time.sleep"):
        inv = DeyeInverter(ip="192.168.1.1", serial=1, config=InverterConfig(phases=3, family="hybrid_3p_hv"))
        with patch.object(inv, "read_register", side_effect=lambda addr: regs.get(addr, 0)):
            data = inv._read_3p_data_unlocked()
    assert data["battery_voltage"] == pytest.approx(312.5)
    assert data["battery_power"] == 3300 and data["battery_status"] == "Charging"
    assert (data["pv1_power"], data["pv2_power"]) == (2100, 1850)


def test_capacity_and_shutdown_soc_for_runtime(inverter):
    regs = {**REGISTERS, 592: 0, 102: 100, 115: 10}   # no BMS-measured capacity: use the setting
    with patch.object(inverter, "read_register", side_effect=lambda addr: regs.get(addr, 0)):
        data = inverter._read_3p_data_unlocked()
    assert data["battery_capacity_ah"] == 100 and data["battery_shutdown_soc"] == 10
    regs[592] = 280                                   # BMS-measured capacity wins
    with patch.object(inverter, "read_register", side_effect=lambda addr: regs.get(addr, 0)):
        assert inverter._read_3p_data_unlocked()["battery_capacity_ah"] == 280


@pytest.mark.parametrize("relay,volts,online", [
    (0x05, (227.6, 225.1, 215.4), True),    # inverter + grid relay closed
    (0x01, (238.0, 255.7, 239.0), False),   # grid relay open (phase over limit), voltage still present
    (0x01, (238.0, 0.0, 239.0), False),     # one phase cut
    (0x00, (227.6, 225.1, 215.4), True),    # relay register not supported: voltages decide
    (0x00, (227.6, 0.0, 215.4), False),     #   ... and every phase must have supply
])
def test_grid_online_from_relay_and_voltages(inverter, relay, volts, online):
    regs = {**REGISTERS, 552: relay, 598: int(volts[0] * 10), 599: int(volts[1] * 10), 600: int(volts[2] * 10)}
    with patch.object(inverter, "read_register", side_effect=lambda addr: regs.get(addr, 0)):
        data = inverter._read_3p_data_unlocked()
    assert data["grid_online"] is online and data["grid_relay"] == relay


@pytest.mark.parametrize("relay,volts,state,missing", [
    (0x05, (227.6, 225.1, 215.4), "online", []),
    (0x01, (251.1, 0.0, 245.8), "partial", [2]),          # live: L2 cut by the smart meter
    (0x01, (0.0, 0.0, 245.8), "partial", [1, 2]),
    (0x01, (238.0, 255.7, 239.0), "out_of_range", []),    # all phases there, inverter disconnected
    (0x01, (0.0, 0.2, 0.0), "outage", [1, 2, 3]),
])
def test_grid_state(inverter, relay, volts, state, missing):
    regs = {**REGISTERS, 552: relay, 598: int(volts[0] * 10), 599: int(volts[1] * 10), 600: int(volts[2] * 10)}
    with patch.object(inverter, "read_register", side_effect=lambda addr: regs.get(addr, 0)):
        data = inverter._read_3p_data_unlocked()
    assert data["grid_state"] == state and data["grid_phases_missing"] == missing


@pytest.mark.parametrize("raw,celsius", [(1270, 27.0), (980, -2.0), (0, None), (2500, None)])
def test_battery_temperature(inverter, raw, celsius):
    regs = {**REGISTERS, 586: raw}   # live: 1270 = 27.0 °C; 0 = not reported
    with patch.object(inverter, "read_register", side_effect=lambda addr: regs.get(addr, 0)):
        data = inverter._read_3p_data_unlocked()
    assert data["battery_temperature"] == (pytest.approx(celsius) if celsius is not None else None)


def test_battery_temperature_single_phase():
    with patch("inverters.deye.PySolarmanV5"), patch("inverters.deye.time.sleep"):
        inv = DeyeInverter(ip="192.168.1.1", serial=1, config=InverterConfig(phases=1))
        with patch.object(inv, "read_register", side_effect=lambda addr: {182: 1255, 183: 5300}.get(addr, 0)):
            assert inv._read_1p_data_unlocked()["battery_temperature"] == pytest.approx(25.5)


def test_poll_reads_neighbouring_registers_in_blocks():
    """~9 logger requests per poll instead of ~27 (one per register)."""
    from unittest.mock import MagicMock, patch
    from inverters import DeyeInverter
    regs = {586: 1250, 587: 5320, 588: 77, 590: 65436, 591: 65036, 592: 200,
            598: 2301, 599: 2310, 600: 2295, 644: 2290, 645: 2300, 646: 2310,
            650: 310, 651: 360, 652: 150, 653: 820, 520: 32, 521: 11, 526: 116, 529: 94,
            672: 1240, 673: 1110, 552: 4, 607: 430, 115: 10, 500: 2}
    with patch("inverters.deye.PySolarmanV5"):
        inv = DeyeInverter(ip="192.168.1.1", serial=123456)
    client = MagicMock()
    client.read_holding_registers.side_effect = lambda addr, count: [regs.get(a, 0) for a in range(addr, addr + count)]
    inv.inverter = client
    with patch("inverters.deye.time.sleep"):
        data = inv.read_all_data()
    assert "error" not in data
    assert data["pv1_power"] == 1240 and data["load_power"] == 820 and data["daily_grid_import"] == 3.2
    assert data["grid_voltage_l2"] == 231.0 and data["load_l3"] == 150 and data["battery_soc_raw"] == 77
    assert client.read_holding_registers.call_count <= 12


def test_a_failing_block_falls_back_to_single_reads():
    from unittest.mock import MagicMock, patch
    from inverters import DeyeInverter
    with patch("inverters.deye.PySolarmanV5"):
        inv = DeyeInverter(ip="192.168.1.1", serial=123456)
    client = MagicMock()

    def read(addr, count):
        if count > 1:
            raise ValueError("illegal data address")
        return [{672: 1240, 653: 820}.get(addr, 0)]
    client.read_holding_registers.side_effect = read
    inv.inverter = client
    with patch("inverters.deye.time.sleep"):
        data = inv.read_all_data()
    assert "error" not in data and data["pv1_power"] == 1240 and data["load_power"] == 820
