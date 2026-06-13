"""Tests for RS-232 command construction and helpers."""

import logging

from cardio2e_modules import cardio2e_serial
from cardio2e_modules.cardio2e_serial import query_name, query_state, send_command, send_date

from _fakes import FakeSerial


class TestSendCommandSimple:
    def test_light_with_state(self, serial_conn):
        assert send_command(serial_conn, "L", 5, 100) is True
        assert serial_conn.last_written_str() == "@S L 5 100\r"

    def test_switch(self, serial_conn):
        send_command(serial_conn, "R", 3, "O")
        assert serial_conn.last_written_str() == "@S R 3 O\r"

    def test_cover_position(self, serial_conn):
        send_command(serial_conn, "C", 2, 0)
        assert serial_conn.last_written_str() == "@S C 2 0\r"

    def test_no_state(self, serial_conn):
        send_command(serial_conn, "M", 4)
        assert serial_conn.last_written_str() == "@S M 4\r"


class TestSendCommandHvac:
    def test_hvac_full(self, serial_conn):
        send_command(
            serial_conn, "H", 2,
            heating_setpoint=18, cooling_setpoint=20, fan_state="off", mode="heat",
        )
        assert serial_conn.last_written_str() == "@S H 2 18 20 S H\r"

    def test_hvac_missing_params_returns_false(self, serial_conn):
        assert send_command(serial_conn, "H", 2) is False
        assert serial_conn.written == []


class TestSendCommandAlarmRedaction:
    """The alarm code must never appear in logs (v2.0.8)."""

    def test_code_redacted_in_log_but_sent_on_wire(self, serial_conn, caplog):
        with caplog.at_level(logging.INFO, logger="cardio2e_modules.cardio2e_serial"):
            send_command(serial_conn, "S", 1, "A 1234")

        # Sent to the controller with the real code
        assert serial_conn.last_written_str() == "@S S 1 A 1234\r"
        # But the log redacts it
        assert "A ****" in caplog.text
        assert "1234" not in caplog.text


class TestSendDate:
    def test_builds_date_command(self, serial_conn):
        send_date(serial_conn, "20260101000000")
        assert serial_conn.last_written_str() == "@S D 20260101000000\r"


class TestSendCommandError:
    def test_write_failure_returns_false(self):
        class BrokenSerial(FakeSerial):
            def write(self, data):
                raise OSError("boom")

        assert send_command(BrokenSerial(), "L", 1, 100) is False


class TestQueryName:
    def test_parses_name_from_response(self):
        conn = FakeSerial(to_read=b"@I N L Kitchen\r")
        assert query_name(conn, 5, "L") == "Kitchen"

    def test_returns_none_when_no_response(self):
        conn = FakeSerial(to_read=b"")
        assert query_name(conn, 5, "L", max_retries=1, timeout=0.05) is None


class TestQueryState:
    def test_parses_state_parts(self):
        conn = FakeSerial(to_read=b"@I L 5 100\r")
        parts = query_state(conn, 5, "L")
        assert parts == ["@I", "L", "5", "100"]

    def test_returns_none_when_no_response(self):
        conn = FakeSerial(to_read=b"")
        assert query_state(conn, 5, "L", timeout=0.05, max_retries=1) is None
