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


class TestReaderThreadInternals:
    def test_does_not_shadow_thread_internal_stop(self):
        # threading.Thread has an internal _stop() method (called by join() on
        # Python <= 3.12). The reader must not shadow it with an instance attr,
        # or join() raises "'Event' object is not callable".
        reader = cs.SerialReader(FakeSerial(), on_message=lambda m, p: None)
        assert "_stop" not in vars(reader)

    def test_start_stop_join_is_clean(self):
        # Exercises the join() path that triggered the shadowing bug.
        reader = cs.SerialReader(FakeSerial(), on_message=lambda m, p: None)
        reader.start()
        time.sleep(0.05)
        reader.stop()
        reader.join(timeout=2)  # must not raise
        assert not reader.is_alive()


class TestReaderQueryIntegration:
    def test_reader_thread_serves_a_coordinated_query(self):
        # Response arrives shortly after the query is issued (as on real hardware),
        # so the pending request is registered before the reader sees the line.
        conn = FakeSerial()
        reader = cs.SerialReader(conn, on_message=lambda msg, parts: None)
        reader.start()

        def respond():
            time.sleep(0.1)
            conn.feed(b"@I L 5 100\r")
        threading.Thread(target=respond, daemon=True).start()

        try:
            parts = cs.query_state(conn, 5, "L", timeout=2.0, max_retries=1)
            assert parts == ["@I", "L", "5", "100"]
        finally:
            reader.stop()
            reader.join(timeout=2)

    def test_reader_dispatches_spontaneous_update_to_handler(self):
        conn = FakeSerial(to_read=b"@I L 7 0\r")
        seen = []
        reader = cs.SerialReader(conn, on_message=lambda msg, parts: seen.append(parts))
        reader.start()
        try:
            deadline = time.time() + 2
            while not seen and time.time() < deadline:
                time.sleep(0.01)
            assert seen == [["@I", "L", "7", "0"]]
        finally:
            reader.stop()
            reader.join(timeout=2)
