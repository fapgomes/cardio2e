"""Tests for zone/bypass MQTT and serial handlers."""

from cardio2e_modules import cardio2e_zones
from cardio2e_modules.cardio2e_config import AppConfig


class TestProcessLoginZones:
    def test_publishes_each_zone_state(self, mqtt, serial_conn):
        cfg = AppConfig(fetch_zone_names=False, zones_normal_as_off=[])
        cardio2e_zones.process_login_zones(mqtt, "@I Z 1 ONCE", serial_conn, cfg, lambda *a: None)
        assert mqtt.payload_for("cardio2e/zone/state/1") == "ON"   # O
        assert mqtt.payload_for("cardio2e/zone/state/2") == "ON"   # N
        assert mqtt.payload_for("cardio2e/zone/state/3") == "OFF"  # C
        assert mqtt.payload_for("cardio2e/zone/state/4") == "ERROR"  # E


class TestProcessZoneUpdate:
    def test_publishes_states(self, mqtt, app_state):
        cfg = AppConfig(zones_normal_as_off=[])
        cardio2e_zones.process_zone_update(mqtt, ["@I", "Z", "1", "ON"], cfg, app_state)
        assert mqtt.payload_for("cardio2e/zone/state/1") == "ON"
        assert mqtt.payload_for("cardio2e/zone/state/2") == "ON"


class TestProcessLoginBypass:
    def test_sets_state_and_publishes(self, mqtt, app_state):
        cardio2e_zones.process_login_bypass(mqtt, "@I B 1 NYNN", app_state)
        assert app_state.bypass_states == "NYNN"
        assert mqtt.payload_for("cardio2e/zone/bypass/state/1") == "OFF"
        assert mqtt.payload_for("cardio2e/zone/bypass/state/2") == "ON"


class TestProcessBypassUpdate:
    def test_publishes(self, mqtt, app_state):
        cardio2e_zones.process_bypass_update(mqtt, ["@I", "B", "1", "NY"], app_state)
        assert mqtt.payload_for("cardio2e/zone/bypass/state/1") == "OFF"
        assert mqtt.payload_for("cardio2e/zone/bypass/state/2") == "ON"


class TestHandleBypassCommand:
    def test_sets_bit_and_sends(self, serial_conn, app_state):
        app_state.bypass_states = "N" * 16
        cardio2e_zones.handle_bypass_command(
            serial_conn, "cardio2e/zone/bypass/set/2", "ON", app_state
        )
        written = serial_conn.last_written_str()
        assert written.startswith("@S B 1 ")
        states = written[len("@S B 1 "):].rstrip("\r")
        assert states[1] == "Y"
        assert app_state.bypass_states[1] == "Y"

    def test_defaults_when_state_missing(self, serial_conn, app_state):
        app_state.bypass_states = ""
        cardio2e_zones.handle_bypass_command(
            serial_conn, "cardio2e/zone/bypass/set/1", "ON", app_state
        )
        written = serial_conn.last_written_str()
        states = written[len("@S B 1 "):].rstrip("\r")
        assert len(states) == 16
        assert states[0] == "Y"
