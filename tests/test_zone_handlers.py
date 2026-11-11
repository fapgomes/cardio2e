"""Tests for zone/bypass MQTT and serial handlers."""

import pytest

from cardio2e_modules import cardio2e_zones
from cardio2e_modules.cardio2e_config import AppConfig

# Real frames from the production log (2026-11-01 09:06:13): a normal one and
# one garbled on the wire (15 chars, last bytes altered).
NORMAL = "CNCNCCCCCCOOOOCC"
GARBLED = "CNCCCCCCCCOOCCI"
BYPASS = "NY" + "N" * 14


class TestProcessLoginZones:
    def test_publishes_each_zone_state(self, mqtt, serial_conn):
        cfg = AppConfig(fetch_zone_names=False, zones_normal_as_off=[])
        cardio2e_zones.process_login_zones(
            mqtt, "@I Z 1 ONCE" + "C" * 12, serial_conn, cfg, lambda *a: None
        )
        assert mqtt.payload_for("cardio2e/zone/state/1") == "ON"   # O
        assert mqtt.payload_for("cardio2e/zone/state/2") == "ON"   # N
        assert mqtt.payload_for("cardio2e/zone/state/3") == "OFF"  # C
        assert mqtt.payload_for("cardio2e/zone/state/4") == "ERROR"  # E
        assert mqtt.payload_for("cardio2e/zone/state/16") == "OFF"

    @pytest.mark.parametrize("states", [GARBLED, "CCCCCCCCCCOOOOCCC", "CCCCCCCCCCOOOOCX"])
    def test_garbled_states_are_discarded(self, mqtt, serial_conn, states):
        cfg = AppConfig(fetch_zone_names=False, zones_normal_as_off=[11, 12, 13, 14])
        cardio2e_zones.process_login_zones(
            mqtt, f"@I Z 1 {states}", serial_conn, cfg, lambda *a: None
        )
        assert mqtt.messages == []


class TestProcessZoneUpdate:
    def test_publishes_states(self, mqtt, app_state):
        cfg = AppConfig(zones_normal_as_off=[])
        cardio2e_zones.process_zone_update(mqtt, ["@I", "Z", "1", "ON" + "C" * 14], cfg, app_state)
        assert mqtt.payload_for("cardio2e/zone/state/1") == "ON"
        assert mqtt.payload_for("cardio2e/zone/state/2") == "ON"
        assert mqtt.payload_for("cardio2e/zone/state/16") == "OFF"

    def test_garbled_frame_does_not_trigger_inverted_zones(self, mqtt, app_state):
        # Regression (2026-11-01): opening the living-room covers garbled one
        # @I Z frame into 15 chars; the shifted "C"s were published as ON for
        # the inverted zones 13 and 14 (flood sensor), raising a false alarm.
        cfg = AppConfig(zones_normal_as_off=[11, 12, 13, 14])
        cardio2e_zones.process_zone_update(mqtt, ["@I", "Z", "1", GARBLED], cfg, app_state)
        assert mqtt.messages == []

    @pytest.mark.parametrize("parts", [
        ["@I", "Z", "1", "CCCCCCCCCCOOOOCCC"],         # too long
        ["@I", "Z", "1", "CCCCCCCCCCOOOOCX"],          # invalid character
        ["@I", "Z", "1", "CCCCCCCCCCOOOOCC", "junk"],  # trailing garbage
    ])
    def test_malformed_frames_are_discarded(self, mqtt, app_state, parts):
        cfg = AppConfig(zones_normal_as_off=[])
        cardio2e_zones.process_zone_update(mqtt, parts, cfg, app_state)
        assert mqtt.messages == []

    def test_valid_frame_after_garbled_one_is_published(self, mqtt, app_state):
        cfg = AppConfig(zones_normal_as_off=[11, 12, 13, 14])
        cardio2e_zones.process_zone_update(mqtt, ["@I", "Z", "1", GARBLED], cfg, app_state)
        cardio2e_zones.process_zone_update(mqtt, ["@I", "Z", "1", NORMAL], cfg, app_state)
        assert mqtt.payload_for("cardio2e/zone/state/14") == "OFF"
        assert mqtt.payload_for("cardio2e/zone/state/2") == "ON"


class TestProcessLoginBypass:
    def test_sets_state_and_publishes(self, mqtt, app_state):
        cardio2e_zones.process_login_bypass(mqtt, "@I B 1 " + BYPASS, app_state)
        assert app_state.bypass_states == BYPASS
        assert mqtt.payload_for("cardio2e/zone/bypass/state/1") == "OFF"
        assert mqtt.payload_for("cardio2e/zone/bypass/state/2") == "ON"
        assert mqtt.payload_for("cardio2e/zone/bypass/state/16") == "OFF"

    @pytest.mark.parametrize("states", ["NYNNNNNNNNNNNNN", "NYNNNNNNNNNNNNNNN", "NYNNNNNNNNNNNNNI"])
    def test_garbled_states_are_discarded(self, mqtt, app_state, states):
        app_state.bypass_states = "N" * 16
        cardio2e_zones.process_login_bypass(mqtt, f"@I B 1 {states}", app_state)
        assert app_state.bypass_states == "N" * 16
        assert mqtt.messages == []


class TestProcessBypassUpdate:
    def test_publishes(self, mqtt, app_state):
        cardio2e_zones.process_bypass_update(mqtt, ["@I", "B", "1", BYPASS], app_state)
        assert mqtt.payload_for("cardio2e/zone/bypass/state/1") == "OFF"
        assert mqtt.payload_for("cardio2e/zone/bypass/state/2") == "ON"
        assert mqtt.payload_for("cardio2e/zone/bypass/state/16") == "OFF"

    def test_updates_cache(self, mqtt, app_state):
        cardio2e_zones.process_bypass_update(mqtt, ["@I", "B", "1", BYPASS], app_state)
        assert app_state.bypass_states == BYPASS

    @pytest.mark.parametrize("parts", [
        ["@I", "B", "1", "NYNNNNNNNNNNNNN"],           # too short (byte lost)
        ["@I", "B", "1", "NYNNNNNNNNNNNNNNN"],         # too long
        ["@I", "B", "1", "NYNNNNNNNNNNNNNI"],          # invalid character
        ["@I", "B", "1", BYPASS, "junk"],              # trailing garbage
    ])
    def test_garbled_frame_is_discarded(self, mqtt, app_state, parts):
        # The cache is sent back to the controller on the next bypass command,
        # so a garbled frame must neither be published nor cached.
        app_state.bypass_states = "N" * 16
        cardio2e_zones.process_bypass_update(mqtt, parts, app_state)
        assert app_state.bypass_states == "N" * 16
        assert mqtt.messages == []


class TestHandleBypassCommand:
    def test_sets_bit_and_sends(self, serial_conn, mqtt, app_state):
        app_state.bypass_states = "N" * 16
        cardio2e_zones.handle_bypass_command(
            serial_conn, mqtt, "cardio2e/zone/bypass/set/2", "ON", app_state
        )
        written = serial_conn.last_written_str()
        assert written.startswith("@S B 1 ")
        states = written[len("@S B 1 "):].rstrip("\r")
        assert states[1] == "Y"
        assert app_state.bypass_states[1] == "Y"

    def test_publishes_new_state_without_requery(self, serial_conn, mqtt, app_state):
        # Regression: a bypass set must publish the new state directly and must
        # NOT issue a @G B 1 re-query (only the @S B 1 set goes on the wire).
        app_state.bypass_states = "N" * 16
        cardio2e_zones.handle_bypass_command(
            serial_conn, mqtt, "cardio2e/zone/bypass/set/2", "ON", app_state
        )
        assert mqtt.payload_for("cardio2e/zone/bypass/state/2") == "ON"
        assert mqtt.payload_for("cardio2e/zone/bypass/state/1") == "OFF"
        assert all("@G B" not in w for w in serial_conn.written_str())

    def test_invalid_cache_is_not_sent_to_controller(self, serial_conn, mqtt, app_state):
        # A cache with invalid characters must not be written back to the
        # controller as is.
        app_state.bypass_states = "NNNNNNNNNNNNNNNI"
        cardio2e_zones.handle_bypass_command(
            serial_conn, mqtt, "cardio2e/zone/bypass/set/1", "ON", app_state
        )
        written = serial_conn.last_written_str()
        states = written[len("@S B 1 "):].rstrip("\r")
        assert states == "Y" + "N" * 15

    def test_defaults_when_state_missing(self, serial_conn, mqtt, app_state):
        app_state.bypass_states = ""
        cardio2e_zones.handle_bypass_command(
            serial_conn, mqtt, "cardio2e/zone/bypass/set/1", "ON", app_state
        )
        written = serial_conn.last_written_str()
        states = written[len("@S B 1 "):].rstrip("\r")
        assert len(states) == 16
        assert states[0] == "Y"


class TestHandleBypassCommandLocking:
    def test_lock_not_held_during_serial_write(self, serial_conn, mqtt, app_state, monkeypatch):
        import threading
        app_state.bypass_states = "N" * 16
        acquired = []

        def fake_send(conn, etype, eid, state=None, **kwargs):
            # Another thread must be able to take the AppState lock while the
            # (slow, throttled) serial write is in progress.
            def probe():
                got = app_state.lock.acquire(timeout=0.2)
                acquired.append(got)
                if got:
                    app_state.lock.release()
            t = threading.Thread(target=probe)
            t.start()
            t.join()
            return True

        monkeypatch.setattr(cardio2e_zones, "send_command", fake_send)
        cardio2e_zones.handle_bypass_command(
            serial_conn, mqtt, "cardio2e/zone/bypass/set/2", "ON", app_state
        )
        assert acquired == [True]
        assert app_state.bypass_states[1] == "Y"
        assert mqtt.payload_for("cardio2e/zone/bypass/state/2") == "ON"


class TestHandleBypassCommandRange:
    @pytest.mark.parametrize("zone", [0, 17, -1])
    def test_out_of_range_zone_is_rejected(self, serial_conn, mqtt, app_state, zone):
        # 16 zones: anything else must be rejected, not raise (17+) or wrap
        # around to another zone via a negative index (0, -1).
        app_state.bypass_states = "N" * 16
        cardio2e_zones.handle_bypass_command(
            serial_conn, mqtt, f"cardio2e/zone/bypass/set/{zone}", "ON", app_state
        )
        assert serial_conn.written == []
        assert app_state.bypass_states == "N" * 16


class TestBypassCommandRejected:
    """The new bypass is published when the command is sent. When the
    controller rejects it (e.g. ``@N B 1 7``, security armed), the cache and
    Home Assistant must go back to the last state the controller confirmed:
    the bypass cannot be queried and the cache is written back on the next
    command."""

    @staticmethod
    def _dispatch(serial_conn, mqtt, app_state, line):
        from cardio2e_modules import cardio2e_listener
        cardio2e_listener._dispatch_message(serial_conn, mqtt, AppConfig(), app_state, line, line.split())

    def _login(self, mqtt, app_state, states=BYPASS):
        cardio2e_zones.process_login_bypass(mqtt, f"@I B 1 {states}", app_state)

    def _set(self, serial_conn, mqtt, app_state, zone, payload):
        cardio2e_zones.handle_bypass_command(
            serial_conn, mqtt, f"cardio2e/zone/bypass/set/{zone}", payload, app_state)

    def test_nack_reverts_cache_and_published_state(self, mqtt, serial_conn, app_state):
        self._login(mqtt, app_state)
        self._set(serial_conn, mqtt, app_state, 3, "ON")
        assert mqtt.payload_for("cardio2e/zone/bypass/state/3") == "ON"
        self._dispatch(serial_conn, mqtt, app_state, "@N B 1 7")
        assert app_state.bypass_states == BYPASS
        assert mqtt.payload_for("cardio2e/zone/bypass/state/3") == "OFF"
        assert mqtt.payload_for("cardio2e/zone/bypass/state/2") == "ON"

    def test_truncated_nack_also_reverts(self, mqtt, serial_conn, app_state):
        self._login(mqtt, app_state)
        self._set(serial_conn, mqtt, app_state, 3, "ON")
        self._dispatch(serial_conn, mqtt, app_state, "@N B 7")
        assert app_state.bypass_states == BYPASS

    def test_ack_confirms_the_new_state(self, mqtt, serial_conn, app_state):
        self._login(mqtt, app_state)
        self._set(serial_conn, mqtt, app_state, 3, "ON")
        self._dispatch(serial_conn, mqtt, app_state, "@A B 1")
        accepted = app_state.bypass_states
        self._set(serial_conn, mqtt, app_state, 4, "ON")
        self._dispatch(serial_conn, mqtt, app_state, "@N B 1 7")
        assert app_state.bypass_states == accepted
        assert mqtt.payload_for("cardio2e/zone/bypass/state/3") == "ON"
        assert mqtt.payload_for("cardio2e/zone/bypass/state/4") == "OFF"

    def test_update_from_controller_is_the_confirmed_state(self, mqtt, serial_conn, app_state):
        self._login(mqtt, app_state)
        updated = "NNNY" + "N" * 12
        cardio2e_zones.process_bypass_update(mqtt, ["@I", "B", "1", updated], app_state)
        self._set(serial_conn, mqtt, app_state, 3, "ON")
        self._dispatch(serial_conn, mqtt, app_state, "@N B 1 7")
        assert app_state.bypass_states == updated

    def test_nack_without_confirmed_state_keeps_cache(self, mqtt, serial_conn, app_state):
        # Bypass never reported by the controller: nothing to go back to
        self._set(serial_conn, mqtt, app_state, 3, "ON")
        sent = app_state.bypass_states
        self._dispatch(serial_conn, mqtt, app_state, "@N B 1 7")
        assert app_state.bypass_states == sent
