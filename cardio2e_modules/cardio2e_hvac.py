"""HVAC entity logic for cardio2e."""

import logging
import re
from .cardio2e_constants import (
    HVAC_CODE_TO_MODE,
    FAN_CODE_TO_STATE,
    TEMP_CODE_TO_STATUS,
)
from .cardio2e_serial import send_command

_LOGGER = logging.getLogger(__name__)



def initialize_hvac_state(hvac_states, hvac_id, heating_setpoint, cooling_setpoint, fan_state, system_mode):
    """Initialize the state of an HVAC device with provided data."""
    if hvac_id not in hvac_states:
        hvac_states[hvac_id] = {
            "heating_setpoint": float(heating_setpoint),
            "cooling_setpoint": float(cooling_setpoint),
            "fan": FAN_CODE_TO_STATE.get(fan_state, "off"),
            "mode": system_mode.lower(),
        }
        _LOGGER.debug("Initialized HVAC %d state: %s", hvac_id, hvac_states[hvac_id])
    else:
        _LOGGER.debug("HVAC %d state already initialized: %s", hvac_id, hvac_states[hvac_id])
    return hvac_states


def update_hvac_state(mqtt_client, hvac_states, hvac_id, key, value):
    """Update the state of a specific parameter of an HVAC."""
    if hvac_id not in hvac_states:
        _LOGGER.warning("HVAC %d state not initialized. Initializing now.", hvac_id)
        hvac_states[hvac_id] = {}

    hvac_states[hvac_id][key] = value

    base_topic = f"cardio2e/hvac/{hvac_id}/state"
    mqtt_client.publish(f"{base_topic}/{key}", value, retain=True)
    _LOGGER.info("Updated HVAC %d: %s -> %s", hvac_id, key, value)

    return hvac_states


def handle_set_command(serial_conn, mqtt_client, topic, payload, app_state):
    """Handle an MQTT set command for HVAC."""
    try:
        parts = topic.split("/")
        hvac_id = int(parts[2])
        setting_type = parts[-1]

        with app_state.lock:
            hvac_states = app_state.hvac_states
            _LOGGER.info("HVAC %d current state before command: %s", hvac_id, hvac_states.get(hvac_id, "NOT FOUND"))

            if hvac_id not in hvac_states or "cooling_setpoint" not in hvac_states.get(hvac_id, {}):
                _LOGGER.warning("HVAC %d state not yet initialized by login. Ignoring command.", hvac_id)
                return

            if setting_type == "heating_setpoint":
                hvac_states[hvac_id]["heating_setpoint"] = float(payload)
            elif setting_type == "cooling_setpoint":
                hvac_states[hvac_id]["cooling_setpoint"] = float(payload)
            elif setting_type == "fan":
                hvac_states[hvac_id]["fan"] = payload.lower()
            elif setting_type == "mode":
                hvac_states[hvac_id]["mode"] = payload.lower()
            else:
                _LOGGER.error("Unknown setting type for HVAC: %s", setting_type)
                return

            heating_setpoint = float(hvac_states[hvac_id]["cooling_setpoint"]) - 2
            cooling_setpoint = float(hvac_states[hvac_id]["cooling_setpoint"])
            fan_state = hvac_states[hvac_id]["fan"]
            mode = hvac_states[hvac_id]["mode"]

            app_state.hvac_states = hvac_states

        send_command(
            serial_conn=serial_conn,
            entity_type="H",
            entity_id=hvac_id,
            heating_setpoint=heating_setpoint,
            cooling_setpoint=cooling_setpoint,
            fan_state=fan_state,
            mode=mode,
        )

        with app_state.lock:
            hvac_states = app_state.hvac_states
            hvac_states = update_hvac_state(mqtt_client, hvac_states, int(hvac_id), "heating_setpoint", heating_setpoint)
            hvac_states = update_hvac_state(mqtt_client, hvac_states, int(hvac_id), "cooling_setpoint", cooling_setpoint)
            hvac_states = update_hvac_state(mqtt_client, hvac_states, int(hvac_id), "fan", fan_state)
            hvac_states = update_hvac_state(mqtt_client, hvac_states, int(hvac_id), "mode", mode)
            app_state.hvac_states = hvac_states

        _LOGGER.info("Updated HVAC %d topics with new settings: Heating %.1f, Cooling %.1f, Fan %s, Mode %s",
                      hvac_id, heating_setpoint, cooling_setpoint, fan_state, mode)

    except ValueError:
        _LOGGER.error("Invalid topic or payload for HVAC command: %s", topic)
    except Exception as e:
        _LOGGER.error("Error processing HVAC message: %s", e)


def process_update(mqtt_client, message_parts, app_state):
    """Process an @I H update from the serial listener."""
    hvac_id = int(message_parts[2])
    heating_setpoint = message_parts[3]
    cooling_setpoint = message_parts[4]
    fan_state = FAN_CODE_TO_STATE.get(message_parts[5], "off")
    mode_state = HVAC_CODE_TO_MODE.get(message_parts[6], "unknown")

    base_topic = f"cardio2e/hvac/{hvac_id}/state"

    mqtt_client.publish(f"{base_topic}/heating_setpoint", heating_setpoint, retain=True)
    _LOGGER.info("HVAC %d heating setpoint updated to: %s", hvac_id, heating_setpoint)

    mqtt_client.publish(f"{base_topic}/cooling_setpoint", cooling_setpoint, retain=True)
    _LOGGER.info("HVAC %d cooling setpoint updated to: %s", hvac_id, cooling_setpoint)

    mqtt_client.publish(f"{base_topic}/fan", fan_state, retain=True)
    _LOGGER.info("HVAC %d fan state updated to: %s", hvac_id, fan_state)

    mqtt_client.publish(f"{base_topic}/mode", mode_state, retain=True)
    _LOGGER.info("HVAC %d mode updated to: %s", hvac_id, mode_state)

    with app_state.lock:
        hvac_states = app_state.hvac_states
        if hvac_id not in hvac_states:
            hvac_states[hvac_id] = {}
        hvac_states[hvac_id]["heating_setpoint"] = float(heating_setpoint)
        hvac_states[hvac_id]["cooling_setpoint"] = float(cooling_setpoint)
        hvac_states[hvac_id]["fan"] = fan_state
        hvac_states[hvac_id]["mode"] = mode_state
        app_state.hvac_states = hvac_states


def process_temp_update(mqtt_client, message_parts, app_state):
    """Process an @I T update from the serial listener or login."""
    match = re.match(r"@I T (\d+) (\d+\.\d+) ([HCO])", " ".join(message_parts) if isinstance(message_parts, list) else message_parts)
    if match:
        temp_sensor_id, temp_value, temp_status = match.groups()
        temp_status_value = TEMP_CODE_TO_STATUS.get(temp_status, "Unknown")
        with app_state.lock:
            hvac_states = app_state.hvac_states
            hvac_states = update_hvac_state(mqtt_client, hvac_states, int(temp_sensor_id), "current_temperature", temp_value)
            hvac_states = update_hvac_state(mqtt_client, hvac_states, int(temp_sensor_id), "alternative_status_from_temp", temp_status_value)
            app_state.hvac_states = hvac_states
        _LOGGER.info("Temperature sensor %s state published to MQTT: %s C, Status: %s", temp_sensor_id, temp_value, temp_status_value)


def process_login(mqtt_client, message, serial_conn, config, app_state, get_name_fn):
    """Process @I H messages from the login response."""
    match = re.match(r"@I H (\d+) (\d+\.\d+) (\d+\.\d+) ([SR]) ([AHCOEN])", message)
    if match:
        hvac_id, heating_setpoint, cooling_setpoint, fan_state, system_mode = match.groups()
        fan_state_value = FAN_CODE_TO_STATE.get(fan_state, "off")
        hvac_state = HVAC_CODE_TO_MODE.get(system_mode, "Unknown")

        with app_state.lock:
            hvac_states = app_state.hvac_states
            hvac_states = initialize_hvac_state(hvac_states, int(hvac_id), heating_setpoint, cooling_setpoint, fan_state, hvac_state)
            hvac_states = update_hvac_state(mqtt_client, hvac_states, int(hvac_id), "heating_setpoint", heating_setpoint)
            hvac_states = update_hvac_state(mqtt_client, hvac_states, int(hvac_id), "cooling_setpoint", cooling_setpoint)
            hvac_states = update_hvac_state(mqtt_client, hvac_states, int(hvac_id), "fan", fan_state_value)
            hvac_states = update_hvac_state(mqtt_client, hvac_states, int(hvac_id), "mode", hvac_state)
            app_state.hvac_states = hvac_states

        if config.fetch_names_hvac:
            get_name_fn(serial_conn, int(hvac_id), "H", mqtt_client)
        else:
            _LOGGER.info("The flag for fetching hvac names is deactivated; skipping name fetch.")

        _LOGGER.info("HVAC %s state published to MQTT: Heating Set Point: %s, Cooling Set Point: %s, Fan State: %s, System mode: %s",
                      hvac_id, heating_setpoint, cooling_setpoint, fan_state, system_mode)
