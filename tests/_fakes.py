"""Shared test doubles: recording MQTT client, fake serial, paho stub.

These let the test suite run without the real ``paho`` / hardware: handlers
only call ``mqtt_client.publish(...)`` and ``serial_conn.write(...)``, so
lightweight recorders are enough to assert behaviour.
"""

import sys
import types


class RecordingMqttClient:
    """Records every publish() call as (topic, payload, retain)."""

    def __init__(self):
        self.messages = []

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.messages.append((topic, payload, retain))

    def payload_for(self, topic):
        """Return the most recent payload published to ``topic`` (or None)."""
        for t, p, _ in reversed(self.messages):
            if t == topic:
                return p
        return None

    def topics(self):
        return [t for t, _, _ in self.messages]


class FakeSerial:
    """Minimal pyserial stand-in: records writes, replays canned reads."""

    def __init__(self, to_read=b""):
        self.written = []
        self._read_buffer = to_read
        self.is_open = True

    def write(self, data):
        self.written.append(data)
        return len(data)

    def flush(self):
        pass

    @property
    def in_waiting(self):
        return len(self._read_buffer)

    def read(self, n):
        chunk = self._read_buffer[:n]
        self._read_buffer = self._read_buffer[n:]
        return chunk

    def last_written_str(self):
        return self.written[-1].decode() if self.written else None

    def written_str(self):
        return [w.decode() for w in self.written]


def install_paho_stub(with_callback_api_version=True):
    """Install a fake ``paho.mqtt.client`` into sys.modules.

    ``with_callback_api_version=True`` mimics paho-mqtt 2.x (exposes
    ``CallbackAPIVersion`` and ``ReasonCode``); False mimics 1.x.
    Returns the stub client module.
    """
    paho = types.ModuleType("paho")
    paho_mqtt = types.ModuleType("paho.mqtt")
    client_mod = types.ModuleType("paho.mqtt.client")

    class _StubClient:
        def __init__(self, *args, **kwargs):
            self.ctor_args = args
            self._userdata = None
            self.subscriptions = []
            self.published = []
            self.will = None
            self.credentials = None
            self.loop_started = False
            self.connected_to = None
            self.on_connect = None
            self.on_disconnect = None
            self.on_message = None

        def will_set(self, topic, payload, qos=0, retain=False):
            self.will = (topic, payload, qos, retain)

        def username_pw_set(self, username, password=None):
            self.credentials = (username, password)

        def user_data_set(self, data):
            self._userdata = data

        def user_data_get(self):
            return self._userdata

        def connect(self, host, port, keepalive):
            self.connected_to = (host, port, keepalive)

        def loop_start(self):
            self.loop_started = True

        def subscribe(self, topic):
            self.subscriptions.append(topic)

        def publish(self, topic, payload=None, qos=0, retain=False):
            self.published.append((topic, payload, qos, retain))

    client_mod.Client = _StubClient

    class ReasonCode:
        def __init__(self, value):
            self.value = value

        @property
        def is_failure(self):
            return self.value != 0

        def __str__(self):
            return "Success" if self.value == 0 else "Failure(%d)" % self.value

    client_mod.ReasonCode = ReasonCode

    if with_callback_api_version:
        class CallbackAPIVersion:
            VERSION1 = 1
            VERSION2 = 2

        client_mod.CallbackAPIVersion = CallbackAPIVersion

    paho.mqtt = paho_mqtt
    paho_mqtt.client = client_mod
    sys.modules["paho"] = paho
    sys.modules["paho.mqtt"] = paho_mqtt
    sys.modules["paho.mqtt.client"] = client_mod
    return client_mod
