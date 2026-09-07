"""Tests for the single retry of @S commands that get no @A ack.

The Cardio2e mirrors physical key presses onto its RS-232 output as ``@S``
frames and sometimes splices them into the middle of another frame. When
that burst coincides with a command of ours, the command is lost: no ``@A``
ever arrives. Lights (L) and relays (R) are re-sent once; covers, scenes,
security and the date sync are never retried (not idempotent)."""

import threading
import time

import pytest

from cardio2e_modules import cardio2e_listener, cardio2e_lights, cardio2e_switches
from cardio2e_modules.cardio2e_config import AppConfig


def _wait_for(predicate, timeout=3.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class TestCommandRetry:
    @pytest.fixture(autouse=True)
    def _fast_timeout(self, monkeypatch):
        monkeypatch.setattr(cardio2e_listener, "COMMAND_ACK_TIMEOUT", 0.05, raising=False)

    @staticmethod
    def _ack(serial_conn, mqtt, app_state, etype, eid):
        cardio2e_listener._dispatch_message(
            serial_conn, mqtt, AppConfig(), app_state, f"@A {etype} {eid}", ["@A", etype, str(eid)]
        )

    @staticmethod
    def _run_due(serial_conn, app_state, late=True):
        now = time.monotonic()
        if late:
            now += cardio2e_listener.COMMAND_ACK_TIMEOUT
        cardio2e_listener._run_due_command_retries(serial_conn, app_state, now=now)

    def test_light_command_without_ack_is_resent_once(self, serial_conn, app_state):
        cardio2e_lights.handle_set_command(serial_conn, "cardio2e/light/set/18", "ON", app_state)
        assert serial_conn.written_str() == ["@S L 18 100\r"]
        self._run_due(serial_conn, app_state)
        assert serial_conn.written_str() == ["@S L 18 100\r", "@S L 18 100\r"]
        # a single retry: nothing more even if the retry is also lost
        self._run_due(serial_conn, app_state)
        self._run_due(serial_conn, app_state)
        assert len(serial_conn.written) == 2

    def test_switch_command_without_ack_is_resent_once(self, serial_conn, app_state):
        cardio2e_switches.handle_set_command(serial_conn, "cardio2e/switch/set/3", "OFF", app_state)
        self._run_due(serial_conn, app_state)
        assert serial_conn.written_str() == ["@S R 3 C\r", "@S R 3 C\r"]

    def test_acked_command_is_not_resent(self, mqtt, serial_conn, app_state):
        cardio2e_lights.handle_set_command(serial_conn, "cardio2e/light/set/18", "ON", app_state)
        self._ack(serial_conn, mqtt, app_state, "L", 18)
        self._run_due(serial_conn, app_state)
        assert serial_conn.written_str() == ["@S L 18 100\r"]

    def test_not_yet_due_does_nothing(self, serial_conn, app_state):
        cardio2e_lights.handle_set_command(serial_conn, "cardio2e/light/set/18", "ON", app_state)
        self._run_due(serial_conn, app_state, late=False)
        assert len(serial_conn.written) == 1

    def test_ack_for_another_entity_does_not_cancel_retry(self, mqtt, serial_conn, app_state):
        cardio2e_lights.handle_set_command(serial_conn, "cardio2e/light/set/18", "ON", app_state)
        self._ack(serial_conn, mqtt, app_state, "L", 19)
        self._ack(serial_conn, mqtt, app_state, "R", 18)
        self._run_due(serial_conn, app_state)
        assert serial_conn.written_str().count("@S L 18 100\r") == 2

    def test_newer_command_for_same_entity_replaces_the_pending_one(self, serial_conn, app_state):
        cardio2e_lights.handle_set_command(serial_conn, "cardio2e/light/set/18", "ON", app_state)
        cardio2e_lights.handle_set_command(serial_conn, "cardio2e/light/set/18", "OFF", app_state)
        self._run_due(serial_conn, app_state)
        assert serial_conn.written_str() == ["@S L 18 100\r", "@S L 18 0\r", "@S L 18 0\r"]

    def test_invalid_payload_schedules_nothing(self, serial_conn, app_state):
        cardio2e_lights.handle_set_command(serial_conn, "cardio2e/light/set/18", "bogus", app_state)
        self._run_due(serial_conn, app_state)
        assert serial_conn.written == []

    def test_handlers_still_work_without_app_state(self, serial_conn):
        cardio2e_lights.handle_set_command(serial_conn, "cardio2e/light/set/5", "ON")
        cardio2e_switches.handle_set_command(serial_conn, "cardio2e/switch/set/3", "ON")
        assert serial_conn.written_str() == ["@S L 5 100\r", "@S R 3 O\r"]

    def test_retry_does_not_spawn_a_thread(self, serial_conn, app_state):
        before = threading.active_count()
        for eid in range(1, 21):
            cardio2e_lights.handle_set_command(serial_conn, f"cardio2e/light/set/{eid}", "ON", app_state)
        assert threading.active_count() == before

    def test_housekeeping_loop_resends_unacked_command(self, mqtt, serial_conn, app_state):
        # End to end: the command is sent, the controller never acks, the
        # housekeeping loop re-sends it once.
        cardio2e_lights.handle_set_command(serial_conn, "cardio2e/light/set/18", "ON", app_state)
        ev = threading.Event()
        t = threading.Thread(
            target=cardio2e_listener.listen_for_updates,
            args=(serial_conn, mqtt, AppConfig(), app_state, ev),
            daemon=True,
        )
        t.start()
        try:
            assert _wait_for(lambda: serial_conn.written_str().count("@S L 18 100\r") == 2, timeout=3)
        finally:
            ev.set()
            t.join(timeout=5)
        assert not t.is_alive()
        assert serial_conn.written_str().count("@S L 18 100\r") == 2
