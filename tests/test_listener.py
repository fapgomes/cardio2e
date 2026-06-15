"""Tests for the listener housekeeping loop lifecycle."""

import threading
import time

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
