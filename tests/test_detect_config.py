"""Tests for DeyeInverter.detect_config() two-stage detection."""
import pytest
from unittest.mock import patch, MagicMock
from inverters import DeyeInverter, InverterConfig


@pytest.fixture(autouse=True)
def no_sleep():
    """detect_config() sleeps 2s between samples; skip that in tests."""
    with patch("inverters.deye.time.sleep"):
        yield


@pytest.fixture
def inverter():
    """Create a DeyeInverter with a mocked PySolarmanV5 connection."""
    with patch("inverters.deye.PySolarmanV5"):
        inv = DeyeInverter(ip="192.168.1.1", serial=123456)
        inv.inverter = MagicMock()
        return inv


def _mock_read_register(register_values):
    """Return a side_effect function that maps register addresses to values."""
    def read_register(addr):
        return register_values.get(addr, 0)
    return read_register


class TestDetectConfig3Phase:
    """Stage 1 detects 3-phase → Stage 2 reads 3-phase registers (587, 673)."""

    def test_3phase_with_battery_and_pv2(self, inverter):
        inverter.read_register = _mock_read_register({
            645: 2300,   # L2 = 230V → 3-phase
            646: 2310,   # L3 = 231V
            587: 5200,   # battery = 52.00V
            673: 300,    # PV2 = 300W
        })
        config = inverter.detect_config()
        assert config.phases == 3
        assert config.has_battery is True
        assert config.pv_strings == 2

    def test_3phase_no_battery_defaults_true(self, inverter):
        inverter.read_register = _mock_read_register({
            645: 2300,
            646: 2310,
            587: 0,      # battery voltage 0 in all samples
            673: 300,
        })
        config = inverter.detect_config()
        assert config.phases == 3
        assert config.has_battery is True  # defaults to True
        assert config.pv_strings == 2

    def test_3phase_no_pv2_defaults_two_strings(self, inverter):
        inverter.read_register = _mock_read_register({
            645: 2300,
            646: 2310,
            587: 5200,
            673: 0,      # PV2 power 0 in all samples
        })
        config = inverter.detect_config()
        assert config.phases == 3
        assert config.has_battery is True
        assert config.pv_strings == 2  # defaults to 2

    def test_3phase_only_l2_above_threshold(self, inverter):
        inverter.read_register = _mock_read_register({
            645: 2300,   # L2 = 230V → enough for 3-phase
            646: 0,      # L3 = 0V
            587: 5200,
            673: 300,
        })
        config = inverter.detect_config()
        assert config.phases == 3

    def test_3phase_only_l3_above_threshold(self, inverter):
        inverter.read_register = _mock_read_register({
            645: 0,      # L2 = 0V
            646: 2300,   # L3 = 230V → enough for 3-phase
            587: 5200,
            673: 300,
        })
        config = inverter.detect_config()
        assert config.phases == 3


class TestDetectConfigSinglePhase:
    """Stage 1 detects single-phase → Stage 2 reads Sunsynk registers (183, 187)."""

    def test_1phase_with_battery_and_pv2(self, inverter):
        inverter.read_register = _mock_read_register({
            645: 0,      # L2 = 0V → single-phase
            646: 0,      # L3 = 0V
            183: 5200,   # battery = 52.00V (Sunsynk register)
            187: 300,    # PV2 = 300W (Sunsynk register)
        })
        config = inverter.detect_config()
        assert config.phases == 1
        assert config.has_battery is True
        assert config.pv_strings == 2

    def test_1phase_no_battery_defaults_true(self, inverter):
        inverter.read_register = _mock_read_register({
            645: 0,
            646: 0,
            183: 0,      # battery voltage 0
            187: 300,
        })
        config = inverter.detect_config()
        assert config.phases == 1
        assert config.has_battery is True  # defaults to True

    def test_1phase_no_pv2_defaults_two_strings(self, inverter):
        inverter.read_register = _mock_read_register({
            645: 0,
            646: 0,
            183: 5200,
            187: 0,      # PV2 power 0
        })
        config = inverter.detect_config()
        assert config.phases == 1
        assert config.pv_strings == 2  # defaults to 2

    def test_1phase_does_not_read_3phase_battery_register(self, inverter):
        """Ensure single-phase path reads 183, not 587."""
        read_registers = []
        original_values = {645: 0, 646: 0, 183: 5200, 187: 300}

        def tracking_read(addr):
            read_registers.append(addr)
            return original_values.get(addr, 0)

        inverter.read_register = tracking_read
        inverter.detect_config()

        assert 587 not in read_registers, "Should not read 3-phase battery register 587"
        assert 673 not in read_registers, "Should not read 3-phase PV2 register 673"
        assert 183 in read_registers, "Should read Sunsynk battery register 183"
        assert 187 in read_registers, "Should read Sunsynk PV2 register 187"

    def test_3phase_does_not_read_sunsynk_registers(self, inverter):
        """Ensure 3-phase path reads 587/673, not 183/187."""
        read_registers = []
        original_values = {645: 2300, 646: 2310, 587: 5200, 673: 300}

        def tracking_read(addr):
            read_registers.append(addr)
            return original_values.get(addr, 0)

        inverter.read_register = tracking_read
        inverter.detect_config()

        assert 183 not in read_registers, "Should not read Sunsynk battery register 183"
        assert 187 not in read_registers, "Should not read Sunsynk PV2 register 187"
        assert 587 in read_registers, "Should read 3-phase battery register 587"
        assert 673 in read_registers, "Should read 3-phase PV2 register 673"


class TestDetectConfigEdgeCases:
    """Edge cases: read failures, threshold boundaries, intermittent readings."""

    def test_l2_l3_below_threshold_is_single_phase(self, inverter):
        """Voltages at or below 50V should not trigger 3-phase."""
        inverter.read_register = _mock_read_register({
            645: 500,    # L2 = 50.0V — exactly at threshold, not above
            646: 400,    # L3 = 40.0V
            183: 5200,
            187: 300,
        })
        config = inverter.detect_config()
        assert config.phases == 1

    def test_l2_just_above_threshold_is_3phase(self, inverter):
        """Voltage just above 50V should trigger 3-phase."""
        inverter.read_register = _mock_read_register({
            645: 510,    # L2 = 51.0V — above 50V threshold
            646: 0,
            587: 5200,
            673: 300,
        })
        config = inverter.detect_config()
        assert config.phases == 3

    def test_battery_at_threshold_not_detected(self, inverter):
        """Battery voltage exactly 10V should not count as detected."""
        inverter.read_register = _mock_read_register({
            645: 2300,
            646: 2300,
            587: 1000,   # 10.00V — exactly at threshold
            673: 300,
        })
        config = inverter.detect_config()
        assert config.has_battery is True  # defaults to True when not detected

    def test_battery_just_above_threshold_detected(self, inverter):
        """Battery voltage above 10V should be detected."""
        inverter.read_register = _mock_read_register({
            645: 2300,
            646: 2300,
            587: 1100,   # 11.00V — above 10V threshold
            673: 300,
        })
        config = inverter.detect_config()
        assert config.has_battery is True

    def test_stage1_read_failure_keeps_the_current_config(self, inverter):
        """L2/L3 reads all failing is a busy logger, not a single-phase inverter:
        detection gives up (the caller keeps the config it has)."""
        def failing_read(addr):
            if addr in (645, 646):
                raise Exception("connection timeout")
            return {183: 5200, 187: 300}.get(addr, 0)

        inverter.read_register = failing_read
        with pytest.raises(RuntimeError, match="phase detection"):
            inverter.detect_config()

    def test_stage2_read_failure_defaults_to_battery_and_pv2(self, inverter):
        """If battery/PV2 reads fail, should default to has_battery=True, pv_strings=2."""
        def failing_read(addr):
            if addr in (587, 673):
                raise Exception("connection timeout")
            return {645: 2300, 646: 2300}.get(addr, 0)

        inverter.read_register = failing_read
        config = inverter.detect_config()
        assert config.phases == 3
        assert config.has_battery is True  # default
        assert config.pv_strings == 2      # default

    def test_all_reads_fail_gives_up(self, inverter):
        """If everything fails there's nothing to detect from: no config at all
        (the caller keeps the one it has)."""
        def always_fail(addr):
            raise Exception("connection timeout")

        inverter.read_register = always_fail
        with pytest.raises(RuntimeError):
            inverter.detect_config()
        assert inverter.inverter is None   # disconnected all the same

    def test_intermittent_3phase_detection(self, inverter):
        """If only one out of three samples detects 3-phase, it still counts."""
        call_count = [0]
        def intermittent_read(addr):
            if addr == 645:
                call_count[0] += 1
                # Only the 2nd sample shows voltage
                if call_count[0] == 2:
                    return 2300
                return 0
            if addr == 646:
                return 0
            return {587: 5200, 673: 300}.get(addr, 0)

        inverter.read_register = intermittent_read
        config = inverter.detect_config()
        assert config.phases == 3

    def test_returns_inverter_config_instance(self, inverter):
        inverter.read_register = _mock_read_register({
            645: 2300, 646: 2300, 587: 5200, 673: 300,
        })
        config = inverter.detect_config()
        assert isinstance(config, InverterConfig)

    def test_all_zeros_returns_single_phase_with_defaults(self, inverter):
        """All registers returning 0 → single-phase with default battery/PV2."""
        inverter.read_register = _mock_read_register({})
        config = inverter.detect_config()
        assert config.phases == 1
        assert config.has_battery is True
        assert config.pv_strings == 2


class TestDetectConfigGenerator:
    """Stage 3 detects generator on GEN/GRID2 port."""

    def test_3phase_generator_detected(self, inverter):
        """3-phase: register 667 returns >0 → has_generator=True."""
        inverter.read_register = _mock_read_register({
            645: 2300, 646: 2310,
            587: 5200, 673: 300,
            667: 3000,   # generator running at 3000W
        })
        config = inverter.detect_config()
        assert config.phases == 3
        assert config.has_generator is True

    def test_3phase_no_generator(self, inverter):
        """3-phase: register 667 returns 0 → has_generator=False."""
        inverter.read_register = _mock_read_register({
            645: 2300, 646: 2310,
            587: 5200, 673: 300,
            667: 0,
        })
        config = inverter.detect_config()
        assert config.phases == 3
        assert config.has_generator is False

    def test_1phase_generator_detected(self, inverter):
        """1-phase: register 166 returns >0 → has_generator=True."""
        inverter.read_register = _mock_read_register({
            645: 0, 646: 0,
            183: 5200, 187: 300,
            166: 5000,   # generator running at 5000W
        })
        config = inverter.detect_config()
        assert config.phases == 1
        assert config.has_generator is True

    def test_1phase_no_generator(self, inverter):
        """1-phase: register 166 returns 0 → has_generator=False."""
        inverter.read_register = _mock_read_register({
            645: 0, 646: 0,
            183: 5200, 187: 300,
            166: 0,
        })
        config = inverter.detect_config()
        assert config.phases == 1
        assert config.has_generator is False

    def test_generator_register_read_failure_gives_up(self, inverter):
        """Generator reads all failing: no answer isn't "no generator", so no
        config (a generator found before stays found)."""
        def failing_gen_read(addr):
            if addr in (667, 166):
                raise Exception("connection timeout")
            return {645: 2300, 646: 2300, 587: 5200, 673: 300}.get(addr, 0)

        inverter.read_register = failing_gen_read
        with pytest.raises(RuntimeError, match="generator"):
            inverter.detect_config()

    def test_one_answered_generator_sample_is_enough(self, inverter):
        calls = []

        def flaky_gen_read(addr):
            if addr == 667:
                calls.append(addr)
                if len(calls) < 3:
                    raise Exception("connection timeout")
            return {645: 2300, 646: 2300, 587: 5200, 673: 300}.get(addr, 0)

        inverter.read_register = flaky_gen_read
        config = inverter.detect_config()
        assert config.phases == 3 and config.has_generator is False


def test_detect_config_disconnects_afterwards(inverter):
    inverter.read_register = _mock_read_register({645: 2300, 646: 2300, 587: 5200})
    conn = inverter.inverter
    inverter.detect_config()
    conn.disconnect.assert_called()
    assert inverter.inverter is None


class TestDeviceType:
    """Register 0 (device type) selects the family and skips voltage-based phase detection."""

    @pytest.mark.parametrize("code,family,phases", [
        (0x0005, "hybrid_3p_lv", 3), (0x0500, "hybrid_3p_lv", 3),
        (0x0006, "hybrid_3p_hv", 3), (0x0601, "hybrid_3p_hv", 3),
        (0x0003, "hybrid_1p", 1), (0x0300, "hybrid_1p", 1),
    ])
    def test_known_family_sets_phases(self, inverter, code, family, phases):
        read = []
        def reg(addr):
            read.append(addr)
            return {0: code, 587: 5200, 183: 5200}.get(addr, 0)
        inverter.read_register = MagicMock(side_effect=reg)
        config = inverter.detect_config()
        assert (config.family, config.device_type, config.phases) == (family, code, phases)
        assert 645 not in read and 646 not in read   # no L2/L3 voltage sampling needed

    def test_unknown_code_falls_back_to_voltage_detection(self, inverter):
        inverter.read_register = MagicMock(side_effect=_mock_read_register({0: 0x7777, 645: 2300, 646: 2300}))
        config = inverter.detect_config()
        assert config.family == "unknown" and config.phases == 3

    @pytest.mark.parametrize("code", [0x0002, 0x0004, 0x0400])
    def test_string_and_micro_flagged_unsupported(self, inverter, code):
        inverter.read_register = MagicMock(side_effect=_mock_read_register({0: code}))
        assert inverter.detect_config().to_dict()["supported"] is False

    def test_device_type_read_failure_is_unknown(self, inverter):
        def reg(addr):
            if addr == 0:
                raise Exception("timeout")
            return {645: 2300, 646: 2300}.get(addr, 0)
        inverter.read_register = MagicMock(side_effect=reg)
        config = inverter.detect_config()
        assert config.family == "unknown" and config.device_type is None and config.phases == 3


def test_disconnect_failure_still_forgets_the_connection(inverter):
    """A client whose disconnect() raises must not be reused by the next read."""
    client = inverter.inverter
    client.disconnect.side_effect = OSError("broken pipe")
    inverter.disconnect()
    assert inverter.inverter is None
    client.disconnect.assert_called_once()
