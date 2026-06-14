"""Tests for scenario (macro) initialization and commands."""

from cardio2e_modules import cardio2e_scenarios
from cardio2e_modules.cardio2e_config import AppConfig


class TestInitializeScenarios:
    def test_missing_scenario_falls_back_with_short_timeout(self, monkeypatch, mqtt, serial_conn, app_state):
        calls = []

        def fake_query_name(conn, sid, etype, max_retries=3, timeout=10):
            calls.append((sid, max_retries, timeout))
            return None  # simulate an undefined scenario slot (no response)

        monkeypatch.setattr(cardio2e_scenarios, "query_name", fake_query_name)
        cfg = AppConfig(nscenarios=2, fetch_scenario_names=True)

        cardio2e_scenarios.initialize_scenarios(serial_conn, mqtt, cfg, app_state)

        # Falls back to the generic name and still publishes autodiscovery
        assert app_state.get_entity_label("scene", "M", 1) == "scene Scenario 1 (id: 1)"
        assert mqtt.payload_for("homeassistant/scene/cardio2e_scene_1/config") is not None
        # Uses a short timeout/retry so a gap is cheap (not the 30s default)
        for _sid, retries, timeout in calls:
            assert retries <= 2
            assert timeout <= 2

    def test_uses_fetched_name_when_available(self, monkeypatch, mqtt, serial_conn, app_state):
        monkeypatch.setattr(cardio2e_scenarios, "query_name", lambda *a, **k: "DESCE ESTOR")
        cfg = AppConfig(nscenarios=1, fetch_scenario_names=True)
        cardio2e_scenarios.initialize_scenarios(serial_conn, mqtt, cfg, app_state)
        assert app_state.get_entity_label("scene", "M", 1) == "scene DESCE ESTOR (id: 1)"

    def test_disabled_when_zero(self, mqtt, serial_conn, app_state):
        cfg = AppConfig(nscenarios=0)
        cardio2e_scenarios.initialize_scenarios(serial_conn, mqtt, cfg, app_state)
        assert mqtt.messages == []


class TestHandleSetCommand:
    def test_on_fires_scenario(self, serial_conn):
        cfg = AppConfig()
        cardio2e_scenarios.handle_set_command(serial_conn, "cardio2e/scene/set/3", "ON", cfg)
        assert serial_conn.last_written_str() == "@S M 3\r"

    def test_numeric_payload_sends_code(self, serial_conn):
        cfg = AppConfig()
        cardio2e_scenarios.handle_set_command(serial_conn, "cardio2e/scene/set/3", "12345", cfg)
        assert serial_conn.last_written_str() == "@S M 3 12345\r"
