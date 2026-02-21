"""Switch entity logic for cardio2e."""

import logging
import re

from .cardio2e_constants import SWITCH_CODE_TO_STATE
from .cardio2e_serial import send_command

_LOGGER = logging.getLogger(__name__)


def handle_set_command(serial_conn, topic, payload):
    """Handle an MQTT set command for a switch."""
    try:
        switch_id = int(topic.split("/")[-1])
    except ValueError:
        _LOGGER.error("Switch ID invalid on topic: %s", topic)
        return

    if payload == "ON":
        command = "O"
    elif payload == "OFF":
        command = "C"
    else:
        _LOGGER.error("Invalid Payload for switch command: %s", payload)
        return

    send_command(serial_conn, "R", switch_id, command)


def process_update(mqtt_client, message_parts, app_state):
    """Process an @I R update from the serial listener."""
    switch_id = int(message_parts[2])
    state = message_parts[3]

    switch_state = SWITCH_CODE_TO_STATE.get(state, "OFF")
    label = app_state.get_entity_label("Switch", "R", switch_id)

    state_topic = f"cardio2e/switch/state/{switch_id}"
    mqtt_client.publish(state_topic, switch_state, retain=True)
    _LOGGER.info("%s state updated to: %s", label, switch_state)


def process_login(mqtt_client, message, serial_conn, config, get_name_fn):
    """Process @I R messages from the login response."""
    match = re.match(r"@I R (\d+) ([OC])", message)
    if match:
        switch_id, switch_state = match.groups()
        switch_state_topic = f"cardio2e/switch/state/{switch_id}"
        switch_state_value = SWITCH_CODE_TO_STATE.get(switch_state, "OFF")
        mqtt_client.publish(switch_state_topic, switch_state_value, retain=True)
        if config.fetch_switch_names:
            get_name_fn(serial_conn, int(switch_id), "R", mqtt_client)
        else:
            _LOGGER.info("The flag for fetching switch names is deactivated; skipping name fetch.")
        _LOGGER.info("Switch %s state published to MQTT: %s", switch_id, switch_state_value)
