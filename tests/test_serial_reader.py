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


class TestSerialReaderProcessing:
    def _make_reader(self, conn):
        dispatched = []
        reader = cs.SerialReader(conn, on_message=lambda msg, parts: dispatched.append((msg, parts)))
        return reader, dispatched

    def test_spontaneous_message_is_dispatched(self):
        conn = FakeSerial()
        reader, dispatched = self._make_reader(conn)
        reader._buffer = "@I L 5 100\r"
        reader._process_buffer()
        assert dispatched == [("@I L 5 100", ["@I", "L", "5", "100"])]

    def test_pending_query_consumes_matching_line_no_dispatch(self):
        conn = FakeSerial()
        reader, dispatched = self._make_reader(conn)
        q = cs._register(lambda p: p[1] == "L" and p[2] == "5")
        try:
            reader._buffer = "@I L 5 100\r"
            reader._process_buffer()
            assert dispatched == []
            assert q.get_nowait() == "@I L 5 100"
        finally:
            cs._unregister(q)

    def test_spontaneous_during_pending_query_is_not_lost(self):
        # nº5 regression: a different entity updating while a query is pending
        conn = FakeSerial()
        reader, dispatched = self._make_reader(conn)
        q = cs._register(lambda p: p[1] == "L" and p[2] == "5")
        try:
            reader._buffer = "@I L 3 100\r@I L 5 100\r"
            reader._process_buffer()
            assert ("@I L 3 100", ["@I", "L", "3", "100"]) in dispatched
            assert q.get_nowait() == "@I L 5 100"
        finally:
            cs._unregister(q)
