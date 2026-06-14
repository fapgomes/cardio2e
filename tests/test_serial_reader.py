"""Tests for the serial reader thread, pending-request registry and queries."""

import threading
import time

import cardio2e_modules.cardio2e_serial as cs

from _fakes import FakeSerial


class TestPendingRegistry:
    def test_deliver_matches_predicate(self):
        q = cs._register(lambda p: p[1] == "L" and p[2] == "5")
        try:
            consumed = cs._deliver_to_pending(["@I", "L", "5", "100"], "@I L 5 100")
            assert consumed is True
            assert q.get_nowait() == "@I L 5 100"
        finally:
            cs._unregister(q)

    def test_deliver_no_match_returns_false(self):
        q = cs._register(lambda p: p[1] == "L" and p[2] == "5")
        try:
            consumed = cs._deliver_to_pending(["@I", "L", "3", "100"], "@I L 3 100")
            assert consumed is False
            assert q.empty()
        finally:
            cs._unregister(q)

    def test_unregister_removes_entry(self):
        q = cs._register(lambda p: True)
        cs._unregister(q)
        assert cs._deliver_to_pending(["@I", "L", "1", "0"], "@I L 1 0") is False


class TestCoordinatedQuery:
    def test_query_state_coordinated_returns_match(self):
        conn = FakeSerial()
        cs._reader_active.set()
        try:
            def deliver():
                time.sleep(0.05)
                cs._deliver_to_pending(["@I", "L", "5", "100"], "@I L 5 100")
            threading.Thread(target=deliver, daemon=True).start()
            parts = cs.query_state(conn, 5, "L", timeout=1.0, max_retries=1)
            assert parts == ["@I", "L", "5", "100"]
        finally:
            cs._reader_active.clear()

    def test_query_state_coordinated_ignores_wrong_id(self):
        conn = FakeSerial()
        cs._reader_active.set()
        try:
            def deliver():
                time.sleep(0.05)
                assert cs._deliver_to_pending(["@I", "L", "3", "100"], "@I L 3 100") is False
            threading.Thread(target=deliver, daemon=True).start()
            parts = cs.query_state(conn, 5, "L", timeout=0.3, max_retries=1)
            assert parts is None
        finally:
            cs._reader_active.clear()
