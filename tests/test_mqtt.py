"""Tests for MQTT setup, routing and paho 1.x/2.x compatibility."""

import importlib
import sys

import pytest

from cardio2e_modules import cardio2e_mqtt
from cardio2e_modules.cardio2e_config import AppConfig, AppState
from cardio2e_modules.cardio2e_constants import AVAILABILITY_TOPIC, PAYLOAD_NOT_AVAILABLE

from _fakes import FakeSerial, install_paho_stub


class _Msg:
    def __init__(self, topic, payload, retain=False):
        self.topic = topic
        self.payload = payload.encode()
        self.retain = retain


def _userdata():
    return {
        "serial_conn": FakeSerial(),
        "config": AppConfig(alarm_code="9999"),
        "app_state": AppState(),
        "get_entity_state_fn": lambda *a: None,
        "init_complete": True,
    }


class TestCreateMqttClient:
    def test_configures_lwt_credentials_and_connects(self):
        cfg = AppConfig()
        client = cardio2e_mqtt.create_mqtt_client(cfg, FakeSerial(), AppState(), lambda *a: None)
        assert client.will == (AVAILABILITY_TOPIC, PAYLOAD_NOT_AVAILABLE, 1, True)
        assert client.credentials == (cfg.mqtt_username, cfg.mqtt_password)
        assert client.connected_to == (cfg.mqtt_address, cfg.mqtt_port, 60)
        assert client.loop_started is True
        assert client._userdata["init_complete"] is False

    def test_uses_callback_api_version2_on_paho2(self):
        # conftest installed a paho-2.x-like stub (has CallbackAPIVersion)
        cfg = AppConfig()
        client = cardio2e_mqtt.create_mqtt_client(cfg, FakeSerial(), AppState(), lambda *a: None)
        assert client.ctor_args == (2,)


class TestSubscribeAfterInit:
    def test_flips_flag_and_subscribes(self):
        cfg = AppConfig()
        client = cardio2e_mqtt.create_mqtt_client(cfg, FakeSerial(), AppState(), lambda *a: None)
        cardio2e_mqtt.subscribe_after_init(client)
        assert client._userdata["init_complete"] is True
        assert len(client.subscriptions) == 8


class TestIsFailure:
    def test_int_codes(self):
        assert cardio2e_mqtt._is_failure(0) is False
        assert cardio2e_mqtt._is_failure(5) is True

    def test_reason_code_objects(self):
        rc = sys.modules["paho.mqtt.client"].ReasonCode
        assert cardio2e_mqtt._is_failure(rc(0)) is False
        assert cardio2e_mqtt._is_failure(rc(0x87)) is True


class TestOnMessageRouting:
    def test_light_set_sends_command(self):
        ud = _userdata()
        cardio2e_mqtt._on_message(None, ud, _Msg("cardio2e/light/set/5", "ON"))
        assert ud["serial_conn"].last_written_str() == "@S L 5 100\r"

    def test_records_last_command(self):
        ud = _userdata()
        cardio2e_mqtt._on_message(None, ud, _Msg("cardio2e/light/set/5", "ON"))
        assert ud["app_state"].get_diagnostics()["last_command"] == "cardio2e/light/set/5 ON"

    def test_retained_is_ignored(self):
        ud = _userdata()
        cardio2e_mqtt._on_message(None, ud, _Msg("cardio2e/light/set/5", "ON", retain=True))
        assert ud["serial_conn"].written == []
        assert ud["app_state"].get_diagnostics()["last_command"] == ""

    def test_scene_numeric_payload_redacted_in_diagnostics(self):
        ud = _userdata()
        cardio2e_mqtt._on_message(None, ud, _Msg("cardio2e/scene/set/3", "12345"))
        # The controller still receives the real code...
        assert ud["serial_conn"].last_written_str() == "@S M 3 12345\r"
        # ...but diagnostics redact it (published to a retained topic)
        assert ud["app_state"].get_diagnostics()["last_command"] == "cardio2e/scene/set/3 ****"

    def test_alarm_set_routes(self):
        ud = _userdata()
        cardio2e_mqtt._on_message(None, ud, _Msg("cardio2e/alarm/set/1", "ARMED_AWAY"))
        assert ud["serial_conn"].last_written_str() == "@S S 1 A 9999\r"


class TestPahoCallbackCompatibility:
    """_on_connect / _on_disconnect must accept both 1.x and 2.x signatures."""

    @pytest.fixture
    def restore_default_stub(self):
        yield
        # Restore the paho-2.x stub and reload so later tests are unaffected
        for m in list(sys.modules):
            if m.startswith("paho"):
                del sys.modules[m]
        install_paho_stub(with_callback_api_version=True)
        importlib.reload(cardio2e_mqtt)

    def _reload(self, with_v2):
        for m in list(sys.modules):
            if m.startswith("paho"):
                del sys.modules[m]
        install_paho_stub(with_callback_api_version=with_v2)
        importlib.reload(cardio2e_mqtt)
        return cardio2e_mqtt

    def test_paho1_signatures(self, restore_default_stub):
        m = self._reload(with_v2=False)
        assert m._PAHO_V2 is False
        client = m.create_mqtt_client(AppConfig(), FakeSerial(), AppState(), lambda *a: None)
        assert client.ctor_args == ()  # legacy constructor, no version arg
        # v1: on_connect(client, userdata, flags, rc); on_disconnect(client, userdata, rc)
        m._on_connect(client, {"init_complete": False}, {}, 0)
        m._on_disconnect(client, {}, 0)
        m._on_disconnect(client, {}, 1)

    def test_paho2_signatures(self, restore_default_stub):
        m = self._reload(with_v2=True)
        assert m._PAHO_V2 is True
        rc = sys.modules["paho.mqtt.client"].ReasonCode
        client = m.create_mqtt_client(AppConfig(), FakeSerial(), AppState(), lambda *a: None)
        assert client.ctor_args == (2,)
        # v2: on_connect(client, userdata, flags, reason_code, properties)
        m._on_connect(client, {"init_complete": False}, {}, rc(0), None)
        # v2: on_disconnect(client, userdata, disconnect_flags, reason_code, properties)
        m._on_disconnect(client, {}, {}, rc(0), None)
        m._on_disconnect(client, {}, {}, rc(0x8B), None)
