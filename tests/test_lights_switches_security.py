"""Tests for light, switch and security handlers."""

from cardio2e_modules import cardio2e_lights, cardio2e_security, cardio2e_switches
from cardio2e_modules.cardio2e_config import AppConfig


def _names_recorder():
    calls = []
    return calls, (lambda *args: calls.append(args))


class TestLights:
    def test_process_login_publishes_state_and_fetches_name(self, mqtt, serial_conn):
        cfg = AppConfig(fetch_light_names=True)
        calls, fn = _names_recorder()
        cardio2e_lights.process_login(mqtt, "@I L 5 100", serial_conn, cfg, fn)
        assert mqtt.payload_for("cardio2e/light/state/5") == "ON"
        assert calls and calls[0][1] == 5

    def test_process_login_off(self, mqtt, serial_conn):
        cfg = AppConfig(fetch_light_names=False)
        _, fn = _names_recorder()
        cardio2e_lights.process_login(mqtt, "@I L 7 0", serial_conn, cfg, fn)
        assert mqtt.payload_for("cardio2e/light/state/7") == "OFF"

    def test_process_update_dimmer_publishes_brightness(self, mqtt, app_state):
        cfg = AppConfig(dimmer_lights=[5])
        cardio2e_lights.process_update(mqtt, ["@I", "L", "5", "80"], cfg, app_state)
        assert mqtt.payload_for("cardio2e/light/state/5") == "ON"
        assert mqtt.payload_for("cardio2e/light/brightness/5") == 80

    def test_process_update_non_dimmer_no_brightness(self, mqtt, app_state):
        cfg = AppConfig(dimmer_lights=[])
        cardio2e_lights.process_update(mqtt, ["@I", "L", "5", "0"], cfg, app_state)
        assert mqtt.payload_for("cardio2e/light/state/5") == "OFF"
        assert mqtt.payload_for("cardio2e/light/brightness/5") is None

    def test_handle_set_on(self, serial_conn):
        cardio2e_lights.handle_set_command(serial_conn, "cardio2e/light/set/5", "ON")
        assert serial_conn.last_written_str() == "@S L 5 100\r"

    def test_handle_set_brightness(self, serial_conn):
        cardio2e_lights.handle_set_command(serial_conn, "cardio2e/light/set/5", "50")
        assert serial_conn.last_written_str() == "@S L 5 50\r"

    def test_handle_set_invalid_payload_no_write(self, serial_conn):
        cardio2e_lights.handle_set_command(serial_conn, "cardio2e/light/set/5", "bogus")
        assert serial_conn.written == []


class TestSwitches:
    def test_process_login(self, mqtt, serial_conn):
        cfg = AppConfig(fetch_switch_names=False)
        _, fn = _names_recorder()
        cardio2e_switches.process_login(mqtt, "@I R 3 O", serial_conn, cfg, fn)
        assert mqtt.payload_for("cardio2e/switch/state/3") == "ON"

    def test_handle_set_on(self, serial_conn):
        cardio2e_switches.handle_set_command(serial_conn, "cardio2e/switch/set/3", "ON")
        assert serial_conn.last_written_str() == "@S R 3 O\r"

    def test_handle_set_off(self, serial_conn):
        cardio2e_switches.handle_set_command(serial_conn, "cardio2e/switch/set/3", "OFF")
        assert serial_conn.last_written_str() == "@S R 3 C\r"


class TestSecurity:
    def test_process_login(self, mqtt):
        cardio2e_security.process_login(mqtt, "@I S 1 A")
        assert mqtt.payload_for("cardio2e/alarm/state/1") == "armed_away"

    def test_handle_set_arm(self, serial_conn):
        cfg = AppConfig(alarm_code="9999")
        cardio2e_security.handle_set_command(serial_conn, "cardio2e/alarm/set/1", "ARMED_AWAY", cfg)
        assert serial_conn.last_written_str() == "@S S 1 A 9999\r"

    def test_handle_set_disarm(self, serial_conn):
        cfg = AppConfig(alarm_code="9999")
        cardio2e_security.handle_set_command(serial_conn, "cardio2e/alarm/set/1", "DISARMED", cfg)
        assert serial_conn.last_written_str() == "@S S 1 D 9999\r"

    def test_handle_set_invalid_no_write(self, serial_conn):
        cfg = AppConfig(alarm_code="9999")
        cardio2e_security.handle_set_command(serial_conn, "cardio2e/alarm/set/1", "ARMED_NIGHT", cfg)
        assert serial_conn.written == []
