"""Tests for cover initialization and handlers."""

from cardio2e_modules import cardio2e_covers


class TestInitializeEntityCover:
    def _spies(self, names):
        """names: dict id->name or None. Returns (get_name_fn, get_state_fn, state_calls)."""
        state_calls = []

        def get_name_fn(serial_conn, entity_id, etype, mqtt_client):
            return names.get(entity_id)

        def get_state_fn(serial_conn, mqtt_client, entity_id, etype):
            state_calls.append(entity_id)

        return get_name_fn, get_state_fn, state_calls

    def test_skips_state_for_covers_without_a_name(self, serial_conn, mqtt):
        # Covers 1 and 3 exist; 2 is an undefined slot (no name)
        names = {1: "Front", 2: None, 3: "Back"}
        get_name_fn, get_state_fn, state_calls = self._spies(names)
        cardio2e_covers.initialize_entity_cover(
            serial_conn, mqtt, get_name_fn, get_state_fn,
            num_entities=3, fetch_names=True, skip_init_state=False,
        )
        assert state_calls == [1, 3]  # cover 2 skipped entirely

    def test_queries_all_states_when_names_disabled(self, serial_conn, mqtt):
        get_name_fn, get_state_fn, state_calls = self._spies({})
        cardio2e_covers.initialize_entity_cover(
            serial_conn, mqtt, get_name_fn, get_state_fn,
            num_entities=3, fetch_names=False, skip_init_state=False,
        )
        assert state_calls == [1, 2, 3]

    def test_skip_init_state(self, serial_conn, mqtt):
        names = {1: "Front", 2: "Back"}
        get_name_fn, get_state_fn, state_calls = self._spies(names)
        cardio2e_covers.initialize_entity_cover(
            serial_conn, mqtt, get_name_fn, get_state_fn,
            num_entities=2, fetch_names=True, skip_init_state=True,
        )
        assert state_calls == []


class TestHandleSetPosition:
    def test_sends_position(self, serial_conn):
        cardio2e_covers.handle_set_position(serial_conn, "cardio2e/cover/set/2", "75")
        assert serial_conn.last_written_str() == "@S C 2 75\r"

    def test_rejects_out_of_range(self, serial_conn):
        cardio2e_covers.handle_set_position(serial_conn, "cardio2e/cover/set/2", "150")
        assert serial_conn.written == []


class TestHandleCommand:
    def test_open(self, serial_conn, mqtt):
        cardio2e_covers.handle_command(serial_conn, mqtt, "cardio2e/cover/command/2", "OPEN", lambda *a: None)
        assert serial_conn.last_written_str() == "@S C 2 100\r"

    def test_close(self, serial_conn, mqtt):
        cardio2e_covers.handle_command(serial_conn, mqtt, "cardio2e/cover/command/2", "CLOSE", lambda *a: None)
        assert serial_conn.last_written_str() == "@S C 2 0\r"


class TestCoverStateFromLogin:
    """``@G C`` makes the controller drive the motor of a standing cover, so
    a position already known (login dump, or cached before a reconnect) is
    not queried again."""

    def test_login_dump_seeds_the_cover_state(self, mqtt, serial_conn, app_state):
        import cardio2e
        from cardio2e_modules.cardio2e_config import AppConfig
        cardio2e.parse_login_response("@I C 3 40\r", mqtt, serial_conn, AppConfig(), app_state)
        assert app_state.get_entity_state("C", 3) == "40"
        assert app_state.get_known_entity_ids("C") == [3]
        assert mqtt.payload_for("cardio2e/cover/state/3") == "40"

    def test_garbled_login_cover_is_ignored(self, mqtt, serial_conn, app_state):
        import cardio2e
        from cardio2e_modules.cardio2e_config import AppConfig
        for frame in ("@I C 3 4O\r", "@I C 3 140\r", "@I C 3 40 junk\r"):
            cardio2e.parse_login_response(frame, mqtt, serial_conn, AppConfig(), app_state)
        assert app_state.get_entity_state("C", 3) is None
        assert mqtt.messages == []

    def test_known_state_is_not_queried(self, serial_conn, mqtt, app_state):
        state_calls = []
        app_state.set_entity_state("C", 1, "40")
        cardio2e_covers.initialize_entity_cover(
            serial_conn, mqtt, lambda *a: "name", lambda s, m, eid, etype: state_calls.append(eid),
            num_entities=2, fetch_names=True, skip_init_state=False, app_state=app_state,
        )
        assert state_calls == [2]


class TestStopFallback:
    """STOP reads the position and sends it back (any ``@S C`` stops a moving
    cover). When the read fails, the value sent must be harmless if the cover
    has stopped in the meantime: its last known position, not an arbitrary
    one that would move it."""

    def test_failed_read_sends_the_cached_position(self, serial_conn, mqtt, app_state):
        app_state.set_entity_state("C", 2, "80")
        cardio2e_covers._stop_cover(serial_conn, mqtt, 2, lambda *a: None, app_state)
        assert serial_conn.written_str() == ["@S C 2 80\r"]

    def test_failed_read_without_cache_still_stops(self, serial_conn, mqtt, app_state):
        cardio2e_covers._stop_cover(serial_conn, mqtt, 2, lambda *a: None, app_state)
        assert serial_conn.written_str() == ["@S C 2 50\r"]

    def test_successful_read_sends_the_position_read(self, serial_conn, mqtt, app_state):
        app_state.set_entity_state("C", 2, "80")
        cardio2e_covers._stop_cover(serial_conn, mqtt, 2, lambda *a: "35", app_state)
        assert serial_conn.written_str() == ["@S C 2 35\r"]

    def test_mqtt_stop_uses_the_cache(self, serial_conn, mqtt, app_state):
        import time
        app_state.set_entity_state("C", 2, "80")
        cardio2e_covers.handle_command(serial_conn, mqtt, "cardio2e/cover/command/2", "STOP",
                                       lambda *a: None, app_state)
        deadline = time.monotonic() + 2
        while not serial_conn.written and time.monotonic() < deadline:
            time.sleep(0.01)
        assert serial_conn.written_str() == ["@S C 2 80\r"]
