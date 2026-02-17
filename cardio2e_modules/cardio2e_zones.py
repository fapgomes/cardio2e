"""Zone entity logic for cardio2e."""

import logging
import re

from .cardio2e_serial import send_command

_LOGGER = logging.getLogger(__name__)


def interpret_zone_character(character, zone_id, zones_normal_as_off):
    """
    Interpret a zone state character, with state inversion for specific zones.
    """
    is_inverted = zone_id in zones_normal_as_off

    _LOGGER.debug("Security %d state, updated to: %s", zone_id, character)
    if character == "O":
        return "OFF" if is_inverted else "ON"
    elif character == "N":
        return "OFF" if is_inverted else "ON"
    elif character == "C":
        return "ON" if is_inverted else "OFF"
    elif character == "E":
        return "ERROR"
    else:
        return "UNKNOWN"


def interpret_bypass_character(character):
    """Interpret a bypass state character."""
    if character == "Y":
        return "ON"
    elif character == "N":
        return "OFF"
    else:
        return "UNKNOWN"


def handle_bypass_command(serial_conn, topic, payload, app_state):
    """Handle an MQTT bypass set command for a zone."""
    _LOGGER.debug("Entering bypass processing...")

    try:
        zone_id = int(topic.split("/")[-1])
    except ValueError:
        _LOGGER.error("Invalid zone ID on topic: %s", topic)
        return

    with app_state.lock:
        _LOGGER.info("Current Zones: %s", app_state.bypass_states)

        if not app_state.bypass_states or len(app_state.bypass_states) != 16:
            _LOGGER.error("Failed to get current state of zones. Using default state.")
            app_state.bypass_states = "N" * 16

        zone_bypass_states = list(app_state.bypass_states)

        if payload == "ON":
            zone_bypass_states[zone_id - 1] = "Y"
            _LOGGER.info("Zone %d deactivated", zone_id)
        elif payload == "OFF":
            zone_bypass_states[zone_id - 1] = "N"
            _LOGGER.info("Zone %d activated", zone_id)
        else:
            _LOGGER.error("Invalid payload for zone bypass control: %s", payload)
            return

        try:
            success = send_command(serial_conn, "B", 1, "".join(zone_bypass_states))
            if success:
                app_state.bypass_states = "".join(zone_bypass_states)
                _LOGGER.info("Bypass states updated successfully: %s", app_state.bypass_states)
            else:
                _LOGGER.warning("Bypass command failed, state not updated.")
        except Exception as e:
            _LOGGER.error("Error sending bypass command: %s", e)


def process_zone_update(mqtt_client, message_parts, config):
    """Process an @I Z update from the serial listener."""
    zone_states = message_parts[3]

    for zone_id in range(1, len(zone_states) + 1):
        zone_state_char = zone_states[zone_id - 1]
        zone_state = interpret_zone_character(zone_state_char, zone_id, config.zones_normal_as_off)

        state_topic = f"cardio2e/zone/state/{zone_id}"
        mqtt_client.publish(state_topic, zone_state, retain=False)
        if zone_state == "ON":
            _LOGGER.info("Status of zone %d published to MQTT: %s", zone_id, zone_state)
        _LOGGER.debug("Status of zone %d published to MQTT: %s", zone_id, zone_state)


def process_bypass_update(mqtt_client, message_parts, app_state):
    """Process an @I B update from the serial listener."""
    states = message_parts[3]

    for zone_id in range(1, len(states) + 1):
        bypass_state_char = states[zone_id - 1]
        bypass_state = interpret_bypass_character(bypass_state_char)

        state_topic = f"cardio2e/zone/bypass/state/{zone_id}"
        mqtt_client.publish(state_topic, bypass_state, retain=False)


def process_login_zones(mqtt_client, message, serial_conn, config, get_name_fn):
    """Process @I Z messages from the login response."""
    match = re.match(r"@I Z \d+ ([CO]+)", message)
    if match:
        zone_states = match.group(1)
        for i, state_char in enumerate(zone_states, start=1):
            zone_state = interpret_zone_character(state_char, i, config.zones_normal_as_off)
            mqtt_client.publish(f"cardio2e/zone/state/{i}", zone_state, retain=True)
            if config.fetch_zone_names:
                get_name_fn(serial_conn, int(i), "Z", mqtt_client)
            else:
                _LOGGER.info("The flag for fetching zones names is deactivated; skipping name fetch.")
            _LOGGER.info("Zone %d state published to MQTT: %s", i, zone_state)


def process_login_bypass(mqtt_client, message, app_state):
    """Process @I B messages from the login response."""
    match = re.match(r"@I B \d+ ([NY]+)", message)
    if match:
        states = match.group(1)
        with app_state.lock:
            app_state.bypass_states = states
        for i, bypass_state_char in enumerate(states, start=1):
            bypass_state = interpret_bypass_character(bypass_state_char)
            mqtt_client.publish(f"cardio2e/zone/bypass/state/{i}", bypass_state, retain=True)
            _LOGGER.info("Bypass state for zone %d published to MQTT: %s", i, bypass_state)
