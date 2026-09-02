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
