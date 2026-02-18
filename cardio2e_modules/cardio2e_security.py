"""Security/alarm entity logic for cardio2e."""

import logging
import re

from .cardio2e_constants import SECURITY_CODE_TO_STATE
from .cardio2e_serial import send_command

_LOGGER = logging.getLogger(__name__)


def handle_set_command(serial_conn, topic, payload, config):
    """Handle an MQTT set command for the alarm."""
    try:
        security_id = int(topic.split("/")[-1])
    except ValueError:
        _LOGGER.error("Security ID invalid on topic: %s", topic)
        return

    if payload == "ARMED_AWAY":
        command = f"A {config.alarm_code}"
    elif payload == "DISARMED":
        command = f"D {config.alarm_code}"
    else:
        _LOGGER.error("Invalid Payload for security command: %s", payload)
        return

    send_command(serial_conn, "S", security_id, command)


def process_update(mqtt_client, message_parts):
    """Process an @I S update from the serial listener."""
    security_id = int(message_parts[2])
    security_state = message_parts[3]

    security_state_value = SECURITY_CODE_TO_STATE.get(security_state, "unknown")

    state_topic = f"cardio2e/alarm/state/{security_id}"
    mqtt_client.publish(state_topic, security_state_value, retain=True)
    _LOGGER.info("Security %d state, updated to: %s - %s", security_id, security_state, security_state_value)


def process_login(mqtt_client, message):
    """Process @I S messages from the login response."""
    match = re.match(r"@I S 1 ([AD])", message)
    if match:
        security_state = match.group(1)
        security_state_topic = "cardio2e/alarm/state/1"
        security_state_value = SECURITY_CODE_TO_STATE.get(security_state, "unknown")
        mqtt_client.publish(security_state_topic, security_state_value, retain=True)
        _LOGGER.info("Security state published to MQTT: %s - %s", security_state_value, security_state)
