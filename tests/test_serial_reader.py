"""Tests for the serial reader thread, pending-request registry and queries."""

import cardio2e_modules.cardio2e_serial as cs


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
