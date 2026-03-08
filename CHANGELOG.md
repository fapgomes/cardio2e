# Changelog

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
