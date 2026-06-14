# Serial reader thread — design

**Date:** 2026-06-14
**Status:** Approved (pending implementation plan)

## Problem

Three related issues in the RS-232 layer, all rooted in concurrent/synchronous
access to the serial port:

- **Lost spontaneous messages (nº 5):** `query_state` / `query_name` read from
  the port inside `_serial_lock` and discard any line that does not match the
  expected prefix ("Message ignored"). Spontaneous `@I` updates that arrive
  during a query are dropped and never reach the dispatcher, leaving Home
  Assistant out of sync until the next event for that entity. Worst during the
  periodic sync, which issues many queries back to back.
- **Blocking sync (nº 8):** `_sync_all_entities` runs inline in the listener
  loop. Each `query_state` can take up to 5 retries × 0.5 s = 2.5 s per
  unresponsive entity, so the sync can stall message reception for tens of
  seconds — which also maximises the nº 5 window.
- **Incomplete throttle (nº 9):** the 150 ms minimum inter-command interval
  (v2.0.9) is only applied in `send_command`. `query_state`, `query_name` and
  `login` write to the port without respecting or updating `_last_command_time`,
  so a sync burst can write unspaced and the controller may drop responses.

## Decision

Adopt a **single reader thread** architecture (approach B), as a **direct
replacement** of the current model (no compatibility flag; rollback via git if
hardware testing fails). The user validates on real hardware.

## Architecture

```
                        ┌─────────────────────────────┐
   serial port  ──────► │  SerialReader (thread)       │
   (only reader reads)  │  read → split lines →         │
                        │  parse → route:               │
                        │   • matches a pending query?  │──► hand to caller
                        │   • else → dispatcher          │──► publish to MQTT
                        └─────────────────────────────┘
                                    ▲
   send_command / query_* / login ──┘ (write only, via throttled _write)
        ↑                    ↑
   MQTT thread          main thread
   (HA commands)        (housekeeping: date/heartbeat/sync)
```

Key properties:

- The reader thread is the **only** caller that performs `read()`. No concurrent
  reads means no spontaneous line is ever consumed-and-discarded → **nº 5 fixed
  at the root**.
- All writes (from any thread) go through one throttled `_write()` → **nº 9
  fixed**.
- The periodic sync runs in the main thread and its queries are serviced by the
  reader in parallel with continuous reading → **nº 8 fixed** (sync no longer
  blocks reception).

External behaviour (MQTT topics, payloads, autodiscovery) is unchanged — Home
Assistant sees no difference.

## Components

All in `cardio2e_serial.py` unless noted.

### 1. `_write(serial_conn, data)` — central throttled write

Single write point. Acquires the lock, enforces `_MIN_COMMAND_INTERVAL`, writes,
flushes, updates `_last_command_time`. Used by `send_command`, `query_state`,
`query_name`, `login`, `logout`, `send_date`. With the reader as the sole
reader, the lock now protects writes only (ends today's read/write contention).

### 2. `SerialReader` thread — owns reads

Loop: read available bytes → accumulate buffer → split into complete lines
(reusing the current `listen_for_updates` logic, including `#015`→`\r` and the
`@`-split handling) → parse each line into `message_parts` → route:

- matches a pending query? → put the parsed line on that request's queue, and do
  **not** also dispatch it;
- else → `_dispatch_message` (publishes to MQTT, as today).

### 3. Pending-request registry — request/response coordination

A shared structure mapping an expected key → a queue (or event). `query_*`
registers its key, calls `_write`, then blocks on the queue with a timeout; the
reader, on a matching line, puts it on the queue.

**Correctness improvement:** today the match is by type only (`@I L `), so a
query for light 5 can capture a spontaneous update for light 3. The reader will
match by **type + id** (Z/B keep type-level matching, since they use id 1 / a
whole-string payload). This is more precise than the current behaviour.

### 4. Two phases: synchronous bootstrap → reader in steady state

- **Bootstrap** (`login` + `parse_login_response` + cover/scenario init): stays
  **as today**, synchronous, **before** the reader starts. This is where the set
  of existing entities is discovered (the login burst) and name fetches happen.
  Minimal risk, code essentially untouched.
- **Steady state:** after init and the MQTT subscribe, the `SerialReader`
  starts. From then on it owns all reads.
- `query_state` / `query_name` detect the mode: reader active → coordinate via
  the registry; reader inactive (bootstrap) → direct read (current behaviour). A
  small branch, not a duplicate code path.

### 5. Main loop becomes housekeeping

`listen_for_updates` stops reading: it runs only the periodic tasks (date,
heartbeat, sync) and watches the connection. On disconnect it signals the reader
to stop, joins it, and hands off to the existing reconnection backoff.

**Shutdown:** signal the reader → join → logout → close.

## Error handling & edge cases

- **Query with no response:** the queue `get(timeout)` expires; current semantics
  preserved — `query_*` return `None` after N attempts and log a warning. The
  pending entry is always removed (try/finally) so the registry never leaks.
- **Reader read exception (port dropped):** log, mark the connection lost, end
  the thread; the housekeeping loop detects it (`serial_conn.is_open` / thread
  dead) and hands off to backoff reconnection. Pending queries expire via timeout
  and return `None`.
- **Malformed line:** the reader ignores it and continues (as today), bumping the
  diagnostics error counter.
- **Duplicate/late response to an already-expired query:** reaches the registry,
  no queue waiting → falls through to the dispatcher as a spontaneous update
  (publishes the state). Harmless and arguably correct.

## Testing

Using the existing suite with a `FakeSerial` that yields bytes in stages, we can
now test what was previously impossible:

- a spontaneous update arriving *during* a pending query is **dispatched**
  (regression guard for nº 5);
- a query matches the correct response **by type + id** (light 5 does not capture
  a light 3 update);
- the throttle is respected **across** `send_command`, `query_*` and `login`
  (all via `_write`);
- the reader dispatches normal updates to the right handlers;
- a query timeout returns `None` and clears the registry.

## Change inventory

- `cardio2e_serial.py`: new `_write`, `SerialReader`, pending-request registry,
  two-mode `query_*`; `login`/`logout`/`send_date` via `_write`.
- `cardio2e_listener.py`: `listen_for_updates` → housekeeping loop (no reading);
  reuse the line-split logic and `_dispatch_message` in the reader.
- `cardio2e.py`: start/stop the reader in the lifecycle; ordered shutdown.
- `tests/`: new reader/coordination tests; adjust tests that assume the old
  model.
- Version bump + CHANGELOG at the end.

## Out of scope

- No change to MQTT topics/payloads or autodiscovery.
- No compatibility flag (direct replacement).
- The intentional behaviours documented in project memory (HVAC heating setpoint
  derived from cooling − 2; zone `device_class` always `motion`) are unaffected.
```
