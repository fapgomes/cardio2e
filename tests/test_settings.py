"""Tests for the outlet/lamp settings config entities."""

import json
import time

import pytest

import cardio2e_modules.cardio2e_memory as mem
import cardio2e_modules.cardio2e_serial as cs
import cardio2e_modules.cardio2e_settings as st
from cardio2e_modules.cardio2e_config import AppConfig

from test_memory import FakeCardio

# Records as read from the controller (backup of 2026-10-12)
PORTAOEX = bytes.fromhex("506f7274616f457800101000020001050000000000")   # outlet 9
SUITE = bytes.fromhex("537569746520202000060618320064050000000000")      # lamp 1
ZONES = {1: "Sala", 2: "Entrada", 4: "Escritorio"}


def _field(fields, key):
    return next(f for f in fields if f.key == key)


def _outlet(key):
    return _field(st.OUTLET_SETTINGS, key)


def _lamp(key):
    return _field(st.LAMP_SETTINGS, key)


class TestDecode:
    def test_outlet_record(self):
        states = {f.key: st.decode(f, PORTAOEX, ZONES) for f in st.OUTLET_SETTINGS}
        assert states["name"] == "PortaoEx"
        assert states["auto_off"] == "2"
        assert states["device_state"] == "ON"
        assert states["reaction_duration"] == "5"
        assert states["off_on_doorbell"] == states["sun_reaction"] == "OFF"
        assert states["pulse_zone"] == "None"
        assert states["active_from"] == "Time" and states["active_from_time"] == "00:00"

    def test_lamp_record(self):
        states = {f.key: st.decode(f, SUITE, ZONES) for f in st.LAMP_SETTINGS}
        assert states["name"] == "Suite"                    # padding stripped
        assert states["deterrence_delay"] == "50"
        assert states["intensity"] == "100"
        assert states["duration"] == "5"
        assert states["soft"] == states["blink_on_intrusion"] == "ON"
        assert states["blink_on_doorbell"] == states["blink_on_phone"] == "OFF"

    def test_zone_and_sun_period(self):
        rec = bytearray(SUITE)
        rec[13] = 4
        rec[16], rec[18], rec[19] = 0xE0, 0x06, 0x30
        assert st.decode(_lamp("pulse_zone"), rec, ZONES) == "4 Escritorio"
        assert st.decode(_lamp("active_from"), rec, ZONES) == "Sunset"
        assert st.decode(_lamp("active_from_time"), rec, ZONES) == st.NO_TIME
        assert st.decode(_lamp("active_to_time"), rec, ZONES) == "06:30"


class TestEncode:
    def test_name_is_padded_and_terminated(self):
        rec = st.encode(_outlet("name"), PORTAOEX, "Portao", ZONES)
        assert rec[:9] == b"Portao  \0" and rec[9:] == PORTAOEX[9:]

    @pytest.mark.parametrize("bad", ["", "TooLongName", "Portão"])
    def test_bad_names(self, bad):
        with pytest.raises(ValueError):
            st.encode(_outlet("name"), PORTAOEX, bad, ZONES)

    def test_number_range(self):
        assert st.encode(_outlet("auto_off"), PORTAOEX, "5", ZONES)[12] == 5
        assert st.encode(_outlet("auto_off"), PORTAOEX, "7.0", ZONES)[12] == 7
        for bad in ("100", "-1", "x"):
            with pytest.raises(ValueError):
                st.encode(_outlet("auto_off"), PORTAOEX, bad, ZONES)

    def test_flag_changes_only_its_bit(self):
        rec = st.encode(_lamp("blink_on_phone"), SUITE, "ON", ZONES)
        assert rec[11] == 0x18 | 0x02
        assert st.encode(_lamp("soft"), SUITE, "OFF", ZONES)[11] == 0x10

    def test_zone(self):
        assert st.encode(_lamp("pulse_zone"), SUITE, "2 Entrada", ZONES)[13] == 2
        assert st.encode(_lamp("pulse_zone"), SUITE, "None", ZONES)[13] == 0
        with pytest.raises(ValueError):
            st.encode(_lamp("pulse_zone"), SUITE, "2 Nowhere", ZONES)

    def test_period(self):
        rec = st.encode(_lamp("active_from"), SUITE, "Sunrise", ZONES)
        assert rec[16] == 0xD0
        rec = st.encode(_lamp("active_from_time"), rec, "19:45", ZONES)
        assert rec[16:18] == bytes([19, 0x45])
        rec = st.encode(_lamp("active_to"), bytes(rec[:18]) + bytes([0xE0, 0x15, 0]), "Time", ZONES)
        assert rec[18:20] == bytes([0, 0x15])
        with pytest.raises(ValueError):
            st.encode(_lamp("active_to_time"), SUITE, "24:00", ZONES)


class TestDiscovery:
    def test_payloads(self):
        topic, payload = st._discovery("R", 9, "PortaoEx", _outlet("auto_off"), ZONES, "homeassistant")
        assert topic == "homeassistant/number/cardio2e_cfg_switch_9_auto_off/config"
        assert payload["name"] == "PortaoEx Auto off"
        assert payload["entity_category"] == "config" and payload["enabled_by_default"] is True
        assert payload["state_topic"] == "cardio2e/config/switch/9/auto_off"
        assert payload["command_topic"] == "cardio2e/config/switch/9/auto_off/set"
        assert (payload["min"], payload["max"], payload["unit_of_measurement"]) == (0, 99, "s")
        _, payload = st._discovery("L", 1, "Suite", _lamp("pulse_zone"), ZONES, "homeassistant")
        assert payload["enabled_by_default"] is False
        assert payload["options"][:3] == ["None", "1 Sala", "2 Entrada"]

    def test_only_auto_off_and_duration_are_enabled(self):
        enabled = [(t, f.key) for t, (_, _, _, fields) in st.TABLES.items() for f in fields if f.enabled]
        assert sorted(enabled) == [("L", "duration"), ("R", "auto_off")]

    @pytest.mark.parametrize("topic", [
        "cardio2e/config/switch/9/nope/set",
        "cardio2e/config/switch/41/auto_off/set",
        "cardio2e/config/cover/1/auto_off/set",
        "cardio2e/config/switch/9/auto_off",
    ])
    def test_bad_command_topics(self, topic):
        with pytest.raises(ValueError):
            st._parse_command(topic)


def _wait(condition, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def setup(monkeypatch, tmp_path, mqtt, app_state):
    monkeypatch.setattr(mem, "REQUEST_TIMEOUT", 0.2)
    monkeypatch.setattr(cs, "BULK_QUIET_PERIOD", 0)
    monkeypatch.setattr(cs, "BULK_COMMAND_PAUSE", 0)
    monkeypatch.setattr(cs, "MEMORY_MODE_EXIT", 0)
    monkeypatch.setattr(st, "LOCK_TIMEOUT", 0.2)
    cs._reader_active.set()
    image = bytearray(mem.MEMORY_SIZE)
    outlet9 = st.record_address("R", 9)
    lamp1 = st.record_address("L", 1)
    image[outlet9:outlet9 + 21] = PORTAOEX
    image[lamp1:lamp1 + 21] = SUITE
    conn = FakeCardio(bytes(image))
    service = mem.MemoryService(str(tmp_path))
    service.set_serial_conn(conn)
    app_state.set_entity_name("R", 9, "PortaoEx")
    app_state.set_entity_name("L", 1, "Suite")
    manager = st.SettingsManager(service, mqtt, AppConfig(), app_state)
    manager.start()
    assert _wait(lambda: mqtt.payload_for("cardio2e/config/light/1/name") == "Suite")
    yield conn, service, manager
    manager._queue.put(None)
    cs._reader_active.clear()


class TestSettingsManager:
    def test_load_publishes_discovery_and_states(self, setup, mqtt):
        assert mqtt.payload_for("cardio2e/config/switch/9/auto_off") == "2"
        assert mqtt.payload_for("cardio2e/config/light/1/soft") == "ON"
        config = json.loads(mqtt.payload_for("homeassistant/number/cardio2e_cfg_switch_9_auto_off/config"))
        assert config["name"] == "PortaoEx Auto off"
        for slug, device in (("switch", "Cardio2e Switches"), ("light", "Cardio2e Lights")):
            button = json.loads(mqtt.payload_for("homeassistant/button/cardio2e_cfg_%s_reload/config" % slug))
            assert button["device"]["name"] == device
            assert button["command_topic"] == st.RELOAD_TOPIC

    def test_change_writes_only_that_byte(self, setup, mqtt):
        conn, _, manager = setup
        before = len(conn.commands)
        manager.handle_command("cardio2e/config/switch/9/auto_off/set", "5")
        assert _wait(lambda: mqtt.payload_for("cardio2e/config/switch/9/auto_off") == "5")
        addr = st.record_address("R", 9)
        assert conn.mem[addr + 12] == 5
        writes = [c for c in conn.commands[before:] if c.startswith("@S EN")]
        assert writes == ["@S EN05%02X" % mem.checksum([5])]

    def test_rename_updates_home_assistant(self, setup, mqtt, app_state):
        conn, _, manager = setup
        manager.handle_command("cardio2e/config/switch/9/name/set", "Portao")
        assert _wait(lambda: mqtt.payload_for("cardio2e/config/switch/9/name") == "Portao")
        addr = st.record_address("R", 9)
        assert bytes(conn.mem[addr:addr + 9]) == b"Portao  \0"
        assert app_state.get_entity_name("R", 9) == "Portao"
        main = json.loads(mqtt.payload_for("homeassistant/switch/cardio2e_switch_9/config"))
        assert main["name"] == "Portao"
        cfg = json.loads(mqtt.payload_for("homeassistant/number/cardio2e_cfg_switch_9_auto_off/config"))
        assert cfg["name"] == "Portao Auto off"

    def test_invalid_value_restores_the_state(self, setup, mqtt):
        _, _, manager = setup
        published = len(mqtt.messages)
        manager.handle_command("cardio2e/config/switch/9/auto_off/set", "500")
        assert _wait(lambda: any(t == "cardio2e/config/switch/9/auto_off" and p == "2"
                                 for t, p, _ in mqtt.messages[published:]))

    def test_change_refused_while_a_job_holds_the_memory(self, setup, mqtt):
        conn, service, manager = setup
        published = len(mqtt.messages)
        with service._op_lock:
            manager.handle_command("cardio2e/config/switch/9/auto_off/set", "7")
            assert _wait(lambda: any(t == "cardio2e/config/switch/9/auto_off" and p == "2"
                                     for t, p, _ in mqtt.messages[published:]))
        assert conn.mem[st.record_address("R", 9) + 12] == 2

    def test_restore_triggers_a_reload(self, setup, mqtt):
        conn, service, manager = setup
        conn.mem[st.record_address("R", 9) + 12] = 9      # changed by a restore
        service.add_restore_listener(manager.request_reload)
        for callback in service._restore_listeners:
            callback()
        assert _wait(lambda: mqtt.payload_for("cardio2e/config/switch/9/auto_off") == "9")


def test_remove_entities(mqtt, app_state):
    app_state.set_entity_name("R", 9, "PortaoEx")
    st.remove_entities(mqtt, AppConfig(), app_state)
    assert mqtt.payload_for("homeassistant/number/cardio2e_cfg_switch_9_auto_off/config") == ""
    assert mqtt.payload_for("homeassistant/button/cardio2e_cfg_switch_reload/config") == ""
    assert mqtt.payload_for("homeassistant/button/cardio2e_cfg_light_reload/config") == ""
