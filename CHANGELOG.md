# Changelog

## v2.3.1 - 2026-06-15

### Fixes
- Truly clean shutdown. The signal handler now only sets an event and does no serial/MQTT I/O (the previous version still did I/O from the signal context, which raced and produced "Bad file descriptor" and a spurious "'Event' object is not callable"). The reader thread is stopped and joined before the port is closed, and all teardown runs in the normal context; the reconnect backoff is interruptible by shutdown.

## v2.3.0 - 2026-06-15

### Features
- Expose key diagnostics as dedicated Home Assistant entities (via autodiscovery, read from the existing diagnostics JSON): `sensor.cardio2e_seconds_since_last_message` (duration), `sensor.cardio2e_pending_queries`, `sensor.cardio2e_reconnects`, and `binary_sensor.cardio2e_reader` (running). The diagnostics sensor and its attributes are unchanged.

## v2.2.1 - 2026-06-15

### Fixes
- Fail fast on undefined cover slots. Covers are probed blindly over `1..ncovers` (they are not in the login dump), so undefined slots stalled startup ~30s each on the name query. Cover name fetches now use a 2s/2-retry budget, and a slot with no name is skipped entirely (no state query, no autodiscovery).

### Other
- Add `requirements.txt` for the runtime dependencies (`pyserial`, `paho-mqtt`).

## v2.2.0 - 2026-06-15

### Features
- Richer diagnostics sensor: adds `reader_active` and `pending_queries` (serial reader health), `seconds_since_last_message` (detects a silent/stuck bus), a `reconnects` counter, and `last_error`. Existing topics/payloads are unchanged — these are added keys.

### Docs
- Rewrite the README (architecture, supported entities, full config reference, development/testing).

## v2.1.3 - 2026-06-14

### Fixes
- Clean shutdown on SIGTERM/SIGINT. The signal handler no longer calls `sys.exit()` (which raced with daemon-thread/interpreter teardown and surfaced a spurious "'Event' object is not callable" error and a "Bad file descriptor" reader error). It now closes the port to unblock the listener, which stops the reader and lets the process exit cleanly. The reader logs a closed port at INFO instead of ERROR during shutdown.

## v2.1.2 - 2026-06-14

### Fixes
- Fail fast when fetching names of undefined scenario slots. With `nscenarios` spanning gaps (e.g. scenario 12 undefined while 13–20 exist), each missing slot stalled startup for 30s on the name query. Scenario name fetches now use a 2s timeout / 2 retries; real scenarios answer in well under a second.

## v2.1.1 - 2026-06-14

### Fixes
- Stop re-querying bypass state with `@G B 1` after every zone bypass toggle. The controller rejects that command (`@N B 2`) and, fired per toggle, it flooded and garbled the RS-232 stream under rapid bypass changes. The new state is now published directly on set and republished from cache during sync.

## v2.1.0 - 2026-06-14

### Changed
- Rework the RS-232 layer around a single reader thread that owns all reads. Spontaneous `@I` updates arriving during a query are no longer lost (nº5); the periodic sync no longer blocks message reception (nº8); all writes go through one throttled write point (nº9).
- Queries now match responses by entity type **and** id (previously type only), so a query no longer captures an unrelated update of the same type.
- Login no longer logs the password (redacted).

## v2.0.13 - 2026-06-13

### Cleanup
- Reword the startup log line to "Cardio2e version v%s starting..."

## v2.0.12 - 2026-06-13

### Cleanup
- Rename the third parameter of `send_command` to `entity_id_or_value` and document that the date command (`D`) uses it as the timestamp payload rather than an entity id (no behavioural change)

## v2.0.11 - 2026-06-13

### Fixes
- Accept negative temperature readings — the `@I T` parser dropped sub-zero values (e.g. `-1.5`), so cold-day temperatures never reached MQTT/HA

### Cleanup
- Remove unused imports (`threading` in main; `re`, `TEMP_CODE_TO_STATUS`, `cardio2e_autodiscovery` in listener)
- Use `sys.exit()` instead of the site-module builtin `exit()` in the signal handler
- `_get_entity_state` returns the mapped switch state (`ON`/`OFF`) for type R, matching type L
- Drop the hardcoded 16-zone cap in `_get_entity_state` to match the other zone-iteration paths
- `subscribe_after_init` uses the public `user_data_get()` API with a fallback for older paho versions
- Populate the diagnostics `last_command` field (previously always empty); numeric scene payloads are redacted as they may carry a security code
- Fix `query_name` docstring

## v2.0.10 - 2026-06-12

### Fixes
- Strip inline comments when parsing the config file — values like `ncovers = 20  # comment` kept the comment as part of the value, crashing startup on int options, silently disabling boolean flags, emptying JSON lists and corrupting the alarm code
- Add paho-mqtt 2.x compatibility: client is created with callback API VERSION2 when available, and connect/disconnect callbacks accept both 1.x and 2.x signatures

## v2.0.9 - 2026-05-25

### Fixes
- Add 150ms minimum interval between consecutive RS-232 commands to prevent the Cardio2e controller from dropping commands sent in rapid succession

## v2.0.8 - 2026-04-18

### Fixes
- Redact alarm code from RS-232 command logs (security commands now logged as `A ****` / `D ****`)

### Cleanup
- Remove global `.upper()` on MQTT payloads in `_on_message`; each handler normalizes case locally as needed

## v2.0.7 - 2026-04-12

### Fixes
- Fix periodic sync causing covers to physically move — the `@G C` query made the Cardio2e re-issue position commands to the motor
- Cover states are now cached in memory and republished from cache during sync instead of querying the hardware

## v2.0.6 - 2026-03-08

### Features
- Add scenario (macro) support — fire-and-forget scenes via `@S M {id}`
- New config options: `nscenarios` (default: 0, disabled) and `fetch_scenario_names` (default: true)
- Scenarios appear as `scene` entities in Home Assistant via MQTT autodiscovery
- Scenario names fetched from Cardio2e controller via `@G N M {id}`

## v2.0.5 - 2026-03-08

### Features
- Add periodic entity sync to re-query all known entities and republish state to MQTT
- New `sync_interval` config option (default: 43200s / 12h, 0 to disable)
- Ensures HA stays in sync with Cardio2e hardware even if MQTT messages are lost

## v2.0.4 - 2026-03-08

### Fixes
- Fix NACK error parsing: use last element as error code to handle optional object number per protocol spec
- Replace deprecated `datetime.utcnow()` with `datetime.now(timezone.utc)`
- Accept integer temperatures and setpoints in HVAC regexes (e.g. "21" in addition to "21.0")

## v2.0.3 - 2026-02-22

### Fixes
- Treat alarm code as string instead of int to preserve leading zeros

## v2.0.2 - 2026-02-21

### Fixes
- Fix zone names not loaded when zone state contains N (Normal) or E (Error) characters
- Prevent empty query_name results from overwriting valid cached names

## v2.0.1 - 2026-02-21

### Features
- Show entity friendly names in log messages (e.g. `Light Sala (id: 7) state updated to: OFF`)
- Add version number and log it on startup
- Add remote syslog support (UDP)

### Fixes
- Translate HVAC mode before publishing, demote serial log to debug
- Sync HVAC app_state on runtime updates from device
- Replace SysLogHandler with custom UDP handler for RFC 3164 compatibility

### Cleanup
- Replace ast.literal_eval with json.loads for config parsing

## v2.0.0 - 2026-02-17

### Major refactor
- Split monolithic cardio2e.py into modular architecture
- Replace readline() with bulk read for faster serial response
- Add thread safety and automatic reconnection with exponential backoff
- Add global serial lock to prevent contention between listener and MQTT threads

### Features
- Non-blocking cover STOP with actual position query
- Heartbeat and diagnostics sensor for Home Assistant
- Deferred MQTT subscriptions (subscribe after login/init completes)
- MQTT messages retained by default

### Fixes
- HVAC commands and login 2-phase read
- Serial lock and cover STOP logic
- Handle @I T (temperature) in listener

### Cleanup
- Demote debug logs to debug level
- Remove unused imports

## v1.x - 2024-11 to 2025-03

### Features
- Initial release with support for Lights, Switches (relays) and Zones
- Login to Cardio2e for initial state
- HVAC support (heating/cooling setpoints, fan, mode)
- Date/time sync with Cardio2e
- Security alarm support (arm/disarm via MQTT)
- Temperature sensor parsing
- Error handling for NACK responses from Cardio2e
- Entity name fetching from device and Home Assistant MQTT autodiscovery
- Force-include lights not present in login response
- Cover support with stop/open/close commands
- Zone bypass support (single and multiple zones)
- RS-232 write function with success/failure return

### Fixes
- Zone state corrections
- MQTT retain for error states
- Cover acknowledgment handling
- HVAC control bug fixes
- Password handling
- Increased timeouts for login and name fetching
- Compatibility with different Cardio2e device firmware versions
