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
    staying stale until the next periodic sync. Verifications are queued in
    AppState and run by the housekeeping loop: no thread per ack."""

    @pytest.fixture(autouse=True)
    def _fast_followup(self, monkeypatch):
        monkeypatch.setattr(cardio2e_listener, "ACK_FOLLOWUP_DELAY", 0.05, raising=False)

    @staticmethod
    def _ack(serial_conn, mqtt, app_state, etype, eid, cfg=None):
        cardio2e_listener._dispatch_message(
            serial_conn, mqtt, cfg or AppConfig(), app_state, f"@A {etype} {eid}", ["@A", etype, str(eid)]
        )

    @staticmethod
    def _run_due(serial_conn, mqtt, app_state):
        cardio2e_listener._run_due_ack_verifications(
            serial_conn, mqtt, AppConfig(), app_state,
            now=time.monotonic() + cardio2e_listener.ACK_FOLLOWUP_DELAY,
        )

    def test_light_ack_without_update_requeries_state(self, mqtt, serial_conn, app_state):
        serial_conn.feed(b"@I L 13 100\r")  # response to the re-query
        self._ack(serial_conn, mqtt, app_state, "L", 13)
        assert serial_conn.written == []  # deferred: nothing on the wire yet
        self._run_due(serial_conn, mqtt, app_state)
        assert "@G L 13\r" in serial_conn.written_str()
        assert mqtt.payload_for("cardio2e/light/state/13") == "ON"

    def test_switch_ack_without_update_requeries_state(self, mqtt, serial_conn, app_state):
        serial_conn.feed(b"@I R 8 C\r")
        self._ack(serial_conn, mqtt, app_state, "R", 8)
        self._run_due(serial_conn, mqtt, app_state)
        assert "@G R 8\r" in serial_conn.written_str()
        assert mqtt.payload_for("cardio2e/switch/state/8") == "OFF"

    def test_light_ack_followed_by_update_does_not_requery(self, mqtt, serial_conn, app_state):
        cfg = AppConfig()
        self._ack(serial_conn, mqtt, app_state, "L", 13, cfg)
        cardio2e_listener._dispatch_message(
            serial_conn, mqtt, cfg, app_state, "@I L 13 100", ["@I", "L", "13", "100"]
        )
        self._run_due(serial_conn, mqtt, app_state)
        assert "@G L 13\r" not in serial_conn.written_str()

    def test_not_yet_due_does_nothing(self, mqtt, serial_conn, app_state):
        self._ack(serial_conn, mqtt, app_state, "L", 13)
        cardio2e_listener._run_due_ack_verifications(
            serial_conn, mqtt, AppConfig(), app_state, now=time.monotonic()
        )
        assert serial_conn.written == []
        # still pending: runs once it is due
        serial_conn.feed(b"@I L 13 100\r")
        self._run_due(serial_conn, mqtt, app_state)
        assert "@G L 13\r" in serial_conn.written_str()

    def test_cover_ack_never_requeries(self, mqtt, serial_conn, app_state):
        # @G C makes the controller re-drive the motor, so covers are excluded.
        self._ack(serial_conn, mqtt, app_state, "C", 7)
        self._run_due(serial_conn, mqtt, app_state)
        assert serial_conn.written == []

    def test_ack_does_not_spawn_a_thread(self, mqtt, serial_conn, app_state):
        before = threading.active_count()
        for eid in range(1, 21):  # a scene toggling 20 lights
            self._ack(serial_conn, mqtt, app_state, "L", eid)
        assert threading.active_count() == before

    def test_repeated_ack_for_same_entity_is_checked_once(self, mqtt, serial_conn, app_state):
        self._ack(serial_conn, mqtt, app_state, "L", 13)
        self._ack(serial_conn, mqtt, app_state, "L", 13)
        serial_conn.feed(b"@I L 13 100\r")
        self._run_due(serial_conn, mqtt, app_state)
        assert serial_conn.written_str().count("@G L 13\r") == 1

    def test_housekeeping_loop_runs_due_verifications(self, mqtt, serial_conn, app_state):
        # End to end: the reader dispatches the ack, the loop re-queries.
        serial_conn.feed(b"@A L 13\r")
        ev = threading.Event()
        t = threading.Thread(
            target=cardio2e_listener.listen_for_updates,
            args=(serial_conn, mqtt, AppConfig(), app_state, ev),
            daemon=True,
        )
        t.start()
        try:
            assert _wait_for(lambda: "@G L 13\r" in serial_conn.written_str(), timeout=3)
            serial_conn.feed(b"@I L 13 100\r")  # answer the re-query
            assert _wait_for(lambda: mqtt.payload_for("cardio2e/light/state/13") == "ON", timeout=3)
        finally:
            ev.set()
            t.join(timeout=5)
        assert not t.is_alive()


class TestGetEntityStateLight:
    def test_dimmer_republishes_brightness(self, mqtt, serial_conn, app_state):
        serial_conn.feed(b"@I L 5 40\r")
        cfg = AppConfig(dimmer_lights=[5])
        state = cardio2e_listener._get_entity_state(serial_conn, mqtt, 5, "L", cfg, app_state)
        assert state == "ON"
        assert mqtt.payload_for("cardio2e/light/state/5") == "ON"
        assert mqtt.payload_for("cardio2e/light/brightness/5") == 40

    def test_non_dimmer_publishes_no_brightness(self, mqtt, serial_conn, app_state):
        serial_conn.feed(b"@I L 5 40\r")
        cardio2e_listener._get_entity_state(serial_conn, mqtt, 5, "L", AppConfig(), app_state)
        assert "cardio2e/light/brightness/5" not in mqtt.topics()


class TestSyncAllEntities:
    def test_temperature_synced_for_each_known_hvac(self, mqtt, serial_conn, app_state, monkeypatch):
        # @I T (temperature) has no names of its own: the ids to query are the
        # HVAC (H) ids. Before, the sync iterated "T" ids, which never exist.
        app_state.register_entity("H", 1)
        app_state.register_entity("H", 3)
        queried = []
        monkeypatch.setattr(
            cardio2e_listener, "_get_entity_state",
            lambda s, m, eid, etype, cfg, st: queried.append((etype, eid)),
        )
        cardio2e_listener._sync_all_entities(serial_conn, mqtt, AppConfig(), app_state)
        assert [eid for etype, eid in queried if etype == "T"] == [1, 3]
        assert [eid for etype, eid in queried if etype == "H"] == [1, 3]


class TestGetEntityStateHvac:
    def test_unknown_mode_code_uses_lowercase_fallback(self, mqtt, serial_conn, app_state):
        # Must match process_update's fallback ("unknown"), otherwise the same
        # HVAC publishes two different spellings depending on the code path.
        serial_conn.feed(b"@I H 2 18.0 20.0 S X\r")
        cardio2e_listener._get_entity_state(serial_conn, mqtt, 2, "H", AppConfig(), app_state)
        assert mqtt.payload_for("cardio2e/hvac/2/state/mode") == "unknown"
