import json
import logging
from datetime import datetime

_LOGGER = logging.getLogger(__name__)

def report_error_state(mqtt_client, error):
    """
    Reporta o estado do erro no tópico MQTT associado.
    
    :param mqtt_client: Instância do cliente MQTT.
    :param error_description: Descrição detalhada do erro.
    """
    # Tópico de estado para o erro
    state_topic = f"cardio2e/errors/state"

    # Payload de estado do erro
    error_state_payload = {
        "error": error,
        "timestamp": datetime.utcnow().isoformat()
    }

    # Publicar o estado do erro
    mqtt_client.publish(state_topic, json.dumps(error_state_payload), retain=True)
    _LOGGER.info("Published state for error: %s", error)

def initialize_error_payload(mqtt_client):
    """
    Inicializa o payload de configuração do sensor de erro no Home Assistant via autodiscovery MQTT.

    :param mqtt_client: Instância do cliente MQTT.
    """
    # Tópico de configuração para autodiscovery
    sensor_config_topic = f"homeassistant/sensor/cardio2e_errors/config"
    state_topic = f"cardio2e/errors/state"

    # Payload de configuração do sensor de erro
    sensor_config_payload = {
        "name": "Cardio2e Errors",
        "unique_id": f"cardio2e_error",
        "state_topic": state_topic,
        "icon": "mdi:alert-circle-outline",
        "qos": 1,
        "retain": True,
        "value_template": "{{ value_json.error }}",
        "device": {
            "identifiers": ["Cardio2e System Errors"],
            "name": "Cardio2e System Errors",
            "model": "Cardio2e",
            "manufacturer": "Cardio2e Manufacturer"
        }
    }

    # Publicar a configuração do sensor no Home Assistant
    mqtt_client.publish(sensor_config_topic, json.dumps(sensor_config_payload), retain=True)
    _LOGGER.info("Published autodiscovery config for error sensor.")
