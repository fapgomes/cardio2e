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
