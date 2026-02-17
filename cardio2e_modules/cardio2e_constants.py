"""Constants and mappings shared across all cardio2e modules."""

CARDIO2E_TERMINATOR = "\r"

# MQTT availability (LWT)
AVAILABILITY_TOPIC = "cardio2e/status"
PAYLOAD_AVAILABLE = "online"
PAYLOAD_NOT_AVAILABLE = "offline"

# HVAC mode mappings
HVAC_MODE_TO_CODE = {
    "auto": "A",
    "heat": "H",
    "cool": "C",
    "off": "O",
    "economy": "E",
    "normal": "N",
}

HVAC_CODE_TO_MODE = {v: k for k, v in HVAC_MODE_TO_CODE.items()}

# HVAC fan state mappings
FAN_STATE_TO_CODE = {"on": "R", "off": "S"}
FAN_CODE_TO_STATE = {v: k for k, v in FAN_STATE_TO_CODE.items()}

# Security state mappings
SECURITY_CODE_TO_STATE = {
    "A": "armed_away",
    "D": "disarmed",
}

# Switch state mappings
SWITCH_CODE_TO_STATE = {
    "O": "ON",
    "C": "OFF",
}

# Temperature status mappings
TEMP_CODE_TO_STATUS = {
    "H": "heat",
    "C": "cool",
    "O": "off",
}

# Error codes returned by cardio2e (@N messages)
ERROR_CODES = {
    "1": "Object type specified by the transaction is not recognized",
    "2": "Object number is out of range for the object type specified",
    "3": "One or more parameters are not valid",
    "4": "Security code is not valid",
    "5": "Transaction S (Set) not supported for the requested type of object",
    "6": "Transaction G (Get) not supported for the requested type of object",
    "7": "Transaction is refused because security is armed",
    "8": "This zone can be ignored",
    "16": "Security can not be armed because there are open zones",
    "17": "Security can not be armed because there is a power problem",
    "18": "Security can not be armed for an unknown reason",
}

# Device info for autodiscovery payloads
DEVICE_INFO = {
    "L": {
        "identifiers": ["Cardio2e Lights"],
        "name": "Cardio2e Lights",
        "model": "Cardio2e",
        "manufacturer": "Cardio2e Manufacturer",
    },
    "R": {
        "identifiers": ["Cardio2e Switches"],
        "name": "Cardio2e Switches",
        "model": "Cardio2e",
        "manufacturer": "Cardio2e Manufacturer",
    },
    "C": {
        "identifiers": ["Cardio2e Covers"],
        "name": "Cardio2e Covers",
        "model": "Cardio2e",
        "manufacturer": "Cardio2e Manufacturer",
    },
    "H": {
        "identifiers": ["Cardio2e HVAC"],
        "name": "Cardio2e HVAC",
        "model": "Cardio2e",
        "manufacturer": "Cardio2e Manufacturer",
    },
    "S": {
        "identifiers": ["Cardio2e Alarm"],
        "name": "Cardio2e Alarm",
        "model": "Cardio2e",
        "manufacturer": "Cardio2e Manufacturer",
    },
    "Z": {
        "identifiers": ["Cardio2e Zones"],
        "name": "Cardio2e Zones",
        "model": "Cardio2e",
        "manufacturer": "Cardio2e Manufacturer",
    },
    "errors": {
        "identifiers": ["Cardio2e System Errors"],
        "name": "Cardio2e System Errors",
        "model": "Cardio2e",
        "manufacturer": "Cardio2e Manufacturer",
    },
}
