"""Tests for Home Assistant MQTT autodiscovery payloads."""

import json

from cardio2e_modules import cardio2e_autodiscovery
from cardio2e_modules.cardio2e_config import AppConfig


def _published_config(mqtt, topic):
    raw = mqtt.payload_for(topic)
    return json.loads(raw) if raw is not None else None


class TestLight:
    def test_plain_light(self, mqtt):
        cfg = AppConfig(dimmer_lights=[])
        cardio2e_autodiscovery.publish_config(mqtt, 5, "Kitchen", "L", cfg)
        payload = _published_config(mqtt, "homeassistant/light/cardio2e_5/config")
        assert payload["name"] == "Kitchen"
        assert payload["unique_id"] == "cardio2e_light_5"
        assert "brightness" not in payload

    def test_dimmer_light_has_brightness(self, mqtt):
        cfg = AppConfig(dimmer_lights=[5])
        cardio2e_autodiscovery.publish_config(mqtt, 5, "Kitchen", "L", cfg)
        payload = _published_config(mqtt, "homeassistant/light/cardio2e_5/config")
        assert payload["brightness"] is True
        assert payload["brightness_scale"] == 100


class TestSwitch:
    def test_switch(self, mqtt):
        cardio2e_autodiscovery.publish_config(mqtt, 3, "Pump", "R")
        payload = _published_config(mqtt, "homeassistant/switch/cardio2e_switch_3/config")
        assert payload["unique_id"] == "cardio2e_switch_3"
        assert payload["command_topic"] == "cardio2e/switch/set/3"


class TestCover:
    def test_cover(self, mqtt):
        cardio2e_autodiscovery.publish_config(mqtt, 2, "Blind", "C")
        payload = _published_config(mqtt, "homeassistant/cover/cardio2e_cover_2/config")
        assert payload["position_topic"] == "cardio2e/cover/state/2"
        assert payload["payload_stop"] == "STOP"


class TestHvac:
    def test_hvac_temp_limits(self, mqtt):
        cardio2e_autodiscovery.publish_config(mqtt, 1, "Living", "H")
        payload = _published_config(mqtt, "homeassistant/climate/cardio2e_hvac_1/config")
        assert payload["min_temp"] == 7
        assert payload["max_temp"] == 35
        assert "auto" in payload["modes"]


class TestAlarm:
    def test_alarm(self, mqtt):
        cardio2e_autodiscovery.publish_config(mqtt, 1, "Alarm", "S")
        payload = _published_config(mqtt, "homeassistant/alarm_control_panel/cardio2e_alarm_1/config")
        assert payload["payload_arm_away"] == "armed_away"
        assert payload["payload_disarm"] == "disarmed"


class TestZone:
    def test_zone_publishes_sensor_and_bypass(self, mqtt):
        cardio2e_autodiscovery.publish_config(mqtt, 4, "Hall", "Z")
        sensor = _published_config(mqtt, "homeassistant/binary_sensor/cardio2e_zone_4/config")
        bypass = _published_config(mqtt, "homeassistant/switch/cardio2e_zone_4_bypass/config")
        assert sensor["device_class"] == "motion"
        assert bypass["name"] == "Hall Bypass"


class TestScene:
    def test_scene(self, mqtt):
        cardio2e_autodiscovery.publish_config(mqtt, 7, "Movie", "M")
        payload = _published_config(mqtt, "homeassistant/scene/cardio2e_scene_7/config")
        assert payload["unique_id"] == "cardio2e_scene_7"
        assert payload["command_topic"] == "cardio2e/scene/set/7"
