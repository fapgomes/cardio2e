"""Error handling and reporting for cardio2e."""

import json
import logging
from datetime import datetime

from .cardio2e_constants import DEVICE_INFO, ERROR_CODES, AVAILABILITY_TOPIC, PAYLOAD_AVAILABLE, PAYLOAD_NOT_AVAILABLE

_LOGGER = logging.getLogger(__name__)


def format_error_message(message_parts):
    """
    Format a NACK error message using the ERROR_CODES dict.
    :param message_parts: List of message parts from the @N response.
    :return: Human-readable error string.
    """
    raw_msg = f"@N {message_parts[1]} {message_parts[2]} {message_parts[3]}"
    error_code = message_parts[3]
    description = ERROR_CODES.get(error_code, f"Unknown error message ({error_code})")
    return f"{description}: {raw_msg}"


def report_error_state(mqtt_client, error):
    """Publish error state to the MQTT error topic."""
    state_topic = "cardio2e/errors/state"
    error_state_payload = {
        "error": error,
        "timestamp": datetime.utcnow().isoformat(),
    }
    mqtt_client.publish(state_topic, json.dumps(error_state_payload), retain=True)
    _LOGGER.info("Published state for error: %s", error)


def initialize_error_payload(mqtt_client):
    """Publish autodiscovery config for the error sensor in Home Assistant."""
    sensor_config_topic = "homeassistant/sensor/cardio2e_errors/config"
    state_topic = "cardio2e/errors/state"

    sensor_config_payload = {
        "name": "Cardio2e Errors",
        "unique_id": "cardio2e_error",
        "state_topic": state_topic,
        "icon": "mdi:alert-circle-outline",
        "qos": 1,
        "retain": True,
        "value_template": "{{ value_json.error }}",
        "availability_topic": AVAILABILITY_TOPIC,
        "payload_available": PAYLOAD_AVAILABLE,
        "payload_not_available": PAYLOAD_NOT_AVAILABLE,
        "device": DEVICE_INFO["errors"],
    }

    mqtt_client.publish(sensor_config_topic, json.dumps(sensor_config_payload), retain=True)
    _LOGGER.info("Published autodiscovery config for error sensor.")
