"""Serial listener loop and message dispatcher for cardio2e."""

import datetime
import json
import logging
import threading
import time

from .cardio2e_serial import send_date, query_state, SerialReader
from . import (
    cardio2e_errors,
    cardio2e_lights,
    cardio2e_switches,
    cardio2e_covers,
    cardio2e_hvac,
    cardio2e_security,
    cardio2e_zones,
)
from .cardio2e_constants import (
    HVAC_CODE_TO_MODE,
    FAN_CODE_TO_STATE,
    SECURITY_CODE_TO_STATE,
    SWITCH_CODE_TO_STATE,
    AVAILABILITY_TOPIC,
    PAYLOAD_AVAILABLE,
    PAYLOAD_NOT_AVAILABLE,
    DEVICE_INFO,
)

HEARTBEAT_INTERVAL = 30  # seconds

_LOGGER = logging.getLogger(__name__)


def _sync_all_entities(serial_conn, mqtt_client, config, app_state):
    """Re-query state of all known entities and republish to MQTT.

    Covers are republished from the cached state instead of querying
    the Cardio2e via RS-232, because the ``@G C`` query causes the
    controller to re-issue the position command to the motor, making
    the cover physically move.
    """
    _LOGGER.info("Starting periodic entity sync...")
    count = 0

    # Z and B: query with ID 1 (returns all zones/bypasses)
    for entity_type in ("Z", "B"):
        _get_entity_state(serial_conn, mqtt_client, 1, entity_type, config, app_state)
        count += 1

    # S: security, always ID 1
    _get_entity_state(serial_conn, mqtt_client, 1, "S", config, app_state)
    count += 1

    # L, R, H, T: iterate known IDs and query via RS-232
    for entity_type in ("L", "R", "H", "T"):
        entity_ids = app_state.get_known_entity_ids(entity_type)
        for entity_id in entity_ids:
            _get_entity_state(serial_conn, mqtt_client, entity_id, entity_type, config, app_state)
            count += 1

    # C (covers): republish from cache only — do NOT query RS-232
    for entity_id in app_state.get_known_entity_ids("C"):
        cached = app_state.get_entity_state("C", entity_id)
        if cached is not None:
            mqtt_client.publish(f"cardio2e/cover/state/{entity_id}", cached, retain=True)
            _LOGGER.debug("Cover %d sync: republished cached state %s", entity_id, cached)
        else:
            _LOGGER.warning("Cover %d sync: no cached state, skipping.", entity_id)
        count += 1

    _LOGGER.info("Periodic entity sync complete: %d entities synced.", count)


def _publish_heartbeat(mqtt_client, app_state):
    """Publish heartbeat and diagnostics to MQTT."""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    mqtt_client.publish("cardio2e/heartbeat", timestamp, retain=False)

    diag = app_state.get_diagnostics()
    diag["timestamp"] = timestamp
    mqtt_client.publish("cardio2e/diagnostics/state", json.dumps(diag), retain=True)
    _LOGGER.debug("Heartbeat published: %s", timestamp)


def _publish_diagnostics_autodiscovery(mqtt_client):
    """Publish autodiscovery config for the diagnostics sensor."""
    config_topic = "homeassistant/sensor/cardio2e_diagnostics/config"
    config_payload = {
        "name": "Cardio2e Diagnostics",
        "unique_id": "cardio2e_diagnostics",
        "state_topic": "cardio2e/diagnostics/state",
        "icon": "mdi:heart-pulse",
        "qos": 1,
        "retain": True,
        "value_template": "{{ value_json.uptime_seconds }}",
        "unit_of_measurement": "s",
        "json_attributes_topic": "cardio2e/diagnostics/state",
        "availability_topic": AVAILABILITY_TOPIC,
        "payload_available": PAYLOAD_AVAILABLE,
        "payload_not_available": PAYLOAD_NOT_AVAILABLE,
        "device": DEVICE_INFO["errors"],
    }
    mqtt_client.publish(config_topic, json.dumps(config_payload), retain=True)
    _LOGGER.info("Published autodiscovery config for diagnostics sensor.")


def listen_for_updates(serial_conn, mqtt_client, config, app_state):
    """Run the housekeeping loop while a SerialReader thread owns the port.

    Returns when the connection is lost (reader stopped), so the caller can
    reconnect.
    """
    last_time_sent = time.monotonic()
    last_heartbeat = time.monotonic()
    last_sync = time.monotonic()

    # Publish diagnostics autodiscovery on start
    _publish_diagnostics_autodiscovery(mqtt_client)

    def on_message(msg, message_parts):
        _LOGGER.info("Processing individual message: %s", msg)
        app_state.increment_messages()
        _dispatch_message(serial_conn, mqtt_client, config, app_state, msg, message_parts)

    reader = SerialReader(serial_conn, on_message)
    reader.start()

    try:
        while serial_conn.is_open and reader.is_alive():
            now = time.monotonic()

            # Send date periodically
            if (now - last_time_sent) >= config.update_date_interval:
                time_command = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
                send_date(serial_conn, time_command)
                cardio2e_errors.report_error_state(mqtt_client, "No errors.")
                _LOGGER.info("Sent time command to cardio2e: %s", time_command)
                last_time_sent = now

            # Publish heartbeat + diagnostics periodically
            if (now - last_heartbeat) >= HEARTBEAT_INTERVAL:
                _publish_heartbeat(mqtt_client, app_state)
                last_heartbeat = now

            # Periodic entity sync (queries are served by the reader, so this
            # no longer blocks message reception)
            if config.sync_interval > 0 and (now - last_sync) >= config.sync_interval:
                _sync_all_entities(serial_conn, mqtt_client, config, app_state)
                last_sync = now

            time.sleep(0.5)
    finally:
        reader.stop()
        reader.join(timeout=2)

    _LOGGER.warning("Serial reader stopped; connection considered lost.")


def _dispatch_message(serial_conn, mqtt_client, config, app_state, msg, message_parts):
    """Dispatch a single parsed message to the appropriate handler."""

    # ACK messages (@A)
    if len(message_parts) == 2 and message_parts[0] == "@A":
        if message_parts[1] == "D":
            _LOGGER.info("Cardio date update successfully.")

    elif len(message_parts) == 3 and message_parts[0] == "@A":
        entity_type = message_parts[1]
        entity_id = int(message_parts[2])

        if entity_type == "L":
            _LOGGER.info("OK for action %s", app_state.get_entity_label("light", "L", entity_id))
        elif entity_type == "R":
            _LOGGER.info("OK for action %s", app_state.get_entity_label("switch", "R", entity_id))
        elif entity_type == "C":
            _LOGGER.info("OK for action %s", app_state.get_entity_label("cover", "C", entity_id))
        elif entity_type == "S":
            _LOGGER.info("OK for action %s", app_state.get_entity_label("security", "S", entity_id))
        elif entity_type == "M":
            _LOGGER.info("OK for action %s", app_state.get_entity_label("scenario", "M", entity_id))
        elif entity_type == "B" and entity_id == 1:
            # Run off the reader thread: _get_entity_state issues a coordinated
            # query that waits on the reader, so calling it inline here would
            # deadlock (the reader would be waiting on itself).
            threading.Thread(
                target=_get_entity_state,
                args=(serial_conn, mqtt_client, 1, "B", config, app_state),
                daemon=True,
            ).start()
            _LOGGER.info("Bypass zones re-publish (async).")

    # NACK messages (@N)
    elif len(message_parts) >= 3 and message_parts[0] == "@N":
        error_msg = cardio2e_errors.format_error_message(message_parts)
        cardio2e_errors.report_error_state(mqtt_client, error_msg)
        app_state.increment_errors()
        _LOGGER.info("\n#######\nNACK from cardio with transaction %s: %s", msg, error_msg)

    # Info/state update messages (@I)
    elif len(message_parts) >= 4 and message_parts[0] == "@I":
        entity_type = message_parts[1]

        if entity_type == "L":
            cardio2e_lights.process_update(mqtt_client, message_parts, config, app_state)
        elif entity_type == "R":
            cardio2e_switches.process_update(mqtt_client, message_parts, app_state)
        elif entity_type == "C":
            cardio2e_covers.process_update(mqtt_client, message_parts, app_state)
        elif entity_type == "H":
            cardio2e_hvac.process_update(mqtt_client, message_parts, app_state)
        elif entity_type == "T":
            cardio2e_hvac.process_temp_update(mqtt_client, message_parts, app_state)
        elif entity_type == "S":
            cardio2e_security.process_update(mqtt_client, message_parts, app_state)
        elif entity_type == "Z":
            cardio2e_zones.process_zone_update(mqtt_client, message_parts, config, app_state)
        elif entity_type == "B":
            cardio2e_zones.process_bypass_update(mqtt_client, message_parts, app_state)
        else:
            _LOGGER.error("Response not processed: %s", message_parts)
    else:
        _LOGGER.error("Response not processed: %s", message_parts)


def _get_entity_state(serial_conn, mqtt_client, entity_id, entity_type, config, app_state):
    """Query entity state and publish to MQTT (used internally by listener)."""
    message_parts = query_state(serial_conn, entity_id, entity_type)
    if message_parts is None:
        return None

    if entity_type == "L" and len(message_parts) >= 4:
        state = int(message_parts[3])
        light_state = "ON" if state > 0 else "OFF"
        mqtt_client.publish(f"cardio2e/light/state/{entity_id}", light_state, retain=True)
        return light_state

    elif entity_type == "R" and len(message_parts) >= 4:
        state = message_parts[3]
        switch_state = SWITCH_CODE_TO_STATE.get(state, "OFF")
        mqtt_client.publish(f"cardio2e/switch/state/{entity_id}", switch_state, retain=True)
        return switch_state

    elif entity_type == "C" and len(message_parts) >= 4:
        state = message_parts[3]
        app_state.set_entity_state("C", entity_id, state)
        mqtt_client.publish(f"cardio2e/cover/state/{entity_id}", state, retain=True)
        return state

    elif entity_type == "T" and len(message_parts) >= 4:
        state = message_parts[3]
        mqtt_client.publish(f"cardio2e/hvac/{entity_id}/state/current_temperature", state, retain=True)
        return state

    elif entity_type == "H" and len(message_parts) >= 7:
        topics = {
            "heating_setpoint": message_parts[3],
            "cooling_setpoint": message_parts[4],
            "fan": FAN_CODE_TO_STATE.get(message_parts[5], "off"),
            "mode": HVAC_CODE_TO_MODE.get(message_parts[6], "Unknown"),
        }
        with app_state.lock:
            hvac_states = app_state.hvac_states
            for topic_suffix, state in topics.items():
                hvac_states = cardio2e_hvac.update_hvac_state(mqtt_client, hvac_states, int(entity_id), topic_suffix, state)
            app_state.hvac_states = hvac_states
        return True

    elif entity_type == "S" and len(message_parts) >= 4:
        state = SECURITY_CODE_TO_STATE.get(message_parts[3], "unknown")
        mqtt_client.publish(f"cardio2e/alarm/state/{entity_id}", state, retain=True)
        return state

    elif entity_type == "Z" and len(message_parts) >= 4:
        zone_states = message_parts[3]
        for zone_id in range(1, len(zone_states) + 1):
            zone_state = cardio2e_zones.interpret_zone_character(zone_states[zone_id - 1], zone_id, config.zones_normal_as_off)
            mqtt_client.publish(f"cardio2e/zone/state/{zone_id}", zone_state, retain=True)
        return zone_states

    elif entity_type == "B" and len(message_parts) >= 4:
        states = message_parts[3]
        for zone_id in range(1, len(states) + 1):
            bypass_state = cardio2e_zones.interpret_bypass_character(states[zone_id - 1])
            mqtt_client.publish(f"cardio2e/zone/bypass/state/{zone_id}", bypass_state, retain=True)
        return states

    return None
