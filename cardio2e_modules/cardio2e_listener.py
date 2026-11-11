"""Serial listener loop and message dispatcher for cardio2e."""

import datetime
import json
import logging
import time

from .cardio2e_serial import (
    send_date, send_command, query_state, SerialReader, reader_active, pending_count,
    bulk_session_active, last_rx_time,
)
from . import (
    cardio2e_errors,
    cardio2e_lights,
    cardio2e_switches,
    cardio2e_covers,
    cardio2e_hvac,
    cardio2e_security,
    cardio2e_zones,
)
from .cardio2e_constants import (
    HVAC_CODE_TO_MODE,
    FAN_CODE_TO_STATE,
    SECURITY_CODE_TO_STATE,
    SWITCH_CODE_TO_STATE,
    AVAILABILITY_TOPIC,
    PAYLOAD_AVAILABLE,
    PAYLOAD_NOT_AVAILABLE,
    DEVICE_INFO,
)

HEARTBEAT_INTERVAL = 30  # seconds

# Grace period for the @I state broadcast that normally follows an @A ack.
# If it doesn't arrive (lost/corrupted on the wire), the state is re-queried.
ACK_FOLLOWUP_DELAY = 2.0  # seconds

# Time allowed for the @A ack of an @S command. The controller normally acks
# within 0.2s; a command still unacked after this is considered lost on the
# wire and re-sent once (see _run_due_command_retries).
COMMAND_ACK_TIMEOUT = 1.0  # seconds

# A NACK naming an entity we sent an @S command to within this many seconds
# is taken as a NACK to that command (see _hold_nack_report).
NACK_REPORT_COMMAND_AGE = 5.0  # seconds

_LOGGER = logging.getLogger(__name__)


def _run_due_command_retries(serial_conn, app_state, now=None):
    """Re-send, once, every light/relay/HVAC @S command whose @A ack did not
    arrive within ``COMMAND_ACK_TIMEOUT``.

    The Cardio2e mirrors physical key presses onto its RS-232 output and
    sometimes splices that ``@S`` echo into the middle of another frame. A
    command of ours sent during such a burst is garbled and never acked, so
    the requested action silently does not happen. Only lights (L), relays
    (R) and HVAC (H) are retried: their commands set an absolute state, so a
    duplicate is harmless. Covers (any ``@S C`` stops a moving cover),
    scenes and security are never retried. The retry itself is not tracked,
    so a command is sent at most twice. An HVAC command is stored as the
    keyword arguments of ``send_command`` (setpoints, fan, mode).
    """
    if now is None:
        now = time.monotonic()
    for entity_type, entity_id, value in app_state.pop_due_command_retries(now, COMMAND_ACK_TIMEOUT):
        _LOGGER.warning(
            "No @A ack within %.1fs of @S %s %d %s; re-sending once.",
            COMMAND_ACK_TIMEOUT, entity_type, entity_id, value,
        )
        if entity_type == "H":
            send_command(serial_conn, entity_type, entity_id, **value)
        else:
            send_command(serial_conn, entity_type, entity_id, value)


def _schedule_ack_verification(app_state, entity_type, entity_id):
    """Queue a check that an @I update follows this entity's @A ack.

    The controller acknowledges an action with ``@A <type> <id>`` and then
    broadcasts the new state as ``@I <type> <id> <value>``. When that @I is
    lost or corrupted on the RS-232 line, the published state goes stale until
    the next periodic sync (hours). The check is queued in ``AppState`` and
    run by the housekeeping loop (see ``_run_due_ack_verifications``), so a
    burst of acks costs no threads. Only lights (L), relays (R) and HVAC (H)
    are verified: ``@G C`` makes the controller re-drive the cover motor, so
    covers must never be re-queried.
    """
    now = time.monotonic()
    app_state.schedule_ack_check(entity_type, entity_id, now, now + ACK_FOLLOWUP_DELAY)


def _schedule_nack_verification(app_state, entity_type, entity_id):
    """Queue a state re-query for an entity whose @S command got a @N.

    A NACK to one of our commands means the frame reached the controller
    garbled and nothing happened. HVAC state is published optimistically when
    the command is sent, so Home Assistant would keep showing the new value
    while the controller kept the old one. The check is due only after the
    single retry has had time to be acked and followed by its @I
    (``COMMAND_ACK_TIMEOUT`` + ``ACK_FOLLOWUP_DELAY``): if the retry went
    through there is nothing to fix and nothing is queried. Same L/R/H scope
    as the ack verification; covers are never re-queried.
    """
    if entity_type not in ("L", "R", "H"):
        return
    now = time.monotonic()
    app_state.schedule_ack_check(
        entity_type, entity_id, now, now + COMMAND_ACK_TIMEOUT + ACK_FOLLOWUP_DELAY
    )


def _command_confirmed_by_update(app_state, entity_type, entity_id, message_parts):
    """True if an @I update shows the state our last command to the entity
    asked for. Called after the update was processed, so for HVAC the zone
    state in ``app_state.hvac_states`` is already the reported one."""
    last = app_state.last_command(entity_type, entity_id)
    if last is None:
        return False
    value = last[0]
    try:
        if entity_type == "L":
            return int(message_parts[3]) == int(value)
        if entity_type == "R":
            return message_parts[3] == value
        if entity_type == "H":
            with app_state.lock:
                state = dict(app_state.hvac_states.get(entity_id, {}))
            return cardio2e_hvac.command_matches_state(value, state)
    except (ValueError, IndexError, TypeError):
        return False
    return False


def _hold_nack_report(app_state, entity_type, entity_id, error_msg, now=None):
    """Hold back the error report of a NACK to one of our own recent L/R/H
    commands. Returns False (report now) for any other NACK.

    A busy line (e.g. a burst of zone updates) garbles one copy of a command
    while another copy, or the retry, goes through: the controller NACKs the
    bad frame and then reports the requested state. Reporting such a NACK
    leaves a scary error on the dashboard for an action that worked. The
    report is decided once the retry and its @I have had time to arrive
    (see ``_run_due_nack_reports``)."""
    if entity_type not in ("L", "R", "H"):
        return False
    last = app_state.last_command(entity_type, entity_id)
    if now is None:
        now = time.monotonic()
    if last is None or now - last[1] > NACK_REPORT_COMMAND_AGE:
        return False
    app_state.schedule_nack_report(
        entity_type, entity_id, error_msg, now, now + COMMAND_ACK_TIMEOUT + ACK_FOLLOWUP_DELAY
    )
    return True


def _run_due_nack_reports(mqtt_client, app_state, now=None):
    """Report the held NACKs whose command was not confirmed after all; only
    log the ones the controller resolved (ack or @I with the requested
    state)."""
    if now is None:
        now = time.monotonic()
    for entity_type, entity_id, error_msg in app_state.pop_due_nack_reports(now):
        if app_state.command_confirmed(entity_type, entity_id):
            _LOGGER.warning(
                "NACK for %s %d resolved: the controller reached the requested state; not reported (%s).",
                entity_type, entity_id, error_msg,
            )
            continue
        cardio2e_errors.report_error_state(mqtt_client, error_msg)


def _record_update_confirmation(app_state, entity_type, message_parts):
    """Mark the last command of an entity as confirmed when its @I update
    shows the requested state."""
    entity_id = int(message_parts[2])
    if _command_confirmed_by_update(app_state, entity_type, entity_id, message_parts):
        app_state.record_command_confirmation(entity_type, entity_id)


def _run_due_ack_verifications(serial_conn, mqtt_client, config, app_state, now=None):
    """Run the queued ack checks that are due: re-query the state of every
    entity whose @A ack was not followed by an @I update."""
    if now is None:
        now = time.monotonic()
    for entity_type, entity_id, ack_time in app_state.pop_due_ack_checks(now):
        last_update = app_state.last_entity_update(entity_type, entity_id)
        if last_update is not None and last_update >= ack_time:
            continue
        _LOGGER.warning(
            "No @I update for %s %d after its @S command was answered; re-querying state.",
            entity_type, entity_id,
        )
        state = _get_entity_state(serial_conn, mqtt_client, entity_id, entity_type, config, app_state)
        _LOGGER.info("Re-queried state for %s %d: %s", entity_type, entity_id, state)


def _sync_all_entities(serial_conn, mqtt_client, config, app_state):
    """Re-query state of all known entities and republish to MQTT.

    Each query first waits for a quiet serial line, so it does not collide
    with a spontaneous @I update (see ``SYNC_QUIET_PERIOD``).

    Covers are republished from the cached state instead of querying
    the Cardio2e via RS-232, because the ``@G C`` query causes the
    controller to re-issue the position command to the motor, making
    the cover physically move.
    """
    _LOGGER.info("Starting periodic entity sync...")
    count = 0

    # Z (zones): query with ID 1 (returns all zones)
    _get_entity_state(serial_conn, mqtt_client, 1, "Z", config, app_state, quiet=True)
    count += 1

    # B (bypass): republish from cache — the controller rejects @G B 1 (@N B 2)
    bypass = app_state.bypass_states
    if bypass:
        cardio2e_zones.publish_bypass_states(mqtt_client, bypass)
        count += 1

    # S: security, always ID 1
    _get_entity_state(serial_conn, mqtt_client, 1, "S", config, app_state, quiet=True)
    count += 1

    # L, R, H, T: iterate known IDs and query via RS-232. Temperature (T)
    # readings have no ids of their own: they belong to the HVAC (H) entities,
    # but not every HVAC zone has a sensor; the controller rejects the others
    # as out of range and they are skipped from then on.
    for entity_type, id_source in (("L", "L"), ("R", "R"), ("H", "H"), ("T", "H")):
        entity_ids = app_state.get_known_entity_ids(id_source)
        for entity_id in entity_ids:
            if entity_type == "T" and app_state.is_entity_absent("T", entity_id):
                continue
            _get_entity_state(serial_conn, mqtt_client, entity_id, entity_type, config, app_state, quiet=True)
            count += 1

    # C (covers): republish from cache only — do NOT query RS-232
    for entity_id in app_state.get_known_entity_ids("C"):
        cached = app_state.get_entity_state("C", entity_id)
        if cached is not None:
            mqtt_client.publish(f"cardio2e/cover/state/{entity_id}", cached, retain=True)
            _LOGGER.debug("Cover %d sync: republished cached state %s", entity_id, cached)
        else:
            _LOGGER.warning("Cover %d sync: no cached state, skipping.", entity_id)
        count += 1

    _LOGGER.info("Periodic entity sync complete: %d entities synced.", count)


def _controller_alive(serial_conn):
    """Probe a controller that has been silent for ``watchdog_interval``
    seconds with a harmless query (``@G S 1``). Any answer, even a NACK,
    means it is there; nothing is published."""
    nacks = []

    def on_nack(parts):
        nacks.append(parts)
        return True

    reply = query_state(serial_conn, 1, "S", quiet=True, on_nack=on_nack)
    return reply is not None or bool(nacks)


def _publish_heartbeat(mqtt_client, app_state):
    """Publish heartbeat and diagnostics to MQTT."""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    mqtt_client.publish("cardio2e/heartbeat", timestamp, retain=False)

    diag = app_state.get_diagnostics()
    diag["timestamp"] = timestamp
    diag["reader_active"] = reader_active()
    diag["pending_queries"] = pending_count()
    mqtt_client.publish("cardio2e/diagnostics/state", json.dumps(diag), retain=True)
    _LOGGER.debug("Heartbeat published: %s", timestamp)


def _diagnostic_entity_config(domain, object_id, name, value_template, extra, discovery_prefix):
    """Build an autodiscovery config for a diagnostic entity that reads a field
    from the shared diagnostics JSON."""
    config_topic = f"{discovery_prefix}/{domain}/{object_id}/config"
    payload = {
        "name": name,
        "unique_id": object_id,
        "state_topic": "cardio2e/diagnostics/state",
        "value_template": value_template,
        "qos": 1,
        "entity_category": "diagnostic",
        "availability_topic": AVAILABILITY_TOPIC,
        "payload_available": PAYLOAD_AVAILABLE,
        "payload_not_available": PAYLOAD_NOT_AVAILABLE,
        "device": DEVICE_INFO["errors"],
    }
    payload.update(extra)
    return config_topic, payload


def _publish_diagnostics_autodiscovery(mqtt_client, discovery_prefix="homeassistant"):
    """Publish autodiscovery config for the diagnostics sensor and the
    individual diagnostic entities derived from the diagnostics JSON."""
    config_topic = f"{discovery_prefix}/sensor/cardio2e_diagnostics/config"
    config_payload = {
        "name": "Cardio2e Diagnostics",
        "unique_id": "cardio2e_diagnostics",
        "state_topic": "cardio2e/diagnostics/state",
        "icon": "mdi:heart-pulse",
        "qos": 1,
        "retain": True,
        "value_template": "{{ value_json.uptime_seconds }}",
        "unit_of_measurement": "s",
        "json_attributes_topic": "cardio2e/diagnostics/state",
        "availability_topic": AVAILABILITY_TOPIC,
        "payload_available": PAYLOAD_AVAILABLE,
        "payload_not_available": PAYLOAD_NOT_AVAILABLE,
        "device": DEVICE_INFO["errors"],
    }
    mqtt_client.publish(config_topic, json.dumps(config_payload), retain=True)

    # Individual diagnostic entities (read from the same diagnostics JSON)
    entities = [
        _diagnostic_entity_config(
            "sensor", "cardio2e_seconds_since_last_message",
            "Cardio2e Seconds Since Last Message",
            "{{ value_json.seconds_since_last_message }}",
            {"unit_of_measurement": "s", "device_class": "duration",
             "state_class": "measurement", "icon": "mdi:timer-sand"},
            discovery_prefix,
        ),
        _diagnostic_entity_config(
            "sensor", "cardio2e_pending_queries", "Cardio2e Pending Queries",
            "{{ value_json.pending_queries }}",
            {"state_class": "measurement", "icon": "mdi:tray-full"},
            discovery_prefix,
        ),
        _diagnostic_entity_config(
            "sensor", "cardio2e_reconnects", "Cardio2e Reconnects",
            "{{ value_json.reconnects }}",
            {"state_class": "total_increasing", "icon": "mdi:restart"},
            discovery_prefix,
        ),
        _diagnostic_entity_config(
            "binary_sensor", "cardio2e_reader", "Cardio2e Reader",
            "{{ 'ON' if value_json.reader_active else 'OFF' }}",
            {"payload_on": "ON", "payload_off": "OFF", "device_class": "running"},
            discovery_prefix,
        ),
    ]
    for topic, payload in entities:
        mqtt_client.publish(topic, json.dumps(payload), retain=True)

    _LOGGER.info("Published autodiscovery config for diagnostics sensors.")


def listen_for_updates(serial_conn, mqtt_client, config, app_state, shutdown_event=None):
    """Run the housekeeping loop while a SerialReader thread owns the port.

    Returns when the connection is lost (reader stopped) or when
    ``shutdown_event`` is set, so the caller can reconnect or shut down.
    """
    # Due at once: a controller that lost power has the wrong time, and this
    # interval restarts on every serial reconnect
    last_time_sent = time.monotonic() - config.update_date_interval
    last_heartbeat = time.monotonic()
    last_sync = time.monotonic()
    last_probe = time.monotonic()
    was_bulk = False

    # Publish diagnostics autodiscovery on start
    _publish_diagnostics_autodiscovery(mqtt_client, config.ha_discover_prefix)

    def on_message(msg, message_parts):
        _LOGGER.info("Processing individual message: %s", msg)
        app_state.record_message()
        _dispatch_message(serial_conn, mqtt_client, config, app_state, msg, message_parts)

    reader = SerialReader(serial_conn, on_message)
    reader.start()

    try:
        while serial_conn.is_open and reader.is_alive():
            if shutdown_event is not None and shutdown_event.is_set():
                break
            now = time.monotonic()

            # Send date periodically
            if (now - last_time_sent) >= config.update_date_interval:
                time_command = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
                send_date(serial_conn, time_command)
                cardio2e_errors.report_error_state(mqtt_client, "No errors.")
                _LOGGER.info("Sent time command to cardio2e: %s", time_command)
                last_time_sent = now

            # Re-send light/relay commands that got no @A ack
            _run_due_command_retries(serial_conn, app_state, now)

            # Report NACKs to our commands that the controller did not resolve
            _run_due_nack_reports(mqtt_client, app_state, now)

            # Re-query entities whose @A ack was not followed by an @I update
            _run_due_ack_verifications(serial_conn, mqtt_client, config, app_state, now)

            # The controller reports no @I updates while it serves memory
            # requests: re-sync everything once a memory session is over (or
            # paused after a command, see BULK_COMMAND_PAUSE)
            bulk = bulk_session_active(now)
            if was_bulk and not bulk:
                _LOGGER.info("Memory access idle; re-syncing all entities.")
                _sync_all_entities(serial_conn, mqtt_client, config, app_state)
                last_sync = now
            was_bulk = bulk

            # States published while the MQTT broker was away were lost
            if app_state.pop_sync_request():
                _LOGGER.info("MQTT broker reconnected; re-syncing all entities.")
                _sync_all_entities(serial_conn, mqtt_client, config, app_state)
                last_sync = now

            # Watchdog: a controller switched off or restarted leaves the
            # port open and silent. Not during a memory session, which has
            # its own traffic and timeouts.
            if config.watchdog_interval > 0 and not bulk:
                if now - max(last_rx_time(), last_probe) >= config.watchdog_interval:
                    if not _controller_alive(serial_conn):
                        _LOGGER.error("No answer from cardio2e after %.0fs of silence; "
                                      "connection considered lost.", config.watchdog_interval)
                        break
                    last_probe = time.monotonic()

            # Publish heartbeat + diagnostics periodically
            if (now - last_heartbeat) >= HEARTBEAT_INTERVAL:
                _publish_heartbeat(mqtt_client, app_state)
                last_heartbeat = now

            # Periodic entity sync (queries are served by the reader, so this
            # no longer blocks message reception)
            if config.sync_interval > 0 and (now - last_sync) >= config.sync_interval:
                _sync_all_entities(serial_conn, mqtt_client, config, app_state)
                last_sync = now

            if shutdown_event is not None:
                if shutdown_event.wait(0.5):
                    break
            else:
                time.sleep(0.5)
    finally:
        reader.stop()
        reader.join(timeout=2)

    _LOGGER.warning("Serial reader stopped; connection considered lost.")


def _dispatch_message(serial_conn, mqtt_client, config, app_state, msg, message_parts):
    """Dispatch a single parsed message to the appropriate handler."""

    # ACK messages (@A)
    if len(message_parts) == 2 and message_parts[0] == "@A":
        if message_parts[1] == "D":
            _LOGGER.info("Cardio date update successfully.")

    elif len(message_parts) == 3 and message_parts[0] == "@A":
        entity_type = message_parts[1]
        entity_id = int(message_parts[2])

        if entity_type == "L":
            _LOGGER.info("OK for action %s", app_state.get_entity_label("light", "L", entity_id))
            app_state.clear_command_retry("L", entity_id)
            app_state.record_command_confirmation("L", entity_id)
            _schedule_ack_verification(app_state, "L", entity_id)
        elif entity_type == "R":
            _LOGGER.info("OK for action %s", app_state.get_entity_label("switch", "R", entity_id))
            app_state.clear_command_retry("R", entity_id)
            app_state.record_command_confirmation("R", entity_id)
            _schedule_ack_verification(app_state, "R", entity_id)
        elif entity_type == "H":
            _LOGGER.info("OK for action %s", app_state.get_entity_label("HVAC", "H", entity_id))
            app_state.clear_command_retry("H", entity_id)
            app_state.record_command_confirmation("H", entity_id)
            _schedule_ack_verification(app_state, "H", entity_id)
        elif entity_type == "C":
            _LOGGER.info("OK for action %s", app_state.get_entity_label("cover", "C", entity_id))
        elif entity_type == "S":
            _LOGGER.info("OK for action %s", app_state.get_entity_label("security", "S", entity_id))
        elif entity_type == "M":
            _LOGGER.info("OK for action %s", app_state.get_entity_label("scenario", "M", entity_id))
        elif entity_type == "B":
            # Bypass set ack. The new state was already published by
            # handle_bypass_command; we do NOT re-query the controller, which
            # rejects @G B 1 (@N B 2) and garbles the stream when flooded.
            _LOGGER.info("OK for bypass set.")
            app_state.confirm_bypass_states()

    # NACK messages (@N)
    elif len(message_parts) >= 3 and message_parts[0] == "@N":
        error_msg = cardio2e_errors.format_error_message(message_parts)
        app_state.increment_errors()
        app_state.set_last_error(error_msg)
        _LOGGER.info("\n#######\nNACK from cardio with transaction %s: %s", msg, error_msg)
        if message_parts[1] == "B":
            # Bypass is never queried, so this is a rejected bypass command
            cardio2e_zones.revert_rejected_bypass(mqtt_client, app_state)
        # "@N <type> <id> <code>" identifies the rejected entity; a truncated
        # "@N <type> <code>" (id lost to garbling) identifies nothing.
        if len(message_parts) >= 4 and message_parts[2].isdigit():
            entity_type, entity_id = message_parts[1], int(message_parts[2])
            _schedule_nack_verification(app_state, entity_type, entity_id)
            if not _hold_nack_report(app_state, entity_type, entity_id, error_msg):
                cardio2e_errors.report_error_state(mqtt_client, error_msg)
        else:
            cardio2e_errors.report_error_state(mqtt_client, error_msg)

    # Info/state update messages (@I)
    elif len(message_parts) >= 4 and message_parts[0] == "@I":
        entity_type = message_parts[1]

        if entity_type == "L":
            app_state.record_entity_update("L", int(message_parts[2]))
            cardio2e_lights.process_update(mqtt_client, message_parts, config, app_state)
            _record_update_confirmation(app_state, "L", message_parts)
        elif entity_type == "R":
            app_state.record_entity_update("R", int(message_parts[2]))
            cardio2e_switches.process_update(mqtt_client, message_parts, app_state)
            _record_update_confirmation(app_state, "R", message_parts)
        elif entity_type == "C":
            cardio2e_covers.process_update(mqtt_client, message_parts, app_state)
        elif entity_type == "H":
            app_state.record_entity_update("H", int(message_parts[2]))
            cardio2e_hvac.process_update(mqtt_client, message_parts, app_state)
            _record_update_confirmation(app_state, "H", message_parts)
        elif entity_type == "T":
            cardio2e_hvac.process_temp_update(mqtt_client, message_parts, app_state)
        elif entity_type == "S":
            cardio2e_security.process_update(mqtt_client, message_parts, app_state)
        elif entity_type == "Z":
            cardio2e_zones.process_zone_update(mqtt_client, message_parts, config, app_state)
        elif entity_type == "B":
            cardio2e_zones.process_bypass_update(mqtt_client, message_parts, app_state)
        else:
            _LOGGER.warning("Response not processed: %s", message_parts)
    else:
        # Typically a fragment of a frame garbled on the wire (e.g. "@CCCC",
        # "@O"): expected noise from the controller, not a bridge failure.
        _LOGGER.warning("Response not processed: %s", message_parts)


def _mark_missing_temperature_sensor(mqtt_client, app_state, entity_id, parts):
    """Handle a NACK to ``@G T <id>``: ``@N T <id> 2`` (out of range) means the
    HVAC zone has no temperature sensor, although the login reports it (as
    0.00). Mark it, so its readings are ignored and the periodic sync stops
    querying it, and clear the zone's current temperature in Home Assistant
    (``None`` is the MQTT climate's "no value" payload; an empty one is
    ignored). Returns True if the NACK was that one (and was logged here)."""
    if parts != ["@N", "T", str(entity_id), "2"]:
        return False
    if app_state.mark_entity_absent("T", entity_id):
        mqtt_client.publish(f"cardio2e/hvac/{entity_id}/state/current_temperature", "None", retain=True)
        _LOGGER.info("Temperature sensor %s does not exist on the controller (%s); "
                     "cleared its temperature and skipping it from now on.", entity_id, " ".join(parts))
    return True


def probe_temperature_sensors(serial_conn, mqtt_client, config, app_state, sensor_ids):
    """Query each temperature sensor once (at login, before its reading is
    published), so the ones the controller rejects are marked absent and
    their 0.00 reading never reaches Home Assistant."""
    for sensor_id in sensor_ids:
        if not app_state.is_entity_absent("T", sensor_id):
            _get_entity_state(serial_conn, mqtt_client, sensor_id, "T", config, app_state)


def _get_entity_state(serial_conn, mqtt_client, entity_id, entity_type, config, app_state, quiet=False):
    """Query entity state and publish to MQTT (used internally by listener).
    ``quiet`` waits for a quiet serial line before querying (periodic sync)."""
    on_nack = None
    if entity_type == "T" and app_state is not None:
        on_nack = lambda parts: _mark_missing_temperature_sensor(mqtt_client, app_state, entity_id, parts)
    message_parts = query_state(serial_conn, entity_id, entity_type, quiet=quiet, on_nack=on_nack)
    if message_parts is None:
        return None
    try:
        return _publish_queried_state(mqtt_client, entity_id, entity_type, config, app_state, message_parts)
    except (ValueError, IndexError):
        # A value garbled on the wire (e.g. "@I L 5 1O0"). Raising here would
        # leave the housekeeping loop and force a serial reconnect.
        _LOGGER.warning("Discarding garbled state reply: %s", " ".join(message_parts))
        return None


def _publish_queried_state(mqtt_client, entity_id, entity_type, config, app_state, message_parts):
    """Publish the state in the reply to a ``@G`` query. Returns the state,
    or None if the reply is not usable."""
    if entity_type == "L" and len(message_parts) >= 4:
        state = int(message_parts[3])
        light_state = "ON" if state > 0 else "OFF"
        mqtt_client.publish(f"cardio2e/light/state/{entity_id}", light_state, retain=True)
        if config is not None and int(entity_id) in config.dimmer_lights:
            mqtt_client.publish(f"cardio2e/light/brightness/{entity_id}", state, retain=True)
        return light_state

    elif entity_type == "R" and len(message_parts) >= 4:
        state = message_parts[3]
        switch_state = SWITCH_CODE_TO_STATE.get(state, "OFF")
        mqtt_client.publish(f"cardio2e/switch/state/{entity_id}", switch_state, retain=True)
        return switch_state

    elif entity_type == "C" and len(message_parts) >= 4:
        state = message_parts[3]
        if not 0 <= int(state) <= 100:      # ValueError: reply discarded
            raise ValueError("cover position out of range")
        app_state.set_entity_state("C", entity_id, state)
        mqtt_client.publish(f"cardio2e/cover/state/{entity_id}", state, retain=True)
        return state

    elif entity_type == "T" and len(message_parts) >= 4:
        state = message_parts[3]
        mqtt_client.publish(f"cardio2e/hvac/{entity_id}/state/current_temperature", state, retain=True)
        return state

    elif entity_type == "H" and len(message_parts) >= 7:
        # The setpoints are stored and sent back with the next command of
        # the zone: a garbled one must not get in (ValueError: reply discarded)
        float(message_parts[3])
        float(message_parts[4])
        topics = {
            "heating_setpoint": message_parts[3],
            "cooling_setpoint": message_parts[4],
            "fan": FAN_CODE_TO_STATE.get(message_parts[5], "off"),
            "mode": HVAC_CODE_TO_MODE.get(message_parts[6], "unknown"),
        }
        with app_state.lock:
            hvac_states = app_state.hvac_states
            for topic_suffix, state in topics.items():
                hvac_states = cardio2e_hvac.update_hvac_state(mqtt_client, hvac_states, int(entity_id), topic_suffix, state)
            app_state.hvac_states = hvac_states
        return True

    elif entity_type == "S" and len(message_parts) >= 4:
        state = SECURITY_CODE_TO_STATE.get(message_parts[3], "unknown")
        mqtt_client.publish(f"cardio2e/alarm/state/{entity_id}", state, retain=True)
        return state

    elif entity_type == "Z" and len(message_parts) >= 4:
        zone_states = message_parts[3]
        if len(message_parts) != 4 or not cardio2e_zones.valid_zone_states(zone_states):
            _LOGGER.warning("Discarding garbled zone state reply: %s", " ".join(message_parts))
            return None
        for zone_id in range(1, len(zone_states) + 1):
            zone_state = cardio2e_zones.interpret_zone_character(zone_states[zone_id - 1], zone_id, config.zones_normal_as_off)
            mqtt_client.publish(f"cardio2e/zone/state/{zone_id}", zone_state, retain=True)
        return zone_states

    elif entity_type == "B" and len(message_parts) >= 4:
        states = message_parts[3]
        if len(message_parts) != 4 or not cardio2e_zones.valid_bypass_states(states):
            _LOGGER.warning("Discarding garbled bypass state reply: %s", " ".join(message_parts))
            return None
        for zone_id in range(1, len(states) + 1):
            bypass_state = cardio2e_zones.interpret_bypass_character(states[zone_id - 1])
            mqtt_client.publish(f"cardio2e/zone/bypass/state/{zone_id}", bypass_state, retain=True)
        return states

    return None
