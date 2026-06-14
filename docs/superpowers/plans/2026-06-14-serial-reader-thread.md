# Single Serial Reader Thread — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the shared-lock serial model with a single reader thread that owns all reads, a central throttled writer, and request/response coordination for queries — fixing lost spontaneous messages (nº5), blocking sync (nº8) and incomplete throttle (nº9).

**Architecture:** One `SerialReader` thread is the only caller that reads the port; it parses each line and either fulfils a pending query (matched by type+id) or dispatches it to MQTT. All writes go through a throttled `_write()`. Login + initialization stay synchronous and run **before** the reader starts (bootstrap phase); the reader owns reads only in steady state. The main loop stops reading and becomes a housekeeping loop (date/heartbeat/sync), so the sync no longer blocks reception.

**Tech Stack:** Python 3, `threading`, `queue`, pyserial (real port), pytest with `FakeSerial`/`RecordingMqttClient` doubles.

---

## Background facts (verified in the current code)

- `query_name` is only ever called during the **bootstrap** phase (from `process_login` handlers and cover/scenario init). It is never reached from `_dispatch_message` in steady state.
- `query_state` is called in **both** phases: bootstrap (cover init) and steady state (periodic sync, cover-stop, and the `@A B 1` bypass re-publish).
- The only query reachable from `_dispatch_message` is the `@A B 1` case (listener). To avoid the reader thread blocking on its own query, that re-query must run off the reader thread.
- During bootstrap, MQTT command topics are not yet subscribed (`init_complete` is False until `subscribe_after_init`), so no `send_command` runs concurrently — direct reads during bootstrap are safe.
- Existing tests in `tests/test_serial.py` exercise `query_state`/`query_name` with the reader **inactive** (direct mode). They must keep passing unchanged.

## File structure

- **Modify `cardio2e_modules/cardio2e_serial.py`** — add `_write`, the pending-request registry (`_register`/`_unregister`/`_deliver_to_pending`), `_reader_active` event, `_send_and_match`, the `SerialReader` thread, and `_split_messages`. Refactor `send_command`/`query_state`/`query_name`/`login`/`logout`/`send_date` to use `_write` and `_send_and_match`.
- **Modify `cardio2e_modules/cardio2e_listener.py`** — `listen_for_updates` becomes a housekeeping loop that starts/stops the reader; move the `@A B 1` re-query to a daemon thread; remove the in-loop reading and buffer logic.
- **Modify `tests/_fakes.py`** — add `FakeSerial.feed()` for staged reads.
- **Create `tests/test_serial_reader.py`** — unit + integration tests for the registry, reader dispatch, coordinated query, and the nº5 regression.
- **Modify `cardio2e.py`** — version bump (last task).

---

## Task 1: Central throttled writer `_write` (fixes nº9)

**Files:**
- Modify: `cardio2e_modules/cardio2e_serial.py`
- Test: `tests/test_serial.py` (existing) and a new check

- [ ] **Step 1: Write the failing test**

Add to `tests/test_serial.py`:

```python
class TestCentralWrite:
    def test_write_sends_bytes_and_flushes(self, serial_conn):
        from cardio2e_modules.cardio2e_serial import _write
        _write(serial_conn, "@S L 1 100\r")
        assert serial_conn.last_written_str() == "@S L 1 100\r"

    def test_login_uses_central_write(self, serial_conn, monkeypatch):
        import cardio2e_modules.cardio2e_serial as cs
        calls = []
        monkeypatch.setattr(cs, "_write", lambda conn, command, log_command=None: calls.append(command))
        # No ACK in buffer -> login retries and fails fast, but must have written via _write
        cs.login(serial_conn, "12345", max_retries=1, timeout=0.05, post_ack_timeout=0.05)
        assert any(c.startswith("@S P I 12345") for c in calls)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_serial.py::TestCentralWrite -v`
Expected: FAIL (`_write` does not exist).

- [ ] **Step 3: Add `_write` and route writers through it**

In `cardio2e_modules/cardio2e_serial.py`, add after the module globals (below `_last_command_time = 0.0`):

```python
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
```

Replace the `try/...` block at the end of `send_command` with:

```python
    try:
        _write(serial_conn, command, log_command)
        return True
    except Exception as e:
        _LOGGER.error("Error sending command to RS-232: %s", e)
        return False
```

In `logout`, replace the body's write with `_write`:

```python
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
```

In `login`, replace `serial_conn.write(command.encode())` (inside the `with _serial_lock:` block) — remove that single write line and instead call `_write` **before** acquiring the read loop. Change the structure so the lock is only used for reading the response:

```python
    while attempts < max_retries:
        try:
            _write(serial_conn, command, log_command=f"@S P I ****{CARDIO2E_TERMINATOR}")
            with _serial_lock:
                _LOGGER.debug("Login command sent.")
                start_time = time.time()
                buffer = ""
                ack_received = False
                # ... rest of phase 1/2 unchanged ...
```

(Note: this also redacts the password in the login log — keep the `log_command` masked exactly as shown.)

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_serial.py -v`
Expected: PASS (new `TestCentralWrite` plus all existing serial tests, including alarm redaction and `send_date`).

- [ ] **Step 5: Commit**

```bash
git add cardio2e_modules/cardio2e_serial.py tests/test_serial.py
git commit -m "refactor: route all serial writes through a central throttled _write"
```

---

## Task 2: Pending-request registry and delivery

**Files:**
- Modify: `cardio2e_modules/cardio2e_serial.py`
- Create: `tests/test_serial_reader.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_serial_reader.py`:

```python
"""Tests for the serial reader thread, pending-request registry and queries."""

import cardio2e_modules.cardio2e_serial as cs


class TestPendingRegistry:
    def test_deliver_matches_predicate(self):
        q = cs._register(lambda p: p[1] == "L" and p[2] == "5")
        try:
            consumed = cs._deliver_to_pending(["@I", "L", "5", "100"], "@I L 5 100")
            assert consumed is True
            assert q.get_nowait() == "@I L 5 100"
        finally:
            cs._unregister(q)

    def test_deliver_no_match_returns_false(self):
        q = cs._register(lambda p: p[1] == "L" and p[2] == "5")
        try:
            consumed = cs._deliver_to_pending(["@I", "L", "3", "100"], "@I L 3 100")
            assert consumed is False
            assert q.empty()
        finally:
            cs._unregister(q)

    def test_unregister_removes_entry(self):
        q = cs._register(lambda p: True)
        cs._unregister(q)
        assert cs._deliver_to_pending(["@I", "L", "1", "0"], "@I L 1 0") is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_serial_reader.py::TestPendingRegistry -v`
Expected: FAIL (`_register` not defined).

- [ ] **Step 3: Implement the registry**

In `cardio2e_modules/cardio2e_serial.py`, add `import queue` at the top (after `import logging`), and add these globals near the other module state:

```python
# Pending query coordination: list of (predicate, queue). The reader thread
# delivers a matching line to the first waiting request; everything else is
# dispatched as a spontaneous update.
_pending_lock = threading.Lock()
_pending = []  # list of [predicate, queue.Queue]

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
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_serial_reader.py::TestPendingRegistry -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cardio2e_modules/cardio2e_serial.py tests/test_serial_reader.py
git commit -m "feat: add pending-request registry for serial query coordination"
```

---

## Task 3: Unified `_send_and_match` + two-mode queries

**Files:**
- Modify: `cardio2e_modules/cardio2e_serial.py`
- Test: `tests/test_serial.py` (existing direct-mode tests), `tests/test_serial_reader.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_serial_reader.py`:

```python
import threading
import time

from _fakes import FakeSerial


class TestCoordinatedQuery:
    def test_query_state_coordinated_returns_match(self):
        conn = FakeSerial()
        cs._reader_active.set()
        try:
            # Simulate the reader delivering the response shortly after the query writes
            def deliver():
                time.sleep(0.05)
                cs._deliver_to_pending(["@I", "L", "5", "100"], "@I L 5 100")
            threading.Thread(target=deliver, daemon=True).start()
            parts = cs.query_state(conn, 5, "L", timeout=1.0, max_retries=1)
            assert parts == ["@I", "L", "5", "100"]
        finally:
            cs._reader_active.clear()

    def test_query_state_coordinated_ignores_wrong_id(self):
        conn = FakeSerial()
        cs._reader_active.set()
        try:
            def deliver():
                time.sleep(0.05)
                # wrong id -> predicate must not match, query should time out
                assert cs._deliver_to_pending(["@I", "L", "3", "100"], "@I L 3 100") is False
            threading.Thread(target=deliver, daemon=True).start()
            parts = cs.query_state(conn, 5, "L", timeout=0.3, max_retries=1)
            assert parts is None
        finally:
            cs._reader_active.clear()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_serial_reader.py::TestCoordinatedQuery -v`
Expected: FAIL (query_state still uses direct read only; coordinated path absent).

- [ ] **Step 3: Implement `_send_and_match` and refactor queries**

In `cardio2e_modules/cardio2e_serial.py`, add the helper:

```python
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
```

Replace the body of `query_state` with:

```python
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
```

Replace the body of `query_name` with:

```python
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
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_serial.py tests/test_serial_reader.py -v`
Expected: PASS. The existing `tests/test_serial.py` query tests run in direct mode (reader inactive) and still pass; the new coordinated tests pass.

- [ ] **Step 5: Commit**

```bash
git add cardio2e_modules/cardio2e_serial.py tests/test_serial_reader.py
git commit -m "feat: two-mode serial queries (direct in bootstrap, coordinated in steady state)"
```

---

## Task 4: `SerialReader` thread

**Files:**
- Modify: `cardio2e_modules/cardio2e_serial.py`, `tests/_fakes.py`
- Test: `tests/test_serial_reader.py`

- [ ] **Step 1: Add `feed()` to FakeSerial**

In `tests/_fakes.py`, inside `FakeSerial`, add:

```python
    def feed(self, data):
        """Append bytes to be returned by subsequent reads."""
        self._read_buffer += data
```

- [ ] **Step 2: Write the failing test**

Add to `tests/test_serial_reader.py`:

```python
class TestSerialReaderProcessing:
    def _make_reader(self, conn):
        dispatched = []
        reader = cs.SerialReader(conn, on_message=lambda msg, parts: dispatched.append((msg, parts)))
        return reader, dispatched

    def test_spontaneous_message_is_dispatched(self):
        conn = FakeSerial()
        reader, dispatched = self._make_reader(conn)
        reader._buffer = "@I L 5 100\r"
        reader._process_buffer()
        assert dispatched == [("@I L 5 100", ["@I", "L", "5", "100"])]

    def test_pending_query_consumes_matching_line_no_dispatch(self):
        conn = FakeSerial()
        reader, dispatched = self._make_reader(conn)
        q = cs._register(lambda p: p[1] == "L" and p[2] == "5")
        try:
            reader._buffer = "@I L 5 100\r"
            reader._process_buffer()
            assert dispatched == []          # consumed by the pending query
            assert q.get_nowait() == "@I L 5 100"
        finally:
            cs._unregister(q)

    def test_spontaneous_during_pending_query_is_not_lost(self):
        # nº5 regression: a different entity updating while a query is pending
        conn = FakeSerial()
        reader, dispatched = self._make_reader(conn)
        q = cs._register(lambda p: p[1] == "L" and p[2] == "5")
        try:
            reader._buffer = "@I L 3 100\r@I L 5 100\r"
            reader._process_buffer()
            assert ("@I L 3 100", ["@I", "L", "3", "100"]) in dispatched
            assert q.get_nowait() == "@I L 5 100"
        finally:
            cs._unregister(q)
```

- [ ] **Step 3: Run to verify it fails**

Run: `python3 -m pytest tests/test_serial_reader.py::TestSerialReaderProcessing -v`
Expected: FAIL (`SerialReader` not defined).

- [ ] **Step 4: Implement `_split_messages` and `SerialReader`**

In `cardio2e_modules/cardio2e_serial.py`, add the line-splitting helper (lifted from the old listener) and the reader thread:

```python
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
```

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/test_serial_reader.py -v`
Expected: PASS (all reader processing tests, including the nº5 regression).

- [ ] **Step 6: Commit**

```bash
git add cardio2e_modules/cardio2e_serial.py tests/_fakes.py tests/test_serial_reader.py
git commit -m "feat: add SerialReader thread that owns reads and fulfils queries"
```

---

## Task 5: Rewire the listener (housekeeping loop + reader lifecycle)

**Files:**
- Modify: `cardio2e_modules/cardio2e_listener.py`
- Test: `tests/test_serial_reader.py`

- [ ] **Step 1: Write the failing test (integration, end-to-end)**

Add to `tests/test_serial_reader.py`:

```python
from cardio2e_modules.cardio2e_config import AppConfig, AppState
from cardio2e_modules import cardio2e_serial


class TestReaderQueryIntegration:
    def test_reader_thread_serves_a_coordinated_query(self):
        # FakeSerial preloaded with the response; the running reader delivers it
        conn = FakeSerial(to_read=b"@I L 5 100\r")
        reader = cardio2e_serial.SerialReader(conn, on_message=lambda msg, parts: None)
        reader.start()
        try:
            parts = cardio2e_serial.query_state(conn, 5, "L", timeout=2.0, max_retries=1)
            assert parts == ["@I", "L", "5", "100"]
        finally:
            reader.stop()
            reader.join(timeout=2)

    def test_reader_dispatches_spontaneous_update_to_handler(self):
        conn = FakeSerial(to_read=b"@I L 7 0\r")
        seen = []
        reader = cardio2e_serial.SerialReader(conn, on_message=lambda msg, parts: seen.append(parts))
        reader.start()
        try:
            deadline = time.time() + 2
            while not seen and time.time() < deadline:
                time.sleep(0.01)
            assert seen == [["@I", "L", "7", "0"]]
        finally:
            reader.stop()
            reader.join(timeout=2)
```

- [ ] **Step 2: Run to verify it passes for the query (reader already built) and shapes the listener change**

Run: `python3 -m pytest tests/test_serial_reader.py::TestReaderQueryIntegration -v`
Expected: PASS (this validates Task 4 end-to-end; no listener change needed for these two, but they guard the contract the listener relies on).

- [ ] **Step 3: Rewrite `listen_for_updates` as housekeeping + reader owner**

In `cardio2e_modules/cardio2e_listener.py`:

Update imports — add `SerialReader` and drop the now-unused `query_state`/`_serial_lock` from the serial import line:

```python
from .cardio2e_serial import send_date, query_state, SerialReader
```

Replace the entire `listen_for_updates` function (the read loop) with:

```python
def listen_for_updates(serial_conn, mqtt_client, config, app_state):
    """Run the housekeeping loop while a SerialReader thread owns the port.

    Returns when the connection is lost (reader stopped), so the caller can
    reconnect.
    """
    last_time_sent = time.monotonic()
    last_heartbeat = time.monotonic()
    last_sync = time.monotonic()

    _publish_diagnostics_autodiscovery(mqtt_client)

    def on_message(msg, message_parts):
        _LOGGER.info("Processing individual message: %s", msg)
        app_state.increment_messages()
        _dispatch_message(serial_conn, mqtt_client, config, app_state, msg, message_parts)

    reader = SerialReader(serial_conn, on_message)
    reader.start()

    try:
        while serial_conn.is_open and reader.is_alive():
            now = time.monotonic()

            if (now - last_time_sent) >= config.update_date_interval:
                time_command = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
                send_date(serial_conn, time_command)
                cardio2e_errors.report_error_state(mqtt_client, "No errors.")
                _LOGGER.info("Sent time command to cardio2e: %s", time_command)
                last_time_sent = now

            if (now - last_heartbeat) >= HEARTBEAT_INTERVAL:
                _publish_heartbeat(mqtt_client, app_state)
                last_heartbeat = now

            if config.sync_interval > 0 and (now - last_sync) >= config.sync_interval:
                _sync_all_entities(serial_conn, mqtt_client, config, app_state)
                last_sync = now

            time.sleep(0.5)
    finally:
        reader.stop()
        reader.join(timeout=2)

    _LOGGER.warning("Serial reader stopped; connection considered lost.")
```

- [ ] **Step 4: Move the `@A B 1` re-query off the reader thread**

Still in `cardio2e_listener.py`, in `_dispatch_message`, replace the `@A B 1` branch:

```python
        elif entity_type == "B" and entity_id == 1:
            _get_entity_state(serial_conn, mqtt_client, 1, "B", config, app_state)
            _LOGGER.info("Bypass zones re-publish.")
```

with a thread spawn (so the reader thread never blocks on its own query):

```python
        elif entity_type == "B" and entity_id == 1:
            threading.Thread(
                target=_get_entity_state,
                args=(serial_conn, mqtt_client, 1, "B", config, app_state),
                daemon=True,
            ).start()
            _LOGGER.info("Bypass zones re-publish (async).")
```

Add `import threading` at the top of `cardio2e_listener.py` if not present.

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest -v`
Expected: PASS (96 existing + new reader tests). If any existing listener test assumed the old read loop, update it to the housekeeping/reader model.

- [ ] **Step 6: Commit**

```bash
git add cardio2e_modules/cardio2e_listener.py tests/test_serial_reader.py
git commit -m "refactor: listener housekeeping loop driving a SerialReader thread"
```

---

## Task 6: Smoke-check bootstrap/shutdown wiring in main

**Files:**
- Modify: `cardio2e.py` (only if the shutdown path needs the reader stopped — verify)
- Test: manual reasoning + full suite

- [ ] **Step 1: Verify the lifecycle**

Confirm in `cardio2e.py` that:
- `_do_login_and_init` runs entirely with the reader inactive (`_reader_active` clear), so bootstrap queries use direct mode.
- `subscribe_after_init` is called, then `listen_for_updates` starts the reader. The brief gap between subscribe and reader start is safe (a command write does not need the reader; the response buffers in the OS until the reader starts).
- On `handle_shutdown`, `listen_for_updates` is not running in the signal context; the reader is a daemon thread and `serial_conn.close()` makes its read loop exit. No code change is required unless verification shows otherwise.

- [ ] **Step 2: No code change expected**

If verification shows the reader is not stopped cleanly on SIGTERM (e.g., logout races the reader), add to `handle_shutdown` in `cardio2e.py`, before `logout(serial_conn)`:

```python
        # Reader is a daemon thread; closing the port makes it exit. No explicit
        # join needed here because listen_for_updates owns the reader lifecycle.
```

(Leave a comment only; the daemon reader plus `serial_conn.close()` is sufficient.)

- [ ] **Step 3: Run the full suite**

Run: `python3 -m pytest -q`
Expected: PASS.

- [ ] **Step 4: Commit (only if `cardio2e.py` changed)**

```bash
git add cardio2e.py
git commit -m "docs: note reader shutdown semantics in signal handler"
```

---

## Task 7: Version bump, changelog, final verification

**Files:**
- Modify: `cardio2e.py`, `CHANGELOG.md`

- [ ] **Step 1: Run the full suite once more**

Run: `python3 -m pytest -q`
Expected: PASS (all tests green).

- [ ] **Step 2: Bump version**

In `cardio2e.py`, change `VERSION = "2.0.13"` to `VERSION = "2.1.0"`.

- [ ] **Step 3: Update the changelog**

Prepend to `CHANGELOG.md` (after `# Changelog`):

```markdown
## v2.1.0 - 2026-06-14

### Changed
- Rework the RS-232 layer around a single reader thread that owns all reads. Spontaneous `@I` updates arriving during a query are no longer lost (nº5); the periodic sync no longer blocks message reception (nº8); all writes go through one throttled write point (nº9).
- Queries now match responses by entity type **and** id (previously type only), so a query no longer captures an unrelated update of the same type.
- Login no longer logs the password (redacted).
```

- [ ] **Step 4: Commit**

```bash
git add cardio2e.py CHANGELOG.md
git commit -m "chore: release v2.1.0 (single serial reader thread)"
```

- [ ] **Step 5: Hardware validation (user)**

Deploy to the host and confirm: login + state population, live events (physical switch, zone trip), HA commands (light/switch/cover/alarm/HVAC/scene), cover STOP, bypass toggle, and that the periodic sync runs without stalling events. This is the real acceptance gate — the suite cannot exercise the hardware.

---

## Self-review notes

- **Spec coverage:** nº9 → Task 1; nº5 registry/reader → Tasks 2/4; two-mode queries → Task 3; nº8 (non-blocking sync) → Task 5 housekeeping; reader-thread deadlock avoidance (`@A B 1`) → Task 5 Step 4; bootstrap/steady-state split → Tasks 3/5/6; type+id matching → Task 3; password redaction → Task 1. Testing section → Tasks 2–6. All spec sections mapped.
- **Type consistency:** `_write`, `_register`/`_unregister`/`_deliver_to_pending`, `_reader_active`, `_send_and_match`/`_direct_request`/`_coordinated_request`, `_split_messages`, `SerialReader(serial_conn, on_message)` with `.start()/.stop()/.is_alive()/._process_buffer()/._buffer` are used consistently across tasks and tests.
- **Out of scope unchanged:** MQTT topics/payloads, autodiscovery, HVAC heating=cooling−2, zone `device_class` motion.
```
