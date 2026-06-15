# Cardio2e ↔ MQTT bridge

Integrates a **Secant Cardio2e** home-automation controller (RS-232) with an
MQTT broker, exposing its entities to **Home Assistant** via MQTT autodiscovery.

The bridge talks to the controller over the serial bus, publishes entity state
to MQTT, and forwards commands received from MQTT back to the controller.

## Supported entities

| Cardio2e type | Home Assistant entity |
|---------------|-----------------------|
| Lights (`L`) | `light` (with optional brightness for configured dimmers) |
| Relays/switches (`R`) | `switch` |
| Covers (`C`) | `cover` (position + open/close/stop) |
| HVAC (`H`/`T`) | `climate` |
| Security (`S`) | `alarm_control_panel` (arm away / disarm) |
| Zones (`Z`/`B`) | `binary_sensor` + bypass `switch` |
| Scenarios/macros (`M`) | `scene` |
| Diagnostics & errors | `sensor` (uptime, message/error/reconnect counters, last command, last error, time since last message, reader status, pending queries) |

All entities are created automatically in Home Assistant through MQTT
autodiscovery — no manual YAML is required.

## How it works

- A single **reader thread** owns all serial reads, parses each line, and either
  fulfils a pending query (matched by entity type + id) or dispatches it as a
  state update to MQTT. This means spontaneous controller events are never lost
  while a query is in flight.
- All serial **writes** go through one throttled path (minimum interval between
  commands) so the controller does not drop commands sent in rapid succession.
- A **housekeeping loop** runs alongside the reader: it sends the date
  periodically, publishes a heartbeat + diagnostics, and runs a periodic
  re-sync of all known entities to keep Home Assistant aligned with the
  controller.
- On startup the bridge logs in, parses the controller's initial state dump, and
  initializes covers and scenarios before subscribing to command topics.
- The serial connection auto-reconnects with exponential backoff, and an MQTT
  Last Will marks the bridge offline if it dies.

## Requirements

- Python 3
- [`pyserial`](https://pypi.org/project/pyserial/)
- [`paho-mqtt`](https://pypi.org/project/paho-mqtt/) (works with both 1.x and 2.x)

Install the system packages (Debian/Ubuntu example):

```
sudo apt-get install python3-serial python3-paho-mqtt
```

## Installation

Clone the repo into `/opt`:

```
cd /opt
sudo git clone https://github.com/fapgomes/cardio2e.git
```

Copy the sample config and edit it with your settings:

```
cd /opt/cardio2e
sudo cp cardio2e.conf-sample cardio2e.conf
sudo vi cardio2e.conf
```

Create the systemd unit `/etc/systemd/system/cardio2e.service` (adjust `User=`
to a user with access to the serial port, e.g. one in the `dialout` group):

```
[Unit]
Description=cardio2e
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/cardio2e/cardio2e.py
WorkingDirectory=/opt/cardio2e
StandardOutput=inherit
StandardError=inherit
Restart=always
User=openhab

[Install]
WantedBy=multi-user.target
```

Enable and start the service:

```
sudo systemctl daemon-reload
sudo systemctl enable --now cardio2e
```

## Configuration

The config file has three sections. Inline comments (`# ...`) are allowed.

### `[global]`

| Option | Default | Description |
|--------|---------|-------------|
| `debug` | `0` | `1` enables DEBUG logging |
| `ha_discover_prefix` | `homeassistant` | MQTT discovery prefix Home Assistant listens on |
| `syslog_address` | *(empty)* | Remote syslog host (UDP). Empty disables remote syslog |
| `syslog_port` | `514` | Remote syslog port |

### `[cardio2e]`

| Option | Default | Description |
|--------|---------|-------------|
| `serial_port` | `/dev/ttyUSB0` | RS-232 device |
| `baudrate` | `9600` | Serial baud rate |
| `password` | `00000` | Cardio2e login password |
| `update_date_interval` | `3600` | Seconds between sending the date to the controller |
| `sync_interval` | `43200` | Seconds between periodic re-sync of all entities (`0` disables) |
| `fetch_light_names` | `true` | Fetch light names on startup (can be disabled after first run) |
| `dimmer_lights` | `[]` | List of light ids to treat as dimmers (brightness) |
| `force_include_lights` | `[]` | Light ids to publish even if absent from the login dump |
| `fetch_switch_names` | `true` | Fetch switch names on startup |
| `fetch_cover_names` | `true` | Fetch cover names on startup |
| `skip_init_cover_state` | `false` | Skip querying cover state on startup |
| `ncovers` | `20` | Number of covers to initialize (iterated 1..N) |
| `fetch_names_hvac` | `true` | Fetch HVAC names on startup |
| `code` | `12345` | Security code used to arm/disarm the alarm |
| `fetch_zone_names` | `true` | Fetch zone names on startup |
| `zones_normal_as_off` | `[]` | Zone ids whose "normal" state should report `OFF` (inverted) |
| `nscenarios` | `0` | Number of scenarios to initialize (`0` disables; iterated 1..N) |
| `fetch_scenario_names` | `true` | Fetch scenario names on startup |

> **Note on `ncovers` / `nscenarios`:** covers and scenarios are iterated blindly
> from 1 to N (they are not discovered from the login dump). Set these to your
> actual counts — undefined slots in the range are queried and time out before
> falling back to a generic name, slowing startup.

### `[mqtt]`

| Option | Description |
|--------|-------------|
| `address` | MQTT broker host |
| `port` | MQTT broker port (e.g. `1883`) |
| `username` | MQTT username |
| `password` | MQTT password |

## Development

Run the test suite (no hardware or broker required — it uses fakes):

```
pip install -r requirements-dev.txt   # or: sudo apt-get install python3-pytest
python3 -m pytest
```

Design and implementation notes live under `docs/superpowers/`.
