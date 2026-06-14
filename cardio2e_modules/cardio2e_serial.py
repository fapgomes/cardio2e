"""RS-232 serial communication layer for cardio2e.

All serial read/write operations go through a global lock to prevent
contention between the listener thread and MQTT callback thread.
"""

import logging
import queue
import threading
import time

from .cardio2e_constants import (
    CARDIO2E_TERMINATOR,
    HVAC_MODE_TO_CODE,
    FAN_STATE_TO_CODE,
)

_LOGGER = logging.getLogger(__name__)

# Global serial lock - prevents interleaved reads/writes from multiple threads
_serial_lock = threading.Lock()

# Minimum interval (seconds) between consecutive RS-232 commands.
# The Cardio2e controller drops commands that arrive too close together.
_MIN_COMMAND_INTERVAL = 0.15
_last_command_time = 0.0

# Pending query coordination: list of (predicate, queue). The reader thread
# delivers a matching line to the first waiting request; everything else is
# dispatched as a spontaneous update.
_pending_lock = threading.Lock()
_pending = []  # list of (predicate, queue.Queue)

# Set while the SerialReader thread owns the port (steady state). When clear
# (bootstrap), queries read the port directly.
_reader_active = threading.Event()


def _register(predicate):
    """Register a pending request; returns the queue the response lands on."""
    q = queue.Queue(maxsize=1)
    with _pending_lock:
        _pending.append((predicate, q))
    return q


def _unregister(q):
    with _pending_lock:
        _pending[:] = [(p, qq) for (p, qq) in _pending if qq is not q]


def _deliver_to_pending(parts, raw_line):
    """If a pending request matches `parts`, hand it `raw_line`. Returns True
    if the line was consumed by a pending request (and must not be dispatched)."""
    with _pending_lock:
        for i, (predicate, q) in enumerate(_pending):
            try:
                matched = predicate(parts)
            except Exception:
                matched = False
            if matched:
                del _pending[i]
                q.put(raw_line)
                return True
    return False


def _write(serial_conn, command, log_command=None):
    """Single throttled write point for the RS-232 bus.

    Serializes writes across threads and enforces the minimum inter-command
    interval. Raises on I/O error (callers decide how to report it).
    """
    global _last_command_time
    if log_command is None:
        log_command = command
    with _serial_lock:
        elapsed = time.monotonic() - _last_command_time
        if elapsed < _MIN_COMMAND_INTERVAL:
            time.sleep(_MIN_COMMAND_INTERVAL - elapsed)
        _LOGGER.info("Sending command to RS-232: %s", log_command)
        serial_conn.write(command.encode())
        serial_conn.flush()
        _last_command_time = time.monotonic()


def send_command(serial_conn, entity_type, entity_id_or_value, state=None,
                 heating_setpoint=None, cooling_setpoint=None,
                 fan_state=None, mode=None):
    """
    Send a command to the RS-232 bus.
    For HVAC commands, all setpoint/fan/mode params are required.

    ``entity_id_or_value`` is normally the entity id (light, switch, ...),
    but for the date command (entity_type "D") it carries the timestamp
    payload instead, since both map to the same third field of ``@S``.
    Returns True on success, False on error.
    """
    if entity_type == "H":
        if heating_setpoint is None or cooling_setpoint is None or fan_state is None or mode is None:
            _LOGGER.error("Missing parameters for HVAC command: heating_setpoint, cooling_setpoint, fan_state, and mode are required.")
            return False

        fan_state_code = FAN_STATE_TO_CODE.get(fan_state, "S")
        mode_code = HVAC_MODE_TO_CODE.get(mode, "O")

        command = f"@S H {entity_id_or_value} {heating_setpoint} {cooling_setpoint} {fan_state_code} {mode_code}{CARDIO2E_TERMINATOR}"
    else:
        if state is None:
            command = f"@S {entity_type} {entity_id_or_value}{CARDIO2E_TERMINATOR}"
        else:
            command = f"@S {entity_type} {entity_id_or_value} {state}{CARDIO2E_TERMINATOR}"

    # Redact alarm code from logs: security commands are "A <code>" or "D <code>"
    if entity_type == "S" and state:
        state_parts = state.split(maxsplit=1)
        if len(state_parts) > 1:
            log_command = f"@S {entity_type} {entity_id_or_value} {state_parts[0]} ****{CARDIO2E_TERMINATOR}"
        else:
            log_command = command
    else:
        log_command = command

    try:
        _write(serial_conn, command, log_command)
        return True
    except Exception as e:
        _LOGGER.error("Error sending command to RS-232: %s", e)
        return False


def _direct_request(serial_conn, command, predicate, timeout):
    """Bootstrap-mode request: write, then read the port directly until a line
    matches `predicate`. Non-matching lines are discarded (legacy behaviour).
    Returns the matched raw line or None."""
    _write(serial_conn, command)
    with _serial_lock:
        start_time = time.time()
        buffer = ""
        while time.time() - start_time < timeout:
            waiting = serial_conn.in_waiting
            if waiting > 0:
                buffer += serial_conn.read(waiting).decode(errors="ignore")
                while "\r" in buffer or "\n" in buffer:
                    cr = buffer.find("\r")
                    lf = buffer.find("\n")
                    pos = lf if cr == -1 else cr if lf == -1 else min(cr, lf)
                    line = buffer[:pos].strip()
                    buffer = buffer[pos + 1:].lstrip("\r\n")
                    if line and predicate(line.split()):
                        return line
            else:
                time.sleep(0.005)
    return None


def _coordinated_request(serial_conn, command, predicate, timeout):
    """Steady-state request: register a pending entry, write, and wait for the
    reader thread to deliver a matching line. Returns the raw line or None."""
    q = _register(predicate)
    try:
        _write(serial_conn, command)
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            return None
    finally:
        _unregister(q)


def _send_and_match(serial_conn, command, predicate, timeout, max_retries):
    """Send `command` and return the first line whose parts match `predicate`,
    using the coordinated path when the reader owns the port, otherwise a direct
    read. Retries up to `max_retries`. Returns the raw line or None."""
    for _ in range(max_retries):
        try:
            if _reader_active.is_set():
                line = _coordinated_request(serial_conn, command, predicate, timeout)
            else:
                line = _direct_request(serial_conn, command, predicate, timeout)
        except Exception as e:
            _LOGGER.error("Error during serial request %r: %s", command.strip(), e)
            line = None
        if line is not None:
            return line
    return None


def query_name(serial_conn, entity_id, entity_type, max_retries=3, timeout=10):
    """
    Query the name of an entity via RS-232.
    Returns the entity name (str) or None on failure.
    """
    command = f"@G N {entity_type} {entity_id}{CARDIO2E_TERMINATOR}"
    expected_prefix = f"@I N {entity_type}"

    def predicate(parts):
        return len(parts) >= 3 and parts[0] == "@I" and parts[1] == "N" and parts[2] == entity_type

    line = _send_and_match(serial_conn, command, predicate, timeout, max_retries)
    if line is None:
        _LOGGER.warning("Could not get entity name %s %s after %d attempts.", entity_type, entity_id, max_retries)
        return None
    name_part = line.split(expected_prefix, 1)[-1].strip()
    return name_part.split("@")[0].strip()


def query_state(serial_conn, entity_id, entity_type, timeout=0.5, max_retries=5):
    """
    Query the state of an entity via RS-232.
    Returns the raw message parts list on success, or None on failure.
    """
    command = f"@G {entity_type} 1{CARDIO2E_TERMINATOR}" if entity_type == "Z" else f"@G {entity_type} {entity_id}{CARDIO2E_TERMINATOR}"

    if entity_type in ("Z", "B"):
        def predicate(parts):
            return len(parts) >= 2 and parts[0] == "@I" and parts[1] == entity_type
    else:
        def predicate(parts):
            return (len(parts) >= 3 and parts[0] == "@I"
                    and parts[1] == entity_type and parts[2] == str(entity_id))

    line = _send_and_match(serial_conn, command, predicate, timeout, max_retries)
    if line is None:
        _LOGGER.warning("Could not get state for entity %s %s after %d attempts.", entity_type, entity_id, max_retries)
        return None
    return line.split()


def login(serial_conn, password, max_retries=5, timeout=10, post_ack_timeout=15):
    """
    Perform login via RS-232.
    Waits for @A P acknowledgment, then continues reading all @I state
    messages until no more data arrives for 1 second.
    Returns the raw response string on success, or None on failure.
    """
    command = f"@S P I {password}{CARDIO2E_TERMINATOR}"
    success_response_prefix = "@A P"
    attempts = 0

    _LOGGER.info("Logging into cardio2e (usually takes 10 seconds)...")

    while attempts < max_retries:
        try:
            _write(serial_conn, command, log_command=f"@S P I ****{CARDIO2E_TERMINATOR}")
            with _serial_lock:
                _LOGGER.debug("Login command sent.")

                start_time = time.time()
                buffer = ""
                ack_received = False

                # Phase 1: wait for @A P acknowledgment
                while time.time() - start_time < timeout:
                    waiting = serial_conn.in_waiting
                    if waiting > 0:
                        buffer += serial_conn.read(waiting).decode(errors="ignore")
                        if success_response_prefix in buffer:
                            ack_received = True
                            _LOGGER.info("Login ACK received. Reading state messages...")
                            break
                    else:
                        time.sleep(0.01)

                if not ack_received:
                    if buffer:
                        _LOGGER.warning("Login response did not contain success prefix. Got: %r", buffer[:200])
                    attempts += 1
                    _LOGGER.debug("Attempt %d failed for cardio2e login. Trying again.", attempts)
                    continue

                # Phase 2: keep reading @I messages until 1s of silence
                last_data_time = time.time()
                phase2_start = time.time()
                while time.time() - phase2_start < post_ack_timeout:
                    waiting = serial_conn.in_waiting
                    if waiting > 0:
                        buffer += serial_conn.read(waiting).decode(errors="ignore")
                        last_data_time = time.time()
                    else:
                        # No data - check if 1 second of silence has passed
                        if time.time() - last_data_time > 1.0:
                            break
                        time.sleep(0.01)

                _LOGGER.info("Login successful with response length: %d", len(buffer))
                return buffer

        except Exception as e:
            _LOGGER.error("Error during cardio2e login attempt %d: %s", attempts + 1, e)
            attempts += 1

    _LOGGER.warning("Cardio2e login failed after %d attempts.", max_retries)
    return None


def logout(serial_conn):
    """Perform logout via RS-232."""
    command = f"@S P O{CARDIO2E_TERMINATOR}"
    try:
        _write(serial_conn, command)
        _LOGGER.info("Logout command sent; no response required.")
        return True
    except Exception as e:
        _LOGGER.error("Error during cardio2e logout: %s", e)
        return False


def send_date(serial_conn, time_command):
    """Send the current date/time to cardio2e."""
    return send_command(serial_conn, "D", time_command)


def _split_messages(received_message):
    """Split one raw RS-232 frame into (msg, parts) pairs.

    Mirrors the legacy listener splitting: unescape ``#015``, split on ``@``,
    then on ``\\r``. Yields ('@...' line, parts list)."""
    received_message = received_message.replace('#015', '\r')
    pieces = []
    for part in received_message.split('@'):
        pieces.extend(part.split('\r'))
    for piece in pieces:
        if not piece:
            continue
        msg = '@' + piece.strip()
        yield msg, msg.split()


class SerialReader(threading.Thread):
    """Owns all reads from the serial port. Parses lines and either fulfils a
    pending query or hands the line to ``on_message`` for dispatch."""

    def __init__(self, serial_conn, on_message):
        super().__init__(daemon=True, name="cardio2e-serial-reader")
        self._serial = serial_conn
        self._on_message = on_message
        self._stop = threading.Event()
        self._buffer = ""

    def stop(self):
        self._stop.set()

    def _process_buffer(self):
        while "\r" in self._buffer or "\n" in self._buffer:
            cr_pos = self._buffer.find("\r")
            lf_pos = self._buffer.find("\n")
            pos = lf_pos if cr_pos == -1 else cr_pos if lf_pos == -1 else min(cr_pos, lf_pos)
            received_message = self._buffer[:pos].strip()
            rest = self._buffer[pos + 1:]
            if rest and rest[0] in ("\r", "\n"):
                rest = rest[1:]
            self._buffer = rest
            if not received_message:
                continue
            for msg, parts in _split_messages(received_message):
                if _deliver_to_pending(parts, msg):
                    continue
                self._on_message(msg, parts)

    def run(self):
        _reader_active.set()
        try:
            while not self._stop.is_set():
                if not self._serial.is_open:
                    break
                try:
                    waiting = self._serial.in_waiting
                    raw = self._serial.read(waiting).decode(errors="ignore") if waiting > 0 else ""
                    if not raw:
                        time.sleep(0.01)
                        continue
                    self._buffer += raw
                    self._process_buffer()
                except Exception as e:
                    _LOGGER.error("Serial reader error: %s", e)
                    break
        finally:
            _reader_active.clear()
            _LOGGER.info("Serial reader stopped.")
