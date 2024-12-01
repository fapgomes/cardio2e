import logging

_LOGGER = logging.getLogger(__name__)

def initialize_hvac_state(hvac_states, hvac_id, heating_setpoint, cooling_setpoint, fan_state, system_mode):
    """Initialize the state of an HVAC device with provided data."""

    if hvac_id not in hvac_states:
        hvac_states[hvac_id] = {
            "heating_setpoint": float(heating_setpoint),
            "cooling_setpoint": float(cooling_setpoint),
            "fan": "on" if fan_state == "R" else "off",
            "mode": system_mode.lower(),  # Converte para minúsculas para consistência
        }
        _LOGGER.debug("Initialized HVAC %d state: %s", hvac_id, hvac_states[hvac_id])
    else:
        _LOGGER.debug("HVAC %d state already initialized: %s", hvac_id, hvac_states[hvac_id])

    return hvac_states  # Retorna o dicionário atualizado

def update_hvac_state(mqtt_client, hvac_states, hvac_id, key, value):
    """Update the state of a specific parameter of an HVAC."""

    if hvac_id not in hvac_states:
        _LOGGER.warning("HVAC %d state not initialized. Initializing now.", hvac_id)
        hvac_states[hvac_id] = {}

    hvac_states[hvac_id][key] = value
    
    base_topic = f"cardio2e/hvac/{hvac_id}/state"
    mqtt_client.publish(f"{base_topic}/{key}", value, retain=True)

    _LOGGER.info("Updated HVAC %d: %s -> %s", hvac_id, key, value)

    return hvac_states  # Retorna o dicionário atualizado
