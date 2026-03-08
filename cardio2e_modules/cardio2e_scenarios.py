"""Scenario (macro) entity logic for cardio2e."""

import logging

from .cardio2e_serial import send_command, query_name
from .cardio2e_autodiscovery import publish_config as publish_autodiscovery_config

_LOGGER = logging.getLogger(__name__)


def handle_set_command(serial_conn, topic, payload, config):
    """Handle an MQTT set command for a scenario (fire-and-forget)."""
    try:
        scenario_id = int(topic.split("/")[-1])
    except ValueError:
        _LOGGER.error("Scenario ID invalid on topic: %s", topic)
        return

    if payload == "ON":
        send_command(serial_conn, "M", scenario_id)
    elif payload.isdigit():
        # Numeric payload = security code for scenarios with security actions
        send_command(serial_conn, "M", scenario_id, payload)
    else:
        _LOGGER.error("Invalid payload for scenario command: %s", payload)


def initialize_scenarios(serial_conn, mqtt_client, config, app_state):
    """Initialize scenario entities (query names and publish autodiscovery)."""
    if config.nscenarios == 0:
        _LOGGER.debug("Scenarios disabled (nscenarios = 0).")
        return

    _LOGGER.info("Initializing %d scenarios...", config.nscenarios)

    for scenario_id in range(1, config.nscenarios + 1):
        entity_name = "Scenario %d" % scenario_id

        if config.fetch_scenario_names:
            fetched_name = query_name(serial_conn, scenario_id, "M")
            if fetched_name:
                entity_name = fetched_name

        app_state.set_entity_name("M", scenario_id, entity_name)
        publish_autodiscovery_config(mqtt_client, scenario_id, entity_name, "M")
        _LOGGER.info("Scenario %d initialized: %s", scenario_id, entity_name)

    _LOGGER.info("Scenario initialization complete (%d scenarios).", config.nscenarios)
