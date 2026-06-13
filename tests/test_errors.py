"""Tests for NACK error message formatting."""

from cardio2e_modules.cardio2e_errors import format_error_message


class TestFormatErrorMessage:
    def test_known_code_without_object_number(self):
        # @N t c  -> code is last element
        msg = format_error_message(["@N", "S", "16"])
        assert "there are open zones" in msg
        assert "@N S 16" in msg

    def test_known_code_with_object_number(self):
        # @N t o c  -> object number present, code still last
        msg = format_error_message(["@N", "C", "5", "3"])
        assert "parameters are not valid" in msg
        assert "@N C 5 3" in msg

    def test_unknown_code(self):
        msg = format_error_message(["@N", "X", "99"])
        assert "Unknown error message (99)" in msg
        assert "@N X 99" in msg
