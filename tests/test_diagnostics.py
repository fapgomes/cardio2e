"""Tests for the richer diagnostics: serial accessors and heartbeat payload."""

import json

import cardio2e_modules.cardio2e_serial as cs
from cardio2e_modules import cardio2e_listener


class TestSerialAccessors:
    def test_reader_active_reflects_event(self):
        assert cs.reader_active() is False
        cs._reader_active.set()
        try:
            assert cs.reader_active() is True
        finally:
            cs._reader_active.clear()

    def test_pending_count(self):
        assert cs.pending_count() == 0
        q = cs._register(lambda p: True)
        try:
            assert cs.pending_count() == 1
        finally:
            cs._unregister(q)
        assert cs.pending_count() == 0


class TestHeartbeatPayload:
    def test_includes_reader_and_diagnostic_fields(self, mqtt, app_state):
        cardio2e_listener._publish_heartbeat(mqtt, app_state)
        raw = mqtt.payload_for("cardio2e/diagnostics/state")
        assert raw is not None
        diag = json.loads(raw)
        for key in (
            "uptime_seconds",
            "messages_processed",
            "errors_count",
            "reconnects",
            "last_command",
            "last_error",
            "seconds_since_last_message",
            "reader_active",
            "pending_queries",
            "timestamp",
        ):
            assert key in diag
