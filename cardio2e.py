#!/usr/bin/env python3

import logging
import os
import signal
import threading
import time

import serial

from cardio2e_modules.cardio2e_config import load_config, AppState
from cardio2e_modules.cardio2e_constants import AVAILABILITY_TOPIC, PAYLOAD_NOT_AVAILABLE
from cardio2e_modules.cardio2e_mqtt import create_mqtt_client, publish_available, publish_not_available
from cardio2e_modules.cardio2e_serial import login, logout
from cardio2e_modules.cardio2e_listener import listen_for_updates, _get_entity_state
from cardio2e_modules.cardio2e_autodiscovery import publish_config as publish_autodiscovery_config
from cardio2e_modules import cardio2e_errors, cardio2e_covers, cardio2e_lights, cardio2e_switches, cardio2e_security, cardio2e_hvac, cardio2e_zones

_LOGGER = logging.getLogger(__name__)


def get_name(serial_conn, entity_id, entity_type, mqtt_client, config, app_state):
    """Query and publish entity name, then publish autodiscovery config."""
    from cardio2e_modules.cardio2e_serial import query_name

    if entity_type == "S":
        entity_name = f"Security {entity_id}"
        publish_autodiscovery_config(mqtt_client, entity_id, entity_name, entity_type, config)
        _LOGGER.info("Published autodiscovery config for security entity %s %d without fetching name.", entity_type, entity_id)
        return entity_name

    entity_name = query_name(serial_conn, entity_id, entity_type)
    if entity_name is None:
        entity_name = "Unknown"
        _LOGGER.warning("Could not get entity name %s %d. Using default name: %s", entity_type, entity_id, entity_name)
        return entity_name

    topic_map = {
        "L": f"cardio2e/light/name/{entity_id}",
        "R": f"cardio2e/switch/name/{entity_id}",
        "C": f"cardio2e/cover/name/{entity_id}",
        "H": f"cardio2e/hvac/{entity_id}/name",
        "Z": f"cardio2e/zone/name/{entity_id}",
    }
    mqtt_topic = topic_map.get(entity_type)
    if mqtt_topic:
        mqtt_client.publish(mqtt_topic, entity_name, retain=True)
        _LOGGER.info("Entity name %s %d published to MQTT: %s", entity_type, entity_id, entity_name)

    publish_autodiscovery_config(mqtt_client, entity_id, entity_name, entity_type, config)
    return entity_name


def get_entity_state(serial_conn, mqtt_client, entity_id, entity_type, config=None, app_state=None):
    """Query entity state via serial and publish to MQTT."""
    return _get_entity_state(serial_conn, mqtt_client, entity_id, entity_type, config, app_state)


def parse_login_response(response, mqtt_client, serial_conn, config, app_state):
    """Process the login response and publish all entity states."""
    messages = response.split("\r")

    def _get_name_fn(s_conn, eid, etype, m_client):
        return get_name(s_conn, eid, etype, m_client, config, app_state)

    for message in messages:
        _LOGGER.debug("Message parsed in login response: %s", message)

        if message.startswith("@I V"):
            _LOGGER.info("System Version Info: %s", message)
            version_info = message.split()
            for i in range(2, len(version_info), 2):
                if version_info[i] == "C":
                    mqtt_client.publish("cardio2e/version/controller", version_info[i + 1], retain=True)
                elif version_info[i] == "M":
                    mqtt_client.publish("cardio2e/version/module", version_info[i + 1], retain=True)
                elif version_info[i] == "P":
                    mqtt_client.publish("cardio2e/version/protocol", version_info[i + 1], retain=True)
                elif version_info[i] == "S":
                    mqtt_client.publish("cardio2e/version/serial", version_info[i + 1], retain=True)

        elif message.startswith("@I L"):
            cardio2e_lights.process_login(mqtt_client, message, serial_conn, config, _get_name_fn)

        elif message.startswith("@I R"):
            cardio2e_switches.process_login(mqtt_client, message, serial_conn, config, _get_name_fn)

        elif message.startswith("@I H"):
            cardio2e_hvac.process_login(mqtt_client, message, serial_conn, config, app_state, _get_name_fn)

        elif message.startswith("@I T"):
            cardio2e_hvac.process_temp_update(mqtt_client, message, app_state)

        elif message.startswith("@I S"):
            cardio2e_security.process_login(mqtt_client, message)

        elif message.startswith("@I Z"):
            cardio2e_zones.process_login_zones(mqtt_client, message, serial_conn, config, _get_name_fn)

        elif message.startswith("@I B"):
            cardio2e_zones.process_login_bypass(mqtt_client, message, app_state)

    # Force inclusion of lights
    for light_id in config.force_include_lights:
        _LOGGER.info("Forcing initialization of light %s (not found in login response)", light_id)
        mqtt_client.publish(f"cardio2e/light/state/{light_id}", "OFF", retain=True)
        if config.fetch_light_names:
            _get_name_fn(serial_conn, light_id, "L", mqtt_client)
        _LOGGER.info("Forced light %s state published to MQTT: OFF", light_id)

    _LOGGER.info("Login response parsing complete.")


def main():
    # Setup logging early so we can see errors
    logging.basicConfig(level=logging.INFO)

    try:
        # Resolve config path relative to the script's directory
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(script_dir, "cardio2e.conf")
        _LOGGER.info("Loading config from: %s (exists: %s)", config_path, os.path.exists(config_path))
        cfg = load_config(config_path)
        app_state = AppState()

        # Reconfigure logging based on config
        if cfg.debug:
            logging.getLogger().setLevel(logging.DEBUG)

        # Serial connection
        serial_conn = serial.Serial(
            port=cfg.serial_port,
            baudrate=cfg.baudrate,
            write_timeout=1,
            timeout=1,
        )
        _LOGGER.info("Connection to Cardio2e established on port %s", cfg.serial_port)

        # Wrapper for get_entity_state that passes config and app_state
        def _get_entity_state_fn(s_conn, m_client, eid, etype):
            return get_entity_state(s_conn, m_client, eid, etype, cfg, app_state)

        # MQTT client with LWT
        mqtt_client = create_mqtt_client(cfg, serial_conn, app_state, _get_entity_state_fn)

        # Shutdown handler
        def handle_shutdown(signum, frame):
            _LOGGER.info("Closing signal received. Logging out...")
            publish_not_available(mqtt_client)
            logout(serial_conn)
            serial_conn.close()
            _LOGGER.info("Logout completed. Closing the program.")
            exit(0)

        signal.signal(signal.SIGTERM, handle_shutdown)
        signal.signal(signal.SIGINT, handle_shutdown)

        # Initialize error topic
        cardio2e_errors.initialize_error_payload(mqtt_client)

        # Login
        response = login(serial_conn, cfg.password)
        if response:
            parse_login_response(response, mqtt_client, serial_conn, cfg, app_state)
            # Initialize covers (not included in login response)
            cardio2e_covers.initialize_entity_cover(
                serial_conn, mqtt_client,
                lambda s, eid, etype, m: get_name(s, eid, etype, m, cfg, app_state),
                _get_entity_state_fn,
                cfg.ncovers, cfg.fetch_cover_names, cfg.skip_init_cover_state,
            )
            # Publish security autodiscovery (always entity 1)
            get_name(serial_conn, 1, "S", mqtt_client, cfg, app_state)

        _LOGGER.info("\n################\nCardio2e ready. Listening for events.\n################")

        # Start listener thread
        listener_thread = threading.Thread(
            target=listen_for_updates,
            args=(serial_conn, mqtt_client, cfg, app_state),
            daemon=True,
        )
        listener_thread.start()

        while True:
            time.sleep(0.1)

    except Exception as e:
        _LOGGER.error("Failed to configure Cardio2e: %s", e)


if __name__ == "__main__":
    main()
