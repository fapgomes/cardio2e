"""Tests for the constant mapping tables."""

from cardio2e_modules.cardio2e_constants import (
    FAN_CODE_TO_STATE,
    FAN_STATE_TO_CODE,
    HVAC_CODE_TO_MODE,
    HVAC_MODE_TO_CODE,
    SECURITY_CODE_TO_STATE,
    SWITCH_CODE_TO_STATE,
)


class TestHvacModeMaps:
    def test_round_trip_all_modes(self):
        for mode, code in HVAC_MODE_TO_CODE.items():
            assert HVAC_CODE_TO_MODE[code] == mode

    def test_known_codes(self):
        assert HVAC_MODE_TO_CODE["heat"] == "H"
        assert HVAC_CODE_TO_MODE["A"] == "auto"


class TestFanMaps:
    def test_round_trip(self):
        for state, code in FAN_STATE_TO_CODE.items():
            assert FAN_CODE_TO_STATE[code] == state

    def test_values(self):
        assert FAN_STATE_TO_CODE["on"] == "R"
        assert FAN_STATE_TO_CODE["off"] == "S"


class TestSwitchMap:
    def test_values(self):
        assert SWITCH_CODE_TO_STATE["O"] == "ON"
        assert SWITCH_CODE_TO_STATE["C"] == "OFF"


class TestSecurityMap:
    def test_values(self):
        assert SECURITY_CODE_TO_STATE["A"] == "armed_away"
        assert SECURITY_CODE_TO_STATE["D"] == "disarmed"
