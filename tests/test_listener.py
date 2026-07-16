"""Tests for the listener housekeeping loop lifecycle."""

import threading
import time

import pytest

from cardio2e_modules import cardio2e_listener
from cardio2e_modules.cardio2e_config import AppConfig


class TestShutdownEvent:
    def test_listen_returns_when_shutdown_event_set(self, mqtt, serial_conn, app_state):
        ev = threading.Event()
        t = threading.Thread(
            target=cardio2e_listener.listen_for_updates,
            args=(serial_conn, mqtt, AppConfig(), app_state, ev),
            daemon=True,
        )
        t.start()
        time.sleep(0.1)  # let it enter the housekeeping loop
        ev.set()
        t.join(timeout=3)
        assert not t.is_alive()  # returned promptly on shutdown


def _wait_for(condition, timeout=2.0):
    """Poll ``condition`` until true or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class TestAckWithoutUpdateRequery:
    """An @A ack is normally followed by an @I state broadcast. When that @I
    is lost or corrupted on the wire, the state must be re-queried instead of
    staying stale until the next periodic sync."""

    @pytest.fixture(autouse=True)
    def _fast_followup(self, monkeypatch):
        monkeypatch.setattr(cardio2e_listener, "ACK_FOLLOWUP_DELAY", 0.05, raising=False)

    def test_light_ack_without_update_requeries_state(self, mqtt, serial_conn, app_state):
        serial_conn.feed(b"@I L 13 100\r")  # response to the re-query
        cardio2e_listener._dispatch_message(
            serial_conn, mqtt, AppConfig(), app_state, "@A L 13", ["@A", "L", "13"]
        )
        assert _wait_for(lambda: "@G L 13\r" in serial_conn.written_str())
        assert _wait_for(lambda: mqtt.payload_for("cardio2e/light/state/13") == "ON")

    def test_switch_ack_without_update_requeries_state(self, mqtt, serial_conn, app_state):
        serial_conn.feed(b"@I R 8 C\r")
        cardio2e_listener._dispatch_message(
            serial_conn, mqtt, AppConfig(), app_state, "@A R 8", ["@A", "R", "8"]
        )
        assert _wait_for(lambda: "@G R 8\r" in serial_conn.written_str())
        assert _wait_for(lambda: mqtt.payload_for("cardio2e/switch/state/8") == "OFF")

    def test_light_ack_followed_by_update_does_not_requery(self, mqtt, serial_conn, app_state):
        cfg = AppConfig()
        cardio2e_listener._dispatch_message(
            serial_conn, mqtt, cfg, app_state, "@A L 13", ["@A", "L", "13"]
        )
        cardio2e_listener._dispatch_message(
            serial_conn, mqtt, cfg, app_state, "@I L 13 100", ["@I", "L", "13", "100"]
        )
        time.sleep(0.2)  # let the verification window elapse
        assert "@G L 13\r" not in serial_conn.written_str()

    def test_cover_ack_never_requeries(self, mqtt, serial_conn, app_state):
        # @G C makes the controller re-drive the motor, so covers are excluded.
        cardio2e_listener._dispatch_message(
            serial_conn, mqtt, AppConfig(), app_state, "@A C 7", ["@A", "C", "7"]
        )
        time.sleep(0.2)
        assert serial_conn.written == []
