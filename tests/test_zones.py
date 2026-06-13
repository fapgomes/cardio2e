"""Tests for zone/bypass character interpretation."""

from cardio2e_modules.cardio2e_zones import (
    interpret_bypass_character,
    interpret_zone_character,
)


class TestInterpretZoneCharacter:
    def test_open_normal(self):
        assert interpret_zone_character("O", 1, []) == "ON"

    def test_normal_char_normal(self):
        assert interpret_zone_character("N", 1, []) == "ON"

    def test_closed_normal(self):
        assert interpret_zone_character("C", 1, []) == "OFF"

    def test_error(self):
        assert interpret_zone_character("E", 1, []) == "ERROR"

    def test_unknown(self):
        assert interpret_zone_character("X", 1, []) == "UNKNOWN"

    def test_open_inverted(self):
        # Zone 14 is in zones_normal_as_off -> inverted
        assert interpret_zone_character("O", 14, [14]) == "OFF"

    def test_closed_inverted(self):
        assert interpret_zone_character("C", 14, [14]) == "ON"

    def test_inversion_only_affects_listed_zone(self):
        assert interpret_zone_character("O", 1, [14]) == "ON"


class TestInterpretBypassCharacter:
    def test_yes(self):
        assert interpret_bypass_character("Y") == "ON"

    def test_no(self):
        assert interpret_bypass_character("N") == "OFF"

    def test_unknown(self):
        assert interpret_bypass_character("?") == "UNKNOWN"
