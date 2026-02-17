"""MQTT setup, LWT, and message routing for cardio2e."""

import logging

import paho.mqtt.client as mqtt

from .cardio2e_constants import AVAILABILITY_TOPIC, PAYLOAD_AVAILABLE, PAYLOAD_NOT_AVAILABLE
from . import (
    cardio2e_lights,
    cardio2e_switches,
    cardio2e_covers,
    cardio2e_hvac,
    cardio2e_security,
    cardio2e_zones,
)

_LOGGER = logging.getLogger(__name__)


def create_mqtt_client(config, serial_conn, app_state, get_entity_state_fn):
    """
    Create and configure the MQTT client with LWT.
    Returns the connected client with loop already started.
    """
    client = mqtt.Client()

    # Set LWT before connecting
    client.will_set(AVAILABILITY_TOPIC, PAYLOAD_NOT_AVAILABLE, qos=1, retain=True)

    client.username_pw_set(config.mqtt_username, password=config.mqtt_password)

    # Store references in userdata for callbacks
    client.user_data_set({
        "serial_conn": serial_conn,
        "config": config,
        "app_state": app_state,
        "get_entity_state_fn": get_entity_state_fn,
    })

    client.on_connect = _on_connect
    client.on_message = _on_message

    client.connect(config.mqtt_address, config.mqtt_port, 60)
    client.loop_start()

    return client


def publish_available(mqtt_client):
    """Publish online status."""
    mqtt_client.publish(AVAILABILITY_TOPIC, PAYLOAD_AVAILABLE, qos=1, retain=True)


def publish_not_available(mqtt_client):
    """Publish offline status (used during graceful shutdown)."""
    mqtt_client.publish(AVAILABILITY_TOPIC, PAYLOAD_NOT_AVAILABLE, qos=1, retain=True)


def _on_connect(client, userdata, flags, rc):
    """Callback when the MQTT client connects."""
    _LOGGER.info("Connected to broker MQTT with code %s", rc)

    # Publish availability on connect
    publish_available(client)

    # Subscribe to all necessary topics
    client.subscribe("cardio2e/light/set/#")
    client.subscribe("cardio2e/switch/set/#")
    client.subscribe("cardio2e/cover/set/#")
    client.subscribe("cardio2e/cover/command/#")
    client.subscribe("cardio2e/hvac/+/set/#")
    client.subscribe("cardio2e/alarm/set/#")
    client.subscribe("cardio2e/zone/bypass/set/#")

    _LOGGER.info("Subscribed to all necessary topics.")


def _on_message(client, userdata, msg):
    """Callback when an MQTT message is received - routes to entity handlers."""
    serial_conn = userdata["serial_conn"]
    config = userdata["config"]
    app_state = userdata["app_state"]
    get_entity_state_fn = userdata["get_entity_state_fn"]

    topic = msg.topic
    payload = msg.payload.decode().upper()
    _LOGGER.debug("Message received on topic %s: %s", topic, payload)

    if topic.startswith("cardio2e/light/set/"):
        cardio2e_lights.handle_set_command(serial_conn, topic, payload)

    elif topic.startswith("cardio2e/switch/set/"):
        cardio2e_switches.handle_set_command(serial_conn, topic, payload)

    elif topic.startswith("cardio2e/cover/set/"):
        cardio2e_covers.handle_set_position(serial_conn, topic, payload)

    elif topic.startswith("cardio2e/cover/command/"):
        cardio2e_covers.handle_command(serial_conn, client, topic, payload, get_entity_state_fn)

    elif topic.startswith("cardio2e/hvac/") and "/set/" in topic:
        cardio2e_hvac.handle_set_command(serial_conn, client, topic, payload, app_state)

    elif topic.startswith("cardio2e/alarm/set/"):
        cardio2e_security.handle_set_command(serial_conn, topic, payload, config)

    elif topic.startswith("cardio2e/zone/bypass/set/"):
        cardio2e_zones.handle_bypass_command(serial_conn, topic, payload, app_state)
