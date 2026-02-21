"""Configuration loading and application state for cardio2e."""

import configparser
import json
import logging
import threading
import time

_LOGGER = logging.getLogger(__name__)


def _parse_list_config(raw_value, field_name):
    """Parse a list value from config, returning list of ints."""
    try:
        result = json.loads(raw_value)
        if not isinstance(result, list):
            raise ValueError("%s must be a list." % field_name)
        return [int(item) for item in result]
    except (ValueError, json.JSONDecodeError) as e:
        _LOGGER.error("Error interpreting %s in config file: %s", field_name, e)
        return []


class AppConfig(object):
    """All configuration parameters loaded from the .conf file."""

    def __init__(self, **kwargs):
        # Global
        self.debug = kwargs.get("debug", 0)
        self.ha_discover_prefix = kwargs.get("ha_discover_prefix", "homeassistant")

        # Cardio2e
        self.serial_port = kwargs.get("serial_port", "/dev/ttyUSB0")
        self.baudrate = kwargs.get("baudrate", 9600)
        self.password = kwargs.get("password", "00000")
        self.update_date_interval = kwargs.get("update_date_interval", 3600)

        # Lights
        self.fetch_light_names = kwargs.get("fetch_light_names", True)
        self.dimmer_lights = kwargs.get("dimmer_lights", [])
        self.force_include_lights = kwargs.get("force_include_lights", [])

        # Switches
        self.fetch_switch_names = kwargs.get("fetch_switch_names", True)

        # Covers
        self.fetch_cover_names = kwargs.get("fetch_cover_names", True)
        self.skip_init_cover_state = kwargs.get("skip_init_cover_state", False)
        self.ncovers = kwargs.get("ncovers", 20)

        # HVAC
        self.fetch_names_hvac = kwargs.get("fetch_names_hvac", True)

        # Security
        self.alarm_code = kwargs.get("alarm_code", 12345)

        # Zones
        self.fetch_zone_names = kwargs.get("fetch_zone_names", True)
        self.zones_normal_as_off = kwargs.get("zones_normal_as_off", [])

        # Syslog
        self.syslog_address = kwargs.get("syslog_address", "")
        self.syslog_port = kwargs.get("syslog_port", 514)

        # MQTT
        self.mqtt_address = kwargs.get("mqtt_address", "localhost")
        self.mqtt_port = kwargs.get("mqtt_port", 1883)
        self.mqtt_username = kwargs.get("mqtt_username", "")
        self.mqtt_password = kwargs.get("mqtt_password", "")


class AppState(object):
    """Mutable application state (replaces global variables). Thread-safe."""
    def __init__(self):
        self._lock = threading.RLock()
        self._hvac_states = {}
        self._bypass_states = ""
        self._entity_names = {}  # {(entity_type, entity_id): name}
        # Diagnostics counters (atomic increments via lock)
        self._messages_processed = 0
        self._errors_count = 0
        self._last_command = ""
        self._start_time = time.monotonic()

    def increment_messages(self):
        with self._lock:
            self._messages_processed += 1

    def increment_errors(self):
        with self._lock:
            self._errors_count += 1

    def set_last_command(self, cmd):
        with self._lock:
            self._last_command = cmd

    def get_diagnostics(self):
        with self._lock:
            uptime_seconds = int(time.monotonic() - self._start_time)
            return {
                "uptime_seconds": uptime_seconds,
                "messages_processed": self._messages_processed,
                "errors_count": self._errors_count,
                "last_command": self._last_command,
            }

    @property
    def hvac_states(self):
        with self._lock:
            return self._hvac_states

    @hvac_states.setter
    def hvac_states(self, value):
        with self._lock:
            self._hvac_states = value

    @property
    def bypass_states(self):
        with self._lock:
            return self._bypass_states

    @bypass_states.setter
    def bypass_states(self, value):
        with self._lock:
            self._bypass_states = value

    def set_entity_name(self, entity_type, entity_id, name):
        """Store the friendly name of an entity."""
        with self._lock:
            self._entity_names[(entity_type, int(entity_id))] = name

    def get_entity_label(self, prefix, entity_type, entity_id):
        """Return 'prefix name (id: N)' if name exists, otherwise 'prefix N'."""
        with self._lock:
            name = self._entity_names.get((entity_type, int(entity_id)))
        if name and name != "Unknown":
            return "%s %s (id: %d)" % (prefix, name, int(entity_id))
        return "%s %d" % (prefix, int(entity_id))

    @property
    def lock(self):
        """Expose lock for operations that need read-modify-write atomicity."""
        return self._lock


def load_config(path="cardio2e.conf"):
    """Parse the .conf file and return an AppConfig instance."""
    config = configparser.ConfigParser()
    files_read = config.read(path)
    if not files_read:
        raise RuntimeError("Config file not found or not readable: %s" % path)
    _LOGGER.info("Config loaded from: %s, sections: %s", path, config.sections())

    c2e = config["cardio2e"]
    mqtt = config["mqtt"]
    glb = config["global"]

    return AppConfig(
        debug=int(glb.get("debug", "0")),
        ha_discover_prefix=glb.get("ha_discover_prefix", "homeassistant"),
        serial_port=c2e.get("serial_port", "/dev/ttyUSB0"),
        baudrate=int(c2e.get("baudrate", "9600")),
        password=c2e["password"],
        update_date_interval=int(c2e.get("update_date_interval", "3600")),
        fetch_light_names=c2e.get("fetch_light_names", "true").lower() == "true",
        dimmer_lights=_parse_list_config(c2e.get("dimmer_lights", "[]"), "dimmer_lights"),
        force_include_lights=_parse_list_config(c2e.get("force_include_lights", "[]"), "force_include_lights"),
        fetch_switch_names=c2e.get("fetch_switch_names", "true").lower() == "true",
        fetch_cover_names=c2e.get("fetch_cover_names", "true").lower() == "true",
        skip_init_cover_state=c2e.get("skip_init_cover_state", "false").lower() == "true",
        ncovers=int(c2e.get("ncovers", "20")),
        fetch_names_hvac=c2e.get("fetch_names_hvac", "true").lower() == "true",
        alarm_code=int(c2e.get("code", "12345")),
        fetch_zone_names=c2e.get("fetch_zone_names", "true").lower() == "true",
        zones_normal_as_off=_parse_list_config(c2e.get("zones_normal_as_off", "[]"), "zones_normal_as_off"),
        syslog_address=glb.get("syslog_address", ""),
        syslog_port=int(glb.get("syslog_port", "514")),
        mqtt_address=mqtt["address"],
        mqtt_port=int(mqtt["port"]),
        mqtt_username=mqtt["username"],
        mqtt_password=mqtt["password"],
    )
