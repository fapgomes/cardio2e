"""RS-232 serial communication layer for cardio2e."""

import logging
import time

from .cardio2e_constants import (
    CARDIO2E_TERMINATOR,
    HVAC_MODE_TO_CODE,
    FAN_STATE_TO_CODE,
)

_LOGGER = logging.getLogger(__name__)


def send_command(serial_conn, entity_type, entity_id, state=None,
                 heating_setpoint=None, cooling_setpoint=None,
                 fan_state=None, mode=None):
    """
    Send a command to the RS-232 bus.
    For HVAC commands, all setpoint/fan/mode params are required.
    Returns True on success, False on error.
    """
    if entity_type == "H":
        if heating_setpoint is None or cooling_setpoint is None or fan_state is None or mode is None:
            _LOGGER.error("Missing parameters for HVAC command: heating_setpoint, cooling_setpoint, fan_state, and mode are required.")
            return False

        fan_state_code = FAN_STATE_TO_CODE.get(fan_state, "S")
        mode_code = HVAC_MODE_TO_CODE.get(mode, "O")

        command = f"@S H {entity_id} {heating_setpoint} {cooling_setpoint} {fan_state_code} {mode_code}{CARDIO2E_TERMINATOR}"
    else:
        if state is None:
            command = f"@S {entity_type} {entity_id}{CARDIO2E_TERMINATOR}"
        else:
            command = f"@S {entity_type} {entity_id} {state}{CARDIO2E_TERMINATOR}"

    try:
        _LOGGER.info("Sending command to RS-232: %s", command)
        serial_conn.write(command.encode())
        serial_conn.flush()
        return True
    except Exception as e:
        _LOGGER.error("Error sending command to RS-232: %s", e)
        return False


def query_name(serial_conn, entity_id, entity_type, max_retries=3, timeout=10):
    """
    Query the name of an entity via RS-232.
    Returns (entity_id_from_response, entity_name) or None on failure.
    """
    command = f"@G N {entity_type} {entity_id}{CARDIO2E_TERMINATOR}"
    attempts = 0

    expected_prefix = f"@I N {entity_type}"

    while attempts < max_retries:
        try:
            serial_conn.write(command.encode())
            _LOGGER.debug("Command sent to get entity name %s %d: %s", entity_type, entity_id, command.strip())

            start_time = time.time()
            buffer = ""

            while time.time() - start_time < timeout:
                waiting = serial_conn.in_waiting
                if waiting > 0:
                    buffer += serial_conn.read(waiting).decode(errors="ignore")
                    # Process complete lines
                    while "\r" in buffer or "\n" in buffer:
                        cr = buffer.find("\r")
                        lf = buffer.find("\n")
                        if cr == -1:
                            pos = lf
                        elif lf == -1:
                            pos = cr
                        else:
                            pos = min(cr, lf)
                        line = buffer[:pos].strip()
                        buffer = buffer[pos + 1:].lstrip("\r\n")

                        if line.startswith(expected_prefix):
                            _LOGGER.debug("Complete message received for entity name %s %d: %s", entity_type, entity_id, line)
                            name_part = line.split(expected_prefix, 1)[-1].strip()
                            entity_name = name_part.split("@")[0].strip()
                            return entity_name
                        elif line:
                            _LOGGER.debug("Message ignored during name search: %s", line)
                else:
                    time.sleep(0.005)

            attempts += 1
            _LOGGER.debug("Attempt %d failed to get the name of entity %s %d. Trying again.", attempts, entity_type, entity_id)

        except Exception as e:
            _LOGGER.error("Error getting entity name %s %d: %s", entity_type, entity_id, e)
            attempts += 1

    _LOGGER.warning("Could not get entity name %s %d after %d attempts.", entity_type, entity_id, max_retries)
    return None


def query_state(serial_conn, entity_id, entity_type, timeout=0.5, max_retries=5):
    """
    Query the state of an entity via RS-232.
    Returns the raw message parts list on success, or None on failure.
    """
    command = f"@G {entity_type} 1{CARDIO2E_TERMINATOR}" if entity_type == "Z" else f"@G {entity_type} {entity_id}{CARDIO2E_TERMINATOR}"
    expected_prefix = f"@I {entity_type} "
    attempts = 0

    while attempts < max_retries:
        try:
            serial_conn.write(command.encode())
            _LOGGER.info("Sent command %s to get entity %s %d state (try %d / %d)", command.strip(), entity_type, entity_id, attempts + 1, max_retries)

            start_time = time.time()
            buffer = ""

            while time.time() - start_time < timeout:
                waiting = serial_conn.in_waiting
                if waiting > 0:
                    buffer += serial_conn.read(waiting).decode(errors="ignore")
                    # Check for complete message (terminated by \r or \n)
                    for terminator in ("\r", "\n"):
                        if terminator in buffer:
                            line, buffer = buffer.split(terminator, 1)
                            line = line.strip()
                            if line.startswith(expected_prefix):
                                _LOGGER.debug("Message received: %s", line)
                                return line.split()
                            elif line:
                                _LOGGER.debug("Message ignored: %s", line)
                else:
                    time.sleep(0.005)

            _LOGGER.warning("Incorrect answer for entity %s %d, attempt %d by %d.", entity_type, entity_id, attempts + 1, max_retries)
            attempts += 1

        except Exception as e:
            _LOGGER.error("Error getting state of entity %s %d: %s", entity_type, entity_id, e)
            attempts += 1

    _LOGGER.warning("Could not get state for entity %s %d after %d attempts.", entity_type, entity_id, max_retries)
    return None


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
            serial_conn.write(command.encode())
            _LOGGER.debug("Login command sent: %s", command.strip())

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
        serial_conn.write(command.encode())
        _LOGGER.info("Logout command sent; no response required.")
        return True
    except Exception as e:
        _LOGGER.error("Error during cardio2e logout: %s", e)
        return False


def send_date(serial_conn, time_command):
    """Send the current date/time to cardio2e."""
    return send_command(serial_conn, "D", time_command)
