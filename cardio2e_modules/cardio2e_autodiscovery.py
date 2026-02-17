"""Home Assistant MQTT autodiscovery payloads for cardio2e."""

import json
import logging

from .cardio2e_constants import (
    AVAILABILITY_TOPIC,
    PAYLOAD_AVAILABLE,
    PAYLOAD_NOT_AVAILABLE,
    DEVICE_INFO,
)

_LOGGER = logging.getLogger(__name__)


def _availability_block():
    """Return the common availability fields for all payloads."""
    return {
        "availability_topic": AVAILABILITY_TOPIC,
        "payload_available": PAYLOAD_AVAILABLE,
        "payload_not_available": PAYLOAD_NOT_AVAILABLE,
    }


def _light_config(entity_id, entity_name, config):
    """Build autodiscovery payload for a light."""
    config_topic = f"homeassistant/light/cardio2e_{entity_id}/config"
    state_topic = f"cardio2e/light/state/{entity_id}"
    command_topic = f"cardio2e/light/set/{entity_id}"

    config_payload = {
        "name": entity_name,
        "unique_id": f"cardio2e_light_{entity_id}",
        "state_topic": state_topic,
        "command_topic": command_topic,
        "payload_on": "ON",
        "payload_off": "OFF",
        "qos": 1,
        "retain": False,
        "device": DEVICE_INFO["L"],
        **_availability_block(),
    }

    if entity_id in config.dimmer_lights:
        brightness_state_topic = f"cardio2e/light/brightness/{entity_id}"
        config_payload.update({
            "brightness": True,
            "brightness_state_topic": brightness_state_topic,
            "brightness_command_topic": command_topic,
            "brightness_scale": 100,
            "on_command_type": "brightness",
        })

    return config_topic, config_payload


def _switch_config(entity_id, entity_name):
    """Build autodiscovery payload for a switch."""
    config_topic = f"homeassistant/switch/cardio2e_switch_{entity_id}/config"
    command_topic = f"cardio2e/switch/set/{entity_id}"
    state_topic = f"cardio2e/switch/state/{entity_id}"

    config_payload = {
        "name": entity_name,
        "unique_id": f"cardio2e_switch_{entity_id}",
        "command_topic": command_topic,
        "state_topic": state_topic,
        "payload_on": "ON",
        "payload_off": "OFF",
        "qos": 1,
        "retain": False,
        "device": DEVICE_INFO["R"],
        **_availability_block(),
    }

    return config_topic, config_payload


def _cover_config(entity_id, entity_name):
    """Build autodiscovery payload for a cover."""
    config_topic = f"homeassistant/cover/cardio2e_cover_{entity_id}/config"
    position_topic = f"cardio2e/cover/state/{entity_id}"
    set_position_topic = f"cardio2e/cover/set/{entity_id}"
    command_topic = f"cardio2e/cover/command/{entity_id}"

    config_payload = {
        "name": entity_name,
        "unique_id": f"cardio2e_cover_{entity_id}",
        "position_topic": position_topic,
        "set_position_topic": set_position_topic,
        "command_topic": command_topic,
        "payload_open": "OPEN",
        "payload_close": "CLOSE",
        "payload_stop": "STOP",
        "position_open": 100,
        "position_closed": 0,
        "optimistic": False,
        "qos": 1,
        "retain": False,
        "device": DEVICE_INFO["C"],
        **_availability_block(),
    }

    return config_topic, config_payload


def _hvac_config(entity_id, entity_name):
    """Build autodiscovery payload for an HVAC entity (climate)."""
    state_topic_base = f"cardio2e/hvac/{entity_id}/state"
    command_topic_base = f"cardio2e/hvac/{entity_id}/set"

    config_topic = f"homeassistant/climate/cardio2e_hvac_{entity_id}/config"
    config_payload = {
        "name": entity_name,
        "unique_id": f"cardio2e_hvac_{entity_id}",
        "state_topic": state_topic_base,
        "current_temperature_topic": f"{state_topic_base}/current_temperature",
        "temperature_state_topic": f"{state_topic_base}/cooling_setpoint",
        "temperature_command_topic": f"{command_topic_base}/cooling_setpoint",
        "temp_step": "1",
        "mode_state_topic": f"{state_topic_base}/mode",
        "mode_command_topic": f"{command_topic_base}/mode",
        "modes": ["auto", "heat", "cool", "off"],
        "fan_mode_state_topic": f"{state_topic_base}/fan",
        "fan_mode_command_topic": f"{command_topic_base}/fan",
        "fan_modes": ["on", "off"],
        "min_temp": 7,
        "max_temp": 35,
        "qos": 1,
        "retain": False,
        "device": DEVICE_INFO["H"],
        **_availability_block(),
    }

    return config_topic, config_payload


def _alarm_config(entity_id, entity_name):
    """Build autodiscovery payload for the alarm control panel."""
    config_topic = f"homeassistant/alarm_control_panel/cardio2e_alarm_{entity_id}/config"
    command_topic = f"cardio2e/alarm/set/{entity_id}"
    state_topic = f"cardio2e/alarm/state/{entity_id}"

    config_payload = {
        "name": entity_name,
        "unique_id": f"cardio2e_alarm_{entity_id}",
        "command_topic": command_topic,
        "state_topic": state_topic,
        "payload_arm_away": "armed_away",
        "payload_disarm": "disarmed",
        "code_arm_required": False,
        "code_disarm_required": False,
        "supported_features": ["arm_away", "arm_night"],
        "qos": 1,
        "retain": False,
        "device": DEVICE_INFO["S"],
        **_availability_block(),
    }

    return config_topic, config_payload


def _zone_config(entity_id, entity_name):
    """Build autodiscovery payloads for a zone (binary sensor + bypass switch)."""
    # Binary sensor for zone state
    sensor_config_topic = f"homeassistant/binary_sensor/cardio2e_zone_{entity_id}/config"
    sensor_state_topic = f"cardio2e/zone/state/{entity_id}"

    sensor_config_payload = {
        "name": entity_name,
        "unique_id": f"cardio2e_zone_{entity_id}",
        "state_topic": sensor_state_topic,
        "payload_on": "ON",
        "payload_off": "OFF",
        "device_class": "motion",
        "qos": 1,
        "retain": False,
        "device": DEVICE_INFO["Z"],
        **_availability_block(),
    }

    # Switch for bypass control
    switch_config_topic = f"homeassistant/switch/cardio2e_zone_{entity_id}_bypass/config"
    bypass_state_topic = f"cardio2e/zone/bypass/state/{entity_id}"
    bypass_command_topic = f"cardio2e/zone/bypass/set/{entity_id}"

    switch_config_payload = {
        "name": f"{entity_name} Bypass",
        "unique_id": f"cardio2e_zone_bypass_{entity_id}",
        "state_topic": bypass_state_topic,
        "command_topic": bypass_command_topic,
        "payload_on": "ON",
        "payload_off": "OFF",
        "qos": 1,
        "retain": False,
        "device": DEVICE_INFO["Z"],
        **_availability_block(),
    }

    return sensor_config_topic, sensor_config_payload, switch_config_topic, switch_config_payload


def publish_config(mqtt_client, entity_id, entity_name, entity_type, config=None):
    """
    Publish the autodiscovery configuration for a given entity.
    :param mqtt_client: MQTT client.
    :param entity_id: Entity ID.
    :param entity_name: Entity name.
    :param entity_type: Entity type code (L, R, C, H, S, Z).
    :param config: AppConfig instance (needed for light dimmer info).
    """
    _LOGGER.debug("Publishing autodiscovery info for %s", entity_name)

    if entity_type == "L":
        topic, payload = _light_config(entity_id, entity_name, config)
        mqtt_client.publish(topic, json.dumps(payload), retain=True)
        _LOGGER.info("Published autodiscovery config for light: %s", entity_name)

    elif entity_type == "R":
        topic, payload = _switch_config(entity_id, entity_name)
        mqtt_client.publish(topic, json.dumps(payload), retain=True)
        _LOGGER.info("Publish autodiscovery config for switches (relays): %s", entity_name)

    elif entity_type == "C":
        topic, payload = _cover_config(entity_id, entity_name)
        mqtt_client.publish(topic, json.dumps(payload), retain=True)
        _LOGGER.info("Publish autodiscovery config for cover: %s", entity_name)

    elif entity_type == "H":
        topic, payload = _hvac_config(entity_id, entity_name)
        mqtt_client.publish(topic, json.dumps(payload), retain=True)
        _LOGGER.info("Published autodiscovery config for consolidated HVAC entity: %s", entity_name)

    elif entity_type == "S":
        topic, payload = _alarm_config(entity_id, entity_name)
        mqtt_client.publish(topic, json.dumps(payload), retain=True)
        _LOGGER.info("Published autodiscovery config for alarm: %s", entity_name)

    elif entity_type == "Z":
        sensor_topic, sensor_payload, switch_topic, switch_payload = _zone_config(entity_id, entity_name)
        mqtt_client.publish(sensor_topic, json.dumps(sensor_payload), retain=True)
        _LOGGER.info("Published autodiscovery config for binary sensor (zone): %s", entity_name)
        mqtt_client.publish(switch_topic, json.dumps(switch_payload), retain=True)
        _LOGGER.info("Published autodiscovery config for zone bypass switch: %s", entity_name)
