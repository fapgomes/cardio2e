"""Tests for the entry-point module (login failure handling, syslog setup)."""

import logging
import logging.handlers
import threading

import pytest

import cardio2e
from cardio2e_modules.cardio2e_config import AppConfig

from _fakes import FakeSerial, RecordingMqttClient


class TestLoginFailure:
    def test_do_login_and_init_raises_when_login_fails(self, mqtt, serial_conn, app_state, monkeypatch):
        monkeypatch.setattr(cardio2e, "login", lambda conn, pw: None)
        with pytest.raises(cardio2e.LoginFailed):
            cardio2e._do_login_and_init(serial_conn, mqtt, AppConfig(), app_state)

    def test_main_does_not_go_ready_when_login_fails(self, monkeypatch):
        class OneShotEvent(threading.Event):
            # First backoff wait ends the loop, so main() returns.
            def wait(self, timeout=None):
                self.set()
                return True

        monkeypatch.setattr(cardio2e.threading, "Event", OneShotEvent)
        monkeypatch.setattr(cardio2e, "load_config", lambda path: AppConfig())
        monkeypatch.setattr(cardio2e, "_connect_serial", lambda cfg: FakeSerial())
        monkeypatch.setattr(cardio2e, "create_mqtt_client", lambda *a, **k: RecordingMqttClient())
        monkeypatch.setattr(cardio2e, "login", lambda conn, pw: None)
        subscribed, listened = [], []
        monkeypatch.setattr(cardio2e, "subscribe_after_init", lambda c: subscribed.append(c))
        monkeypatch.setattr(cardio2e, "listen_for_updates", lambda *a, **k: listened.append(1))

        cardio2e.main()

        assert subscribed == []
        assert listened == []


class TestLoginTemperatureProbe:
    def test_sensors_probed_before_login_states_are_published(self, mqtt, serial_conn, app_state, monkeypatch):
        # The login reports 0.00 for HVAC zones without a sensor: those are
        # found (and their readings ignored) before the login is parsed
        response = "@A P 1\r@I H 1 20.00 23.00 S H\r@I H 2 20.00 23.00 S H\r@I T 1 21.03 O\r@I T 2 0.00 O\r@I P E\r"
        calls = []
        monkeypatch.setattr(cardio2e, "login", lambda conn, pw: response)
        monkeypatch.setattr(cardio2e, "probe_temperature_sensors",
                            lambda s, m, cfg, st, ids: calls.append(("probe", ids)))
        monkeypatch.setattr(cardio2e, "parse_login_response",
                            lambda resp, m, s, cfg, st: calls.append(("parse", resp)))
        monkeypatch.setattr(cardio2e.cardio2e_covers, "initialize_entity_cover", lambda *a, **k: None)
        monkeypatch.setattr(cardio2e, "get_name", lambda *a, **k: None)
        monkeypatch.setattr(cardio2e.cardio2e_scenarios, "initialize_scenarios", lambda *a, **k: None)

        cardio2e._do_login_and_init(serial_conn, mqtt, AppConfig(), app_state)

        assert calls == [("probe", [1, 2]), ("parse", response)]


class TestSyslog:
    def test_setup_syslog_uses_stdlib_handler(self):
        handler = cardio2e._setup_syslog("127.0.0.1", 5514)
        try:
            assert isinstance(handler, logging.handlers.SysLogHandler)
            assert handler.address == ("127.0.0.1", 5514)
            assert handler.ident == "cardio2e: "
        finally:
            handler.close()


class TestParseLoginResponse:
    def test_force_included_light_is_registered(self, mqtt, serial_conn, app_state):
        cfg = AppConfig(force_include_lights=[46], fetch_light_names=False)
        cardio2e.parse_login_response("@I L 1 0\r", mqtt, serial_conn, cfg, app_state)
        assert app_state.get_known_entity_ids("L") == [1, 46]
        assert mqtt.payload_for("cardio2e/light/state/46") == "OFF"


class TestGetNameCache:
    def test_uses_cached_name_without_querying(self, mqtt, serial_conn, app_state, monkeypatch):
        # On a serial reconnect the names are already known: do not spend
        # seconds per entity asking the controller again.
        queries = []
        monkeypatch.setattr(cardio2e, "query_name", lambda *a, **k: queries.append(a) or "FromWire")
        app_state.set_entity_name("L", 5, "Kitchen")
        name = cardio2e.get_name(serial_conn, 5, "L", mqtt, AppConfig(), app_state)
        assert name == "Kitchen"
        assert queries == []
        assert mqtt.payload_for("cardio2e/light/name/5") == "Kitchen"
        assert mqtt.payload_for("homeassistant/light/cardio2e_5/config") is not None

    def test_queries_when_not_cached(self, mqtt, serial_conn, app_state, monkeypatch):
        monkeypatch.setattr(cardio2e, "query_name", lambda *a, **k: "FromWire")
        assert cardio2e.get_name(serial_conn, 5, "L", mqtt, AppConfig(), app_state) == "FromWire"
        assert app_state.get_entity_name("L", 5) == "FromWire"


class TestSerialReconnectKeepsMqttClient:
    def test_mqtt_client_survives_serial_loss(self, monkeypatch):
        from cardio2e_modules.cardio2e_constants import AVAILABILITY_TOPIC

        class TwoIterationsEvent(threading.Event):
            # First backoff wait: keep going (second iteration). Second: stop.
            def __init__(self):
                super().__init__()
                self.waits = 0

            def wait(self, timeout=None):
                self.waits += 1
                if self.waits >= 2:
                    self.set()
                    return True
                return False

        monkeypatch.setattr(cardio2e.threading, "Event", TwoIterationsEvent)
        monkeypatch.setattr(cardio2e, "load_config", lambda path: AppConfig(ncovers=0, nscenarios=0))
        serials = []

        def connect(cfg):
            s = FakeSerial()
            serials.append(s)
            return s
        monkeypatch.setattr(cardio2e, "_connect_serial", connect)
        created = []
        real_create = cardio2e.create_mqtt_client

        def create(*args):
            c = real_create(*args)
            created.append(c)
            return c
        monkeypatch.setattr(cardio2e, "create_mqtt_client", create)
        monkeypatch.setattr(cardio2e, "login", lambda conn, pw: "@I V C 1\r")
        # listen_for_updates returning without shutdown == serial connection lost
        monkeypatch.setattr(cardio2e, "listen_for_updates", lambda *a, **k: None)

        cardio2e.main()

        assert len(serials) == 2  # serial reopened once
        assert len(created) == 1  # but the MQTT client was reused
        client = created[0]
        # handlers now write to the new serial handle
        assert client.user_data_get()["serial_conn"] is serials[1]
        # commands (re)enabled after each init and suspended on each loss
        assert client.subscriptions.count("cardio2e/light/set/#") == 2
        assert client.unsubscriptions.count("cardio2e/light/set/#") == 2
        availability = [p for t, p, _q, _r in client.published if t == AVAILABILITY_TOPIC]
        assert availability == ["online", "offline", "online", "offline", "offline"]


class TestConfigError:
    def test_main_exits_with_a_message_instead_of_a_traceback(self, monkeypatch, caplog):
        from cardio2e_modules.cardio2e_config import ConfigError

        def bad_config(path):
            raise ConfigError("Invalid value for [cardio2e] ncovers: 'twenty' (expected an integer)")

        monkeypatch.setattr(cardio2e, "load_config", bad_config)
        with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exit_info:
            cardio2e.main()
        assert exit_info.value.code == 1
        assert "[cardio2e] ncovers" in caplog.text


class TestMqttClientRecreated:
    def test_settings_manager_follows_the_new_client(self, monkeypatch):
        class TwoIterationsEvent(threading.Event):
            def __init__(self):
                super().__init__()
                self.waits = 0

            def wait(self, timeout=None):
                self.waits += 1
                if self.waits >= 2:
                    self.set()
                    return True
                return False

        class FakeSettings:
            instances = []

            def __init__(self, memory_service, mqtt_client, config, app_state):
                self.mqtt = mqtt_client
                FakeSettings.instances.append(self)

            def start(self):
                pass

            def request_reload(self):
                pass

            def set_mqtt_client(self, mqtt_client):
                self.mqtt = mqtt_client

        monkeypatch.setattr(cardio2e.threading, "Event", TwoIterationsEvent)
        monkeypatch.setattr(cardio2e, "load_config", lambda path: AppConfig(ncovers=0, nscenarios=0))
        monkeypatch.setattr(cardio2e, "_connect_serial", lambda cfg: FakeSerial())
        monkeypatch.setattr(cardio2e, "SettingsManager", FakeSettings)
        created = []
        real_create = cardio2e.create_mqtt_client

        def create(*args):
            c = real_create(*args)
            if not created:
                # The first client turns out unusable when the serial is lost
                def broken(topic):
                    raise RuntimeError("client is dead")
                c.unsubscribe = broken
            created.append(c)
            return c
        monkeypatch.setattr(cardio2e, "create_mqtt_client", create)
        monkeypatch.setattr(cardio2e, "login", lambda conn, pw: "@I V C 1\r")
        monkeypatch.setattr(cardio2e, "listen_for_updates", lambda *a, **k: None)

        cardio2e.main()

        assert len(created) == 2
        assert len(FakeSettings.instances) == 1
        settings = FakeSettings.instances[0]
        assert settings.mqtt is created[1]
        assert created[1].user_data_get()["settings"] is settings
