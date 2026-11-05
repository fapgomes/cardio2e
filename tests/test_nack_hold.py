"""Tests for duplicate HVAC command suppression and held NACK reports.

Captured 2026-11-05: Home Assistant's climate.turn_off produced two identical
``@S H 1 30.0 32.0 S O`` frames, the bridge re-sent one of them when the ack
was slow (busy line, zone burst), one copy was garbled (``@. .``) and NACKed
(``@N H 1 3``), and the controller then reported ``@I H 1 30.00 32.00 S O``:
the zone was off as requested, yet the error sensor showed the NACK."""

import json
import time

import pytest

from cardio2e_modules import cardio2e_hvac, cardio2e_lights, cardio2e_listener
from cardio2e_modules.cardio2e_config import AppConfig

ERROR_TOPIC = "cardio2e/errors/state"


def _init_hvac(app_state, hvac_id=1):
    app_state.hvac_states = {
        hvac_id: {"heating_setpoint": 30.0, "cooling_setpoint": 32.0, "fan": "off", "mode": "heat"}
    }


def _dispatch(serial_conn, mqtt, app_state, msg):
    cardio2e_listener._dispatch_message(serial_conn, mqtt, AppConfig(), app_state, msg, msg.split())


def _errors(mqtt):
    return [json.loads(p)["error"] for t, p, _ in mqtt.messages if t == ERROR_TOPIC]


def _run_nack_reports(mqtt, app_state):
    late = time.monotonic() + cardio2e_listener.COMMAND_ACK_TIMEOUT + cardio2e_listener.ACK_FOLLOWUP_DELAY + 0.1
    cardio2e_listener._run_due_nack_reports(mqtt, app_state, now=late)


class TestHvacDuplicateCommand:
    def test_identical_command_right_after_is_not_resent(self, mqtt, serial_conn, app_state):
        _init_hvac(app_state)
        cardio2e_hvac.handle_set_command(serial_conn, mqtt, "cardio2e/hvac/1/set/mode", "off", app_state)
        cardio2e_hvac.handle_set_command(serial_conn, mqtt, "cardio2e/hvac/1/set/fan", "off", app_state)
        assert serial_conn.written_str() == ["@S H 1 30.0 32.0 S O\r"]

    def test_different_command_is_sent(self, mqtt, serial_conn, app_state):
        _init_hvac(app_state)
        cardio2e_hvac.handle_set_command(serial_conn, mqtt, "cardio2e/hvac/1/set/mode", "off", app_state)
        cardio2e_hvac.handle_set_command(serial_conn, mqtt, "cardio2e/hvac/1/set/cooling_setpoint", "25", app_state)
        assert len(serial_conn.written) == 2

    def test_identical_command_after_window_is_sent(self, mqtt, serial_conn, app_state, monkeypatch):
        monkeypatch.setattr(cardio2e_hvac, "DUPLICATE_COMMAND_WINDOW", 0.0)
        _init_hvac(app_state)
        cardio2e_hvac.handle_set_command(serial_conn, mqtt, "cardio2e/hvac/1/set/mode", "off", app_state)
        cardio2e_hvac.handle_set_command(serial_conn, mqtt, "cardio2e/hvac/1/set/mode", "off", app_state)
        assert len(serial_conn.written) == 2

    def test_identical_command_after_other_state_reported_is_sent(self, mqtt, serial_conn, app_state):
        # The zone was switched back on at the panel in between: re-send.
        _init_hvac(app_state)
        cardio2e_hvac.handle_set_command(serial_conn, mqtt, "cardio2e/hvac/1/set/mode", "off", app_state)
        _dispatch(serial_conn, mqtt, app_state, "@I H 1 30.00 32.00 S C")
        cardio2e_hvac.handle_set_command(serial_conn, mqtt, "cardio2e/hvac/1/set/mode", "off", app_state)
        assert len(serial_conn.written) == 2

    def test_identical_command_after_confirmation_is_skipped(self, mqtt, serial_conn, app_state):
        _init_hvac(app_state)
        cardio2e_hvac.handle_set_command(serial_conn, mqtt, "cardio2e/hvac/1/set/mode", "off", app_state)
        _dispatch(serial_conn, mqtt, app_state, "@I H 1 30.00 32.00 S O")
        cardio2e_hvac.handle_set_command(serial_conn, mqtt, "cardio2e/hvac/1/set/fan", "off", app_state)
        assert len(serial_conn.written) == 1


class TestHeldNackReport:
    def test_nack_resolved_by_state_update_is_not_reported(self, mqtt, serial_conn, app_state):
        _init_hvac(app_state)
        cardio2e_hvac.handle_set_command(serial_conn, mqtt, "cardio2e/hvac/1/set/mode", "off", app_state)
        _dispatch(serial_conn, mqtt, app_state, "@N H 1 3")
        _dispatch(serial_conn, mqtt, app_state, "@I H 1 30.00 32.00 S O")
        _run_nack_reports(mqtt, app_state)
        assert _errors(mqtt) == []
        # still counted in the diagnostics
        assert app_state.get_diagnostics()["errors_count"] == 1

    def test_nack_resolved_by_ack_is_not_reported(self, mqtt, serial_conn, app_state):
        cardio2e_lights.handle_set_command(serial_conn, "cardio2e/light/set/18", "ON", app_state)
        _dispatch(serial_conn, mqtt, app_state, "@N L 18 3")
        _dispatch(serial_conn, mqtt, app_state, "@A L 18")
        _run_nack_reports(mqtt, app_state)
        assert _errors(mqtt) == []

    def test_unresolved_nack_is_reported_after_the_delay(self, mqtt, serial_conn, app_state):
        _init_hvac(app_state)
        cardio2e_hvac.handle_set_command(serial_conn, mqtt, "cardio2e/hvac/1/set/mode", "off", app_state)
        _dispatch(serial_conn, mqtt, app_state, "@N H 1 3")
        assert _errors(mqtt) == []
        cardio2e_listener._run_due_nack_reports(mqtt, app_state, now=time.monotonic())
        assert _errors(mqtt) == []  # not due yet
        _run_nack_reports(mqtt, app_state)
        assert _errors(mqtt) == ["One or more parameters are not valid: @N H 1 3"]

    def test_nack_with_other_state_reported_is_reported(self, mqtt, serial_conn, app_state):
        _init_hvac(app_state)
        cardio2e_hvac.handle_set_command(serial_conn, mqtt, "cardio2e/hvac/1/set/mode", "off", app_state)
        _dispatch(serial_conn, mqtt, app_state, "@N H 1 3")
        _dispatch(serial_conn, mqtt, app_state, "@I H 1 30.00 32.00 S H")
        _run_nack_reports(mqtt, app_state)
        assert len(_errors(mqtt)) == 1

    @pytest.mark.parametrize("msg", ["@N C 381 2", "@N L 1070 2", "@N 1 1", "@N Z 5"])
    def test_nack_unrelated_to_our_commands_is_reported_at_once(self, mqtt, serial_conn, app_state, msg):
        _dispatch(serial_conn, mqtt, app_state, msg)
        assert len(_errors(mqtt)) == 1
