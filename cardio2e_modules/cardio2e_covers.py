import logging

_LOGGER = logging.getLogger(__name__)

def initialize_entity_cover(serial_conn, mqtt_client, get_name, get_entity_state, num_entities, fetch_names, skip_init_state):
    """
    Initialize all entities of type cover and publish them to MQTT.
    :param serial_conn: serial RS-232 connection.
    :param mqtt_client: MQTT client.
    :param num_entities: entities number
    """
    _LOGGER.info("Initializing entity state from type cover...")

    if fetch_names:
        for entity_id in range(1, num_entities + 1):
            get_name(serial_conn, entity_id, "C", mqtt_client)
    else:
        _LOGGER.info("The flag for fetching cover names is deactivated; skipping name fetch.")

    if skip_init_state:
        _LOGGER.info("The flag for fetching cover state is deactivated; skipping state fetch.")
    else:
        for entity_id in range(1, num_entities + 1):
            get_entity_state(serial_conn, mqtt_client, entity_id, "C")

    _LOGGER.info("States of all entities of type cover have been initialized.")

