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

    if not fetch_names:
        _LOGGER.info("The flag for fetching cover names is deactivated; skipping name fetch.")
    if skip_init_state:
        _LOGGER.info("The flag for fetching cover state is deactivated; skipping state fetch.")

    for entity_id in range(1, num_entities + 1):
        if fetch_names:
            name = get_name_fn(serial_conn, entity_id, "C", mqtt_client)
            if name is None:
                # No name response: this cover slot is not defined. Skip its
                # state query too, so undefined covers cost almost nothing.
                continue
        if not skip_init_state:
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
    """Emulate STOP for a moving cover.

    The Secant protocol has no STOP transaction for curtains (only ``@S C o d``
    to set a level, ``@G C o`` to read it and ``@I C o d`` as reply/broadcast).
    What the controller does have is this property: any ``@S C`` received
    while the cover is moving makes it stop. So:

    1. ``@G C`` reads the position the controller reports mid-travel (and
       publishes it to MQTT).
    2. ``@S C`` with that same position is the command that actually stops the
       motor. The controller then acks (``@A C``) and broadcasts the final
       position (``@I C o d``), which the serial reader dispatches to
       ``process_update`` — that is what leaves MQTT with the real resting
       position.

    If the read fails, any value works as a stop command, hence the dummy 50.

    Note the periodic sync deliberately does NOT use ``@G C``: on a cover that
    is standing still it makes the controller re-issue the position command to
    the motor, so idle covers are republished from the cached state instead.
    That caveat does not apply here, where the cover is already moving.
    """
    try:
        # Query actual position first (serial lock prevents listener contention)
        position = get_entity_state_fn(serial_conn, mqtt_client, cover_id, "C")
        if position is not None:
            send_command(serial_conn, "C", cover_id, position)
            _LOGGER.info("Cover %d stopped at position: %s", cover_id, position)
        else:
            # Fallback: any @S C stops a moving cover, so send a dummy position
            send_command(serial_conn, "C", cover_id, 50)
            _LOGGER.warning("Cover %d: could not query position, sent dummy 50 to stop.", cover_id)
    except Exception as e:
        _LOGGER.error("Error stopping cover %d: %s", cover_id, e)


def process_update(mqtt_client, message_parts, app_state):
    """Process an @I C update from the serial listener."""
    cover_id = int(message_parts[2])
    cover_state = message_parts[3]
    label = app_state.get_entity_label("Cover", "C", cover_id)

    app_state.set_entity_state("C", cover_id, cover_state)

    state_topic = f"cardio2e/cover/state/{cover_id}"
    mqtt_client.publish(state_topic, cover_state, retain=True)
    _LOGGER.info("%s state updated to: %s", label, cover_state)
