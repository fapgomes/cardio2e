"""Cover entity logic for cardio2e."""

import logging
import threading

from .cardio2e_serial import send_command

_LOGGER = logging.getLogger(__name__)


def initialize_entity_cover(serial_conn, mqtt_client, get_name_fn, get_entity_state_fn, num_entities, fetch_names, skip_init_state):
    """
    Initialize all entities of type cover and publish them to MQTT.
    """
    _LOGGER.info("Initializing entity state from type cover...")

    if fetch_names:
        for entity_id in range(1, num_entities + 1):
            get_name_fn(serial_conn, entity_id, "C", mqtt_client)
    else:
        _LOGGER.info("The flag for fetching cover names is deactivated; skipping name fetch.")

    if skip_init_state:
        _LOGGER.info("The flag for fetching cover state is deactivated; skipping state fetch.")
    else:
        for entity_id in range(1, num_entities + 1):
            get_entity_state_fn(serial_conn, mqtt_client, entity_id, "C")

    _LOGGER.info("States of all entities of type cover have been initialized.")


def handle_set_position(serial_conn, topic, payload):
    """Handle an MQTT set position command for a cover."""
    try:
        cover_id = int(topic.split("/")[-1])
    except ValueError:
        _LOGGER.error("Topic invalid Cover ID: %s", topic)
        return

    try:
        position = int(payload)
        if position < 0 or position > 100:
            raise ValueError("The position must be between 0 and 100")
    except ValueError:
        _LOGGER.error("Invalid payload for shutter position command: %s", payload)
        return

    send_command(serial_conn, "C", cover_id, position)


def handle_command(serial_conn, mqtt_client, topic, payload, get_entity_state_fn):
    """Handle an MQTT command (OPEN/CLOSE/STOP) for a cover."""
    try:
        cover_id = int(topic.split("/")[-1])
    except ValueError:
        _LOGGER.error("Topic invalid Cover ID: %s", topic)
        return

    command = payload.upper()
    if command == "OPEN":
        send_command(serial_conn, "C", cover_id, 100)
    elif command == "CLOSE":
        send_command(serial_conn, "C", cover_id, 0)
    elif command == "STOP":
        # Run in a separate thread to avoid blocking the MQTT callback
        t = threading.Thread(
            target=_stop_cover,
            args=(serial_conn, mqtt_client, cover_id, get_entity_state_fn),
            daemon=True,
        )
        t.start()
    else:
        _LOGGER.error("Invalid command received: %s", command)


def _stop_cover(serial_conn, mqtt_client, cover_id, get_entity_state_fn):
    """Stop a cover by querying its actual position and re-sending it."""
    try:
        # Query actual position first (serial lock prevents listener contention)
        position = get_entity_state_fn(serial_conn, mqtt_client, cover_id, "C")
        if position is not None:
            send_command(serial_conn, "C", cover_id, position)
            _LOGGER.info("Cover %d stopped at position: %s", cover_id, position)
        else:
            # Fallback: send dummy position to at least trigger a stop
            send_command(serial_conn, "C", cover_id, 50)
            _LOGGER.warning("Cover %d: could not query position, sent dummy 50 to stop.", cover_id)
    except Exception as e:
        _LOGGER.error("Error stopping cover %d: %s", cover_id, e)


def process_update(mqtt_client, message_parts):
    """Process an @I C update from the serial listener."""
    cover_id = int(message_parts[2])
    cover_state = message_parts[3]

    state_topic = f"cardio2e/cover/state/{cover_id}"
    mqtt_client.publish(state_topic, cover_state, retain=False)
    _LOGGER.info("Cover %d state, updated to: %s", cover_id, cover_state)
