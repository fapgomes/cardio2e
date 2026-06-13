"""Tests for HVAC handlers."""

from cardio2e_modules import cardio2e_hvac
from cardio2e_modules.cardio2e_config import AppConfig


class TestProcessTempUpdate:
    def test_positive_temp(self, mqtt, app_state):
        cardio2e_hvac.process_temp_update(mqtt, "@I T 1 21.5 H", app_state)
        assert mqtt.payload_for("cardio2e/hvac/1/state/current_temperature") == "21.5"

    def test_negative_temp(self, mqtt, app_state):
        # Regression guard: sub-zero readings must not be dropped (v2.0.11)
        cardio2e_hvac.process_temp_update(mqtt, "@I T 1 -1.5 C", app_state)
        assert mqtt.payload_for("cardio2e/hvac/1/state/current_temperature") == "-1.5"

    def test_accepts_list_input(self, mqtt, app_state):
        cardio2e_hvac.process_temp_update(mqtt, ["@I", "T", "2", "19.0", "O"], app_state)
        assert mqtt.payload_for("cardio2e/hvac/2/state/current_temperature") == "19.0"


class TestProcessLogin:
    def test_initializes_and_publishes(self, mqtt, serial_conn, app_state):
        cfg = AppConfig(fetch_names_hvac=False)
        cardio2e_hvac.process_login(mqtt, "@I H 2 18.0 20.0 S H", serial_conn, cfg, app_state, lambda *a: None)
        assert mqtt.payload_for("cardio2e/hvac/2/state/heating_setpoint") == "18.0"
        assert mqtt.payload_for("cardio2e/hvac/2/state/cooling_setpoint") == "20.0"
        assert mqtt.payload_for("cardio2e/hvac/2/state/fan") == "off"
        assert mqtt.payload_for("cardio2e/hvac/2/state/mode") == "heat"
        # Stored value may be str or float depending on the code path
        assert float(app_state.hvac_states[2]["cooling_setpoint"]) == 20.0


class TestHandleSetCommand:
    def _init(self, app_state):
        app_state.hvac_states = {
            2: {
                "heating_setpoint": 18.0,
                "cooling_setpoint": 20.0,
                "fan": "off",
                "mode": "heat",
            }
        }

    def test_cooling_setpoint_derives_heating(self, serial_conn, app_state, mqtt):
        self._init(app_state)
        cardio2e_hvac.handle_set_command(
            serial_conn, mqtt, "cardio2e/hvac/2/set/cooling_setpoint", "22", app_state
        )
        # heating is always cooling - 2 (intentional, see project memory)
        assert serial_conn.last_written_str() == "@S H 2 20.0 22.0 S H\r"

    def test_ignored_when_not_initialized(self, serial_conn, app_state, mqtt):
        cardio2e_hvac.handle_set_command(
            serial_conn, mqtt, "cardio2e/hvac/9/set/cooling_setpoint", "22", app_state
        )
        assert serial_conn.written == []
