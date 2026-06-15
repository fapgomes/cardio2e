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
