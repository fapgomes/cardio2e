"""Serial listener loop and message dispatcher for cardio2e."""

import datetime
import json
import logging
import re
import time

from .cardio2e_serial import send_date, query_state, _serial_lock
from . import (
    cardio2e_errors,
    cardio2e_lights,
    cardio2e_switches,
    cardio2e_covers,
    cardio2e_hvac,
    cardio2e_security,
    cardio2e_zones,
    cardio2e_autodiscovery,
)
from .cardio2e_constants import (
    HVAC_CODE_TO_MODE,
    FAN_CODE_TO_STATE,
    SECURITY_CODE_TO_STATE,
    SWITCH_CODE_TO_STATE,
    TEMP_CODE_TO_STATUS,
    AVAILABILITY_TOPIC,
    PAYLOAD_AVAILABLE,
    PAYLOAD_NOT_AVAILABLE,
    DEVICE_INFO,
)

HEARTBEAT_INTERVAL = 30  # seconds

_LOGGER = logging.getLogger(__name__)


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
    """Listen for RS-232 updates and dispatch to entity handlers."""
    last_time_sent = time.monotonic()
    last_heartbeat = time.monotonic()
    buffer = ""

    # Publish diagnostics autodiscovery on start
    _publish_diagnostics_autodiscovery(mqtt_client)

    while True:
        if not serial_conn.is_open:
            _LOGGER.debug("The serial connection was closed.")
            break
        try:
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

            # Read all available bytes at once (under serial lock)
            with _serial_lock:
                waiting = serial_conn.in_waiting
                if waiting > 0:
                    raw = serial_conn.read(waiting).decode(errors="ignore")
                    buffer += raw
            if not waiting:
                time.sleep(0.01)
                continue

            # Process complete messages (terminated by \r or \n)
            while "\r" in buffer or "\n" in buffer:
                cr_pos = buffer.find("\r")
                lf_pos = buffer.find("\n")
                if cr_pos == -1:
                    pos = lf_pos
                elif lf_pos == -1:
                    pos = cr_pos
                else:
                    pos = min(cr_pos, lf_pos)

                received_message = buffer[:pos].strip()
                rest = buffer[pos + 1:]
                if rest and rest[0] in ("\r", "\n"):
                    rest = rest[1:]
                buffer = rest

                if not received_message:
                    continue

                _LOGGER.info("RS-232 message received: %s", received_message)

                received_message = received_message.replace('#015', '\r')
                messages = []
                for part in received_message.split('@'):
                    sub_parts = part.split('\r')
                    messages.extend(sub_parts)

                for msg in messages:
                    if not msg:
                        continue

                    msg = '@' + msg.strip()
                    _LOGGER.info("Processing individual message: %s", msg)
                    message_parts = msg.split()

                    app_state.increment_messages()
                    _dispatch_message(serial_conn, mqtt_client, config, app_state, msg, message_parts)

        except Exception as e:
            _LOGGER.error("Error reading from RS-232 loop: %s", e)
            app_state.increment_errors()
            time.sleep(1)


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
            _LOGGER.info("OK for action light: %s", entity_id)
        elif entity_type == "R":
            _LOGGER.info("OK for action switch: %s", entity_id)
        elif entity_type == "C":
            _LOGGER.info("OK for action cover: %s", entity_id)
        elif entity_type == "S":
            _LOGGER.info("OK for action security: %s", entity_id)
        elif entity_type == "B" and entity_id == 1:
            _get_entity_state(serial_conn, mqtt_client, 1, "B", config, app_state)
            _LOGGER.info("Bypass zones re-publish.")

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
            cardio2e_lights.process_update(mqtt_client, message_parts, config)
        elif entity_type == "R":
            cardio2e_switches.process_update(mqtt_client, message_parts)
        elif entity_type == "C":
            cardio2e_covers.process_update(mqtt_client, message_parts)
        elif entity_type == "H":
            cardio2e_hvac.process_update(mqtt_client, message_parts)
        elif entity_type == "T":
            cardio2e_hvac.process_temp_update(mqtt_client, message_parts, app_state)
        elif entity_type == "S":
            cardio2e_security.process_update(mqtt_client, message_parts)
        elif entity_type == "Z":
            cardio2e_zones.process_zone_update(mqtt_client, message_parts, config)
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
        return state

    elif entity_type == "C" and len(message_parts) >= 4:
        state = message_parts[3]
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
            "mode": message_parts[6],
        }
        with app_state.lock:
            hvac_states = app_state.hvac_states
            for topic_suffix, state in topics.items():
                hvac_states = cardio2e_hvac.update_hvac_state(mqtt_client, hvac_states, int(entity_id), topic_suffix, state)
            mode_state = HVAC_CODE_TO_MODE.get(topics["mode"], "Unknown")
            hvac_states = cardio2e_hvac.update_hvac_state(mqtt_client, hvac_states, int(entity_id), "mode", mode_state)
            app_state.hvac_states = hvac_states
        return True

    elif entity_type == "S" and len(message_parts) >= 4:
        state = SECURITY_CODE_TO_STATE.get(message_parts[3], "unknown")
        mqtt_client.publish(f"cardio2e/alarm/state/{entity_id}", state, retain=True)
        return state

    elif entity_type == "Z" and len(message_parts) >= 4:
        zone_states = message_parts[3]
        for zone_id in range(1, min(16, len(zone_states)) + 1):
            zone_state = cardio2e_zones.interpret_zone_character(zone_states[zone_id - 1], zone_id, config.zones_normal_as_off)
            mqtt_client.publish(f"cardio2e/zone/state/{zone_id}", zone_state, retain=True)
        return zone_states

    elif entity_type == "B" and len(message_parts) >= 4:
        states = message_parts[3]
        for zone_id in range(1, min(16, len(states)) + 1):
            bypass_state = cardio2e_zones.interpret_bypass_character(states[zone_id - 1])
            mqtt_client.publish(f"cardio2e/zone/bypass/state/{zone_id}", bypass_state, retain=True)
        return states

    return None
