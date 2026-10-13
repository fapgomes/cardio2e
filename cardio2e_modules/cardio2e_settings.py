"""Home Assistant config entities for the outlet and lamp settings kept in the
configuration memory (names, auto off, durations, reactions, periods, ...).

Every outlet (relay) and lamp known to the bridge gets a set of MQTT
entities with ``entity_category: config`` on the existing Cardio2e Switches
and Cardio2e Lights devices; only the most useful ones are enabled by
default. The records (21 bytes each, layout in ``cardio2e_memory``) are read
once in the background after startup and on demand (reload button, after a
restore from the web panel). Changing a value reads the record again,
changes only that field, writes the changed bytes and reads them back; the
new state is published only after that.

All memory access runs in one worker thread and holds the memory service's
exclusive lock, so it never overlaps a backup or a restore.
"""

import json
import logging
import queue
import re
import threading
import time

from .cardio2e_autodiscovery import _availability_block, publish_config
from .cardio2e_constants import DEVICE_INFO
from .cardio2e_memory import (
    LAMPS_BASE, LAMPS_COUNT, NAME_SIZE, OUTLETS_BASE, OUTLETS_COUNT, RECORD_SIZE,
    read_memory, write_memory,
)

_LOGGER = logging.getLogger(__name__)

TOPIC_ROOT = "cardio2e/config"
RELOAD_TOPIC = TOPIC_ROOT + "/reload"
LOCK_TIMEOUT = 5.0      # seconds a change waits for a running backup/restore
NAME_LENGTH = NAME_SIZE - 1
NAME_RE = re.compile(r"^[ -~]{1,%d}$" % NAME_LENGTH)   # printable ASCII
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
NO_TIME = "--:--"
PERIOD_MODES = (("Time", None), ("Sunset", 0xE0), ("Sunrise", 0xD0))
FLAGS_OFFSET = 11
ZONE_OFFSET = 13
MAX_ZONES = 16


class Field(object):
    """One setting of a record: ``key`` (topic/unique id), Home Assistant
    ``component``, display ``label``, byte ``offset`` and kind-specific
    options."""

    def __init__(self, key, component, label, offset, kind, enabled=False, **opts):
        self.key, self.component, self.label = key, component, label
        self.offset, self.kind, self.enabled, self.opts = offset, kind, enabled, opts


def _period_fields():
    return [
        Field("active_from", "select", "Active from", 16, "period_mode"),
        Field("active_from_time", "text", "Active from time", 16, "period_time"),
        Field("active_to", "select", "Active to", 18, "period_mode"),
        Field("active_to_time", "text", "Active to time", 18, "period_time"),
    ]


OUTLET_SETTINGS = [
    Field("name", "text", "Name", 0, "name"),
    Field("auto_off", "number", "Auto off", 12, "int", enabled=True, max=99, unit="s"),
    Field("device_state", "switch", "Device state", 14, "onoff"),
    Field("reaction_duration", "number", "Reaction duration", 15, "int", max=99, unit="min"),
    Field("off_on_doorbell", "switch", "Off on doorbell", FLAGS_OFFSET, "flag", bit=0x01),
    Field("off_on_phone", "switch", "Off on phone", FLAGS_OFFSET, "flag", bit=0x02),
    Field("sun_reaction", "switch", "Sunrise/sunset reaction", FLAGS_OFFSET, "flag", bit=0x04),
    Field("pulse_zone", "select", "Pulse zone", ZONE_OFFSET, "zone"),
] + _period_fields()

LAMP_SETTINGS = [
    Field("name", "text", "Name", 0, "name"),
    Field("deterrence_delay", "number", "Deterrence delay", 12, "int", max=99, unit="min"),
    Field("intensity", "number", "Intensity", 14, "int", max=100, unit="%"),
    Field("duration", "number", "Duration", 15, "int", enabled=True, max=99, unit="min"),
    Field("blink_on_doorbell", "switch", "Blink on doorbell", FLAGS_OFFSET, "flag", bit=0x01),
    Field("blink_on_phone", "switch", "Blink on phone", FLAGS_OFFSET, "flag", bit=0x02),
    Field("sun_reaction", "switch", "Sunrise/sunset reaction", FLAGS_OFFSET, "flag", bit=0x04),
    Field("soft", "switch", "Soft on/off", FLAGS_OFFSET, "flag", bit=0x08),
    Field("blink_on_intrusion", "switch", "Blink on intrusion", FLAGS_OFFSET, "flag", bit=0x10),
    Field("pulse_zone", "select", "Pulse zone", ZONE_OFFSET, "zone"),
] + _period_fields()

# Cardio2e entity type -> (topic slug, table base, record count, fields)
TABLES = {
    "R": ("switch", OUTLETS_BASE, OUTLETS_COUNT, OUTLET_SETTINGS),
    "L": ("light", LAMPS_BASE, LAMPS_COUNT, LAMP_SETTINGS),
}
_SLUG_TO_TYPE = {slug: etype for etype, (slug, _, _, _) in TABLES.items()}


def record_address(entity_type, entity_id):
    _, base, _, _ = TABLES[entity_type]
    return base + (entity_id - 1) * RECORD_SIZE


def _bcd(value):
    return (value >> 4) * 10 + (value & 0x0F)


def _to_bcd(value):
    return ((value // 10) << 4) | (value % 10)


def zone_options(zone_names):
    """Options of a pulse zone select: "None", then "<n> <name>"."""
    return ["None"] + ["%d %s" % (n, zone_names.get(n) or "Zone %d" % n)
                       for n in range(1, MAX_ZONES + 1)]


def decode(field, record, zone_names):
    """State string of ``field`` in a 21-byte ``record``."""
    value = record[field.offset]
    if field.kind == "name":
        return record[:NAME_SIZE].split(b"\0", 1)[0].decode("latin-1").rstrip()
    if field.kind == "int":
        return str(value)
    if field.kind == "onoff":
        return "ON" if value else "OFF"
    if field.kind == "flag":
        return "ON" if value & field.opts["bit"] else "OFF"
    if field.kind == "zone":
        options = zone_options(zone_names)
        return options[value] if value < len(options) else str(value)
    if field.kind == "period_mode":
        for label, code in PERIOD_MODES[1:]:
            if value == code:
                return label
        return "Time"
    if field.kind == "period_time":
        if value >= 24:
            return NO_TIME
        return "%02d:%02d" % (value, _bcd(record[field.offset + 1]))
    raise ValueError("unknown field kind %s" % field.kind)


def encode(field, record, payload, zone_names):
    """Return a copy of ``record`` with ``field`` set from the MQTT
    ``payload``; raises ValueError for an invalid value."""
    new = bytearray(record)
    off = field.offset
    if field.kind == "name":
        if not NAME_RE.match(payload):
            raise ValueError("a name has 1 to %d printable ASCII characters" % NAME_LENGTH)
        new[0:NAME_SIZE] = payload.ljust(NAME_LENGTH).encode("ascii") + b"\0"
    elif field.kind == "int":
        try:
            value = int(float(payload))
        except ValueError:
            raise ValueError("not a number: %r" % payload)
        if not 0 <= value <= field.opts["max"]:
            raise ValueError("%d is outside 0..%d" % (value, field.opts["max"]))
        new[off] = value
    elif field.kind in ("onoff", "flag"):
        if payload not in ("ON", "OFF"):
            raise ValueError("expected ON or OFF, got %r" % payload)
        if field.kind == "onoff":
            new[off] = 1 if payload == "ON" else 0
        elif payload == "ON":
            new[off] |= field.opts["bit"]
        else:
            new[off] &= ~field.opts["bit"] & 0xFF
    elif field.kind == "zone":
        options = zone_options(zone_names)
        if payload not in options:
            raise ValueError("unknown zone %r" % payload)
        new[off] = options.index(payload)
    elif field.kind == "period_mode":
        codes = dict(PERIOD_MODES)
        if payload not in codes:
            raise ValueError("unknown period mode %r" % payload)
        if codes[payload] is not None:
            new[off] = codes[payload]
        elif new[off] >= 24:
            new[off] = 0                        # back to a time: 00:mm
    elif field.kind == "period_time":
        match = TIME_RE.match(payload)
        if not match:
            raise ValueError("expected HH:MM, got %r" % payload)
        new[off] = int(match.group(1))
        new[off + 1] = _to_bcd(int(match.group(2)))
    else:
        raise ValueError("unknown field kind %s" % field.kind)
    return bytes(new)


def state_topic(entity_type, entity_id, key):
    return "%s/%s/%d/%s" % (TOPIC_ROOT, TABLES[entity_type][0], entity_id, key)


def _discovery(entity_type, entity_id, entity_name, field, zone_names, prefix):
    slug = TABLES[entity_type][0]
    object_id = "cardio2e_cfg_%s_%d_%s" % (slug, entity_id, field.key)
    topic = state_topic(entity_type, entity_id, field.key)
    payload = {
        "name": "%s %s" % (entity_name, field.label),
        "unique_id": object_id,
        "state_topic": topic,
        "command_topic": topic + "/set",
        "entity_category": "config",
        "enabled_by_default": field.enabled,
        "qos": 1,
        "retain": False,
        "device": DEVICE_INFO[entity_type],
        **_availability_block(),
    }
    if field.component == "number":
        payload.update({"min": 0, "max": field.opts["max"], "step": 1, "mode": "box",
                        "unit_of_measurement": field.opts["unit"]})
    elif field.component == "switch":
        payload.update({"payload_on": "ON", "payload_off": "OFF"})
    elif field.component == "select":
        payload["options"] = (zone_options(zone_names) if field.kind == "zone"
                              else [label for label, _ in PERIOD_MODES])
    elif field.kind == "name":
        payload.update({"min": 1, "max": NAME_LENGTH, "pattern": NAME_RE.pattern})
    else:
        payload.update({"min": 4, "max": 5,
                        "pattern": r"^(([01]?\d|2[0-3]):[0-5]\d|--:--)$"})
    return "%s/%s/%s/config" % (prefix, field.component, object_id), payload


def _parse_command(topic):
    """``cardio2e/config/<slug>/<id>/<key>/set`` -> (type, id, field)."""
    parts = topic.split("/")
    if len(parts) != 6 or parts[5] != "set" or parts[2] not in _SLUG_TO_TYPE or not parts[3].isdigit():
        raise ValueError("unexpected settings topic %s" % topic)
    entity_type, entity_id = _SLUG_TO_TYPE[parts[2]], int(parts[3])
    fields = {f.key: f for f in TABLES[entity_type][3]}
    if parts[4] not in fields or not 1 <= entity_id <= TABLES[entity_type][2]:
        raise ValueError("unexpected settings topic %s" % topic)
    return entity_type, entity_id, fields[parts[4]]


class SettingsManager(object):
    """Publishes the settings entities and serves their commands from a
    single worker thread."""

    def __init__(self, memory_service, mqtt_client, config, app_state):
        self._memory = memory_service
        self._mqtt = mqtt_client
        self._config = config
        self._app_state = app_state
        self._prefix = config.ha_discover_prefix
        self._queue = queue.Queue()
        self._records = {}            # (type, id) -> 21-byte record
        self._thread = None

    # -- public API (any thread)

    def start(self):
        """Publish the entities and read the records in the background."""
        self._thread = threading.Thread(target=self._worker, daemon=True, name="cardio2e-settings")
        self._thread.start()
        self.request_reload()

    def request_reload(self):
        self._queue.put(("reload",))

    def handle_command(self, topic, payload):
        if topic == RELOAD_TOPIC:
            self.request_reload()
            return
        try:
            entity_type, entity_id, field = _parse_command(topic)
        except ValueError as e:
            _LOGGER.warning("%s", e)
            return
        self._queue.put(("set", entity_type, entity_id, field, payload))

    # -- worker

    def _worker(self):
        while True:
            task = self._queue.get()
            if task is None:
                return
            try:
                if task[0] == "reload":
                    self._reload()
                else:
                    self._set(*task[1:])
            except Exception as e:
                _LOGGER.error("Settings %s failed: %s", task[0], e)
                if task[0] == "set":
                    self._publish_record(task[1], task[2])   # undo the UI change

    def _known(self, entity_type):
        _, _, count, _ = TABLES[entity_type]
        return [i for i in self._app_state.get_known_entity_ids(entity_type) if 1 <= i <= count]

    def _zone_names(self):
        return {n: self._app_state.get_entity_name("Z", n) for n in range(1, MAX_ZONES + 1)}

    def _entity_name(self, entity_type, entity_id):
        return self._app_state.get_entity_name(entity_type, entity_id) or "%s %d" % (
            "Switch" if entity_type == "R" else "Light", entity_id)

    def _reload(self):
        self._publish_discovery()
        started = time.monotonic()
        count = 0
        for entity_type in TABLES:
            ids = self._known(entity_type)
            if not ids:
                continue
            first, last = min(ids), max(ids)
            start = record_address(entity_type, first)
            while True:
                try:
                    with self._memory.exclusive(LOCK_TIMEOUT) as conn:
                        data = read_memory(conn, start, (last - first + 1) * RECORD_SIZE)
                    break
                except RuntimeError as e:
                    _LOGGER.info("Settings reload waiting: %s", e)
                    time.sleep(10)
            for entity_id in ids:
                off = (entity_id - first) * RECORD_SIZE
                record = data[off:off + RECORD_SIZE]
                self._records[(entity_type, entity_id)] = record
                # A name changed on the touch panel or in PCCardio
                name = decode(TABLES[entity_type][3][0], record, {})
                if name and name != self._app_state.get_entity_name(entity_type, entity_id):
                    self._rename(entity_type, entity_id, name)
                self._publish_record(entity_type, entity_id)
                count += 1
        _LOGGER.info("Settings of %d outlets/lamps read in %.0fs.", count, time.monotonic() - started)

    def _set(self, entity_type, entity_id, field, payload):
        if (entity_type, entity_id) not in self._records:
            raise RuntimeError("%s %d is not loaded yet" % (entity_type, entity_id))
        addr = record_address(entity_type, entity_id)
        with self._memory.exclusive(LOCK_TIMEOUT) as conn:
            current = read_memory(conn, addr, RECORD_SIZE)
            wanted = encode(field, current, payload, self._zone_names())
            changed = [i for i in range(RECORD_SIZE) if current[i] != wanted[i]]
            if changed:
                lo, hi = changed[0], changed[-1] + 1
                write_memory(conn, addr + lo, wanted[lo:hi])
        self._records[(entity_type, entity_id)] = wanted
        _LOGGER.info("Setting %s of %s %d set to %r (%d byte(s) written).",
                     field.key, entity_type, entity_id, payload, len(changed))
        if field.kind == "name" and changed:
            self._rename(entity_type, entity_id, decode(field, wanted, {}))
        self._publish_record(entity_type, entity_id)

    def _rename(self, entity_type, entity_id, name):
        """Propagate a new controller name to Home Assistant."""
        self._app_state.set_entity_name(entity_type, entity_id, name)
        publish_config(self._mqtt, entity_id, name, entity_type, self._config)
        self._publish_discovery_of(entity_type, entity_id)

    # -- publishing

    def _publish_discovery(self):
        for entity_type in TABLES:
            for entity_id in self._known(entity_type):
                self._publish_discovery_of(entity_type, entity_id)
        # One button on each device; either reloads both tables
        for entity_type, (slug, _, _, _) in TABLES.items():
            self._mqtt.publish(_reload_topic(self._prefix, slug), json.dumps({
                "name": "Reload configuration",
                "unique_id": "cardio2e_cfg_%s_reload" % slug,
                "command_topic": RELOAD_TOPIC,
                "payload_press": "PRESS",
                "entity_category": "config",
                "device": DEVICE_INFO[entity_type],
                **_availability_block(),
            }), retain=True)

    def _publish_discovery_of(self, entity_type, entity_id):
        name = self._entity_name(entity_type, entity_id)
        zones = self._zone_names()
        for field in TABLES[entity_type][3]:
            topic, payload = _discovery(entity_type, entity_id, name, field, zones, self._prefix)
            self._mqtt.publish(topic, json.dumps(payload), retain=True)

    def _publish_record(self, entity_type, entity_id):
        record = self._records.get((entity_type, entity_id))
        if record is None:
            return
        zones = self._zone_names()
        for field in TABLES[entity_type][3]:
            self._mqtt.publish(state_topic(entity_type, entity_id, field.key),
                               decode(field, record, zones), retain=True)


def _reload_topic(prefix, slug):
    return "%s/button/cardio2e_cfg_%s_reload/config" % (prefix, slug)


def remove_entities(mqtt_client, config, app_state):
    """Clear the discovery of every settings entity (``config_entities``
    turned off), so Home Assistant removes them."""
    prefix = config.ha_discover_prefix
    for entity_type, (slug, _, count, fields) in TABLES.items():
        for entity_id in app_state.get_known_entity_ids(entity_type):
            for field in fields:
                object_id = "cardio2e_cfg_%s_%d_%s" % (slug, entity_id, field.key)
                mqtt_client.publish("%s/%s/%s/config" % (prefix, field.component, object_id), "", retain=True)
        mqtt_client.publish(_reload_topic(prefix, slug), "", retain=True)
