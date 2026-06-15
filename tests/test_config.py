"""Tests for config parsing and AppState."""

import pytest

from cardio2e_modules.cardio2e_config import AppState, _parse_list_config, load_config


class TestParseListConfig:
    def test_valid_list(self):
        assert _parse_list_config("[1, 2, 3]", "x") == [1, 2, 3]

    def test_empty_list(self):
        assert _parse_list_config("[]", "x") == []

    def test_invalid_json_returns_empty(self):
        assert _parse_list_config("not json", "x") == []

    def test_non_list_returns_empty(self):
        assert _parse_list_config('{"a": 1}', "x") == []


class TestLoadConfigSample:
    """Regression guard for the inline-comment bug (v2.0.10).

    The sample config has inline comments on most options; loading it must
    not let those comments bleed into the parsed values.
    """

    def test_int_option_not_corrupted_by_inline_comment(self, sample_config_path):
        cfg = load_config(sample_config_path)
        assert cfg.nscenarios == 0
        assert cfg.ncovers == 20

    def test_alarm_code_has_no_trailing_comment(self, sample_config_path):
        cfg = load_config(sample_config_path)
        assert cfg.alarm_code == "12345"

    def test_boolean_flags_parse_true(self, sample_config_path):
        cfg = load_config(sample_config_path)
        assert cfg.fetch_light_names is True
        assert cfg.fetch_switch_names is True

    def test_list_options_parse(self, sample_config_path):
        cfg = load_config(sample_config_path)
        assert cfg.dimmer_lights == [1, 2, 3, 4, 5]
        assert cfg.force_include_lights == [46, 47]
        assert cfg.zones_normal_as_off == [14]

    def test_mqtt_section(self, sample_config_path):
        cfg = load_config(sample_config_path)
        assert cfg.mqtt_address == "192.168.1.100"
        assert cfg.mqtt_port == 1883

    def test_missing_file_raises(self):
        with pytest.raises(RuntimeError):
            load_config("/nonexistent/cardio2e.conf")


class TestAppStateLabels:
    def test_label_without_name(self, app_state):
        assert app_state.get_entity_label("Light", "L", 5) == "Light 5"

    def test_label_with_name(self, app_state):
        app_state.set_entity_name("L", 5, "Kitchen")
        assert app_state.get_entity_label("Light", "L", 5) == "Light Kitchen (id: 5)"

    def test_label_with_unknown_name_falls_back(self, app_state):
        app_state.set_entity_name("L", 5, "Unknown")
        assert app_state.get_entity_label("Light", "L", 5) == "Light 5"


class TestAppStateKnownIds:
    def test_returns_sorted_ids_filtered_by_type(self, app_state):
        app_state.set_entity_name("L", 3, "c")
        app_state.set_entity_name("L", 1, "a")
        app_state.set_entity_name("R", 2, "b")
        assert app_state.get_known_entity_ids("L") == [1, 3]
        assert app_state.get_known_entity_ids("R") == [2]
        assert app_state.get_known_entity_ids("C") == []


class TestAppStateEntityState:
    def test_set_and_get(self, app_state):
        app_state.set_entity_state("C", 4, "75")
        assert app_state.get_entity_state("C", 4) == "75"

    def test_missing_returns_none(self, app_state):
        assert app_state.get_entity_state("C", 99) is None


class TestAppStateDiagnostics:
    def test_counters_and_last_command(self, app_state):
        app_state.record_message()
        app_state.record_message()
        app_state.increment_errors()
        app_state.set_last_command("cardio2e/light/set/1 ON")

        diag = app_state.get_diagnostics()
        assert diag["messages_processed"] == 2
        assert diag["errors_count"] == 1
        assert diag["last_command"] == "cardio2e/light/set/1 ON"
        assert diag["uptime_seconds"] >= 0

    def test_reconnects_and_last_error(self, app_state):
        app_state.increment_reconnects()
        app_state.increment_reconnects()
        app_state.set_last_error("some NACK")
        diag = app_state.get_diagnostics()
        assert diag["reconnects"] == 2
        assert diag["last_error"] == "some NACK"

    def test_seconds_since_last_message_none_until_first(self, app_state):
        assert app_state.get_diagnostics()["seconds_since_last_message"] is None
        app_state.record_message()
        assert app_state.get_diagnostics()["seconds_since_last_message"] >= 0
