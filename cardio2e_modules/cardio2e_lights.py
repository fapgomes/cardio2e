"""Light entity logic for cardio2e."""

import logging

from .cardio2e_serial import send_command

_LOGGER = logging.getLogger(__name__)


def handle_set_command(serial_conn, topic, payload):
    """Handle an MQTT set command for a light."""
    try:
        light_id = int(topic.split("/")[-1])
    except ValueError:
        _LOGGER.error("Invalid light ID on topic: %s", topic)
        return

    if payload == "ON":
        command = 100
    elif payload == "OFF":
        command = 0
    else:
        try:
            command = int(payload)
            if command < 0 or command > 100:
                raise ValueError("Value must be between 0 and 100")
        except ValueError:
            _LOGGER.error("Invalid payload for light command: %s", payload)
            return

    send_command(serial_conn, "L", light_id, command)


def process_update(mqtt_client, message_parts, config):
    """Process an @I L update from the serial listener."""
    light_id = int(message_parts[2])
    state = int(message_parts[3])

    light_state = "ON" if state > 0 else "OFF"

    state_topic = f"cardio2e/light/state/{light_id}"
    mqtt_client.publish(state_topic, light_state, retain=False)
    _LOGGER.info("Light %d state updated to: %s", light_id, light_state)

    if light_id in config.dimmer_lights:
        brightness_topic = f"cardio2e/light/brightness/{light_id}"
        mqtt_client.publish(brightness_topic, state, retain=False)
        _LOGGER.info("Light %d brightness updated to: %d", light_id, state)


def process_login(mqtt_client, message, serial_conn, config, get_name_fn):
    """Process @I L messages from the login response."""
    import re
    match = re.match(r"@I L (\d+) (\d+)", message)
    if match:
        light_id, light_state = match.groups()
        light_state_topic = f"cardio2e/light/state/{light_id}"
        light_state_value = "ON" if int(light_state) > 0 else "OFF"
        mqtt_client.publish(light_state_topic, light_state_value, retain=True)
        if config.fetch_light_names:
            get_name_fn(serial_conn, int(light_id), "L", mqtt_client)
        else:
            _LOGGER.info("The flag for fetching lights names is deactivated; skipping name fetch.")
        _LOGGER.info("Light %s state published to MQTT: %s", light_id, light_state_value)
