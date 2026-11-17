# Home Assistant Power Monitoring Dashboard

Creates and maintains a native, storage-mode Home Assistant Lovelace dashboard for smart plugs with power and cumulative-energy sensors.

The generated dashboard contains four views:

- **Hour** — energy consumed during the latest rolling hour and raw power history.
- **Day** — consumption for the latest 24 hours, with hourly charts.
- **Month** — consumption for the latest 31 days, with daily charts.
- **Year** — consumption for the latest 365 days, with monthly charts.

Cumulative energy sensor states are never shown directly. The dashboard uses Home Assistant long-term statistics with `stat_type: change` to display consumption.

## Requirements

- Home Assistant sensors with suitable statistics metadata:
  - power: `device_class: power`, `state_class: measurement`
  - energy: `device_class: energy`, `state_class: total_increasing`
- An administrator Home Assistant long-lived access token
- Python 3.11+
- `aiohttp`

Dashboard management uses Home Assistant frontend WebSocket commands. These are used by the Home Assistant UI but are not a documented stable third-party API; verify compatibility after major Home Assistant upgrades.

## Configure

```fish
cp config.example.json config.json
$EDITOR config.json
```

`config.json` is intentionally gitignored because entity IDs can reveal private household details.

Each plug may define `color` and `marker`. The color is applied explicitly to that plug in every power and energy graph; the marker is shown before its name in every **Energy consumed** summary. If omitted, the tool assigns a stable default from its eight-color palette based on plug order. Set both fields explicitly if colors must remain unchanged when the plug list is reordered.

Credentials are read from `HASS_URL` and `HASS_TOKEN`. On a Hermes Agent host, the script can also use the active profile's protected secret scope from `$HERMES_HOME` or `~/.hermes`; it does not print or persist the token.

Do not put tokens in this repository, command arguments, or shell history. Use Hermes credential setup or another purpose-built secret manager.

## Install

Use an isolated environment:

```fish
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Usage

```fish
# Read-only capability and statistics check
.venv/bin/python ha_power_dashboard.py --config config.json probe

# Create a new dashboard and rollback record
.venv/bin/python ha_power_dashboard.py --config config.json create

# Replace the managed dashboard configuration
.venv/bin/python ha_power_dashboard.py --config config.json update

# Read back and validate the dashboard and statistics
.venv/bin/python ha_power_dashboard.py --config config.json verify

# Delete the dashboard created by this tool
.venv/bin/python ha_power_dashboard.py --config config.json rollback
```

`create` refuses to overwrite an existing dashboard. `update` only targets the configured dashboard URL path. `rollback` uses the gitignored `rollback.json` created alongside the script.

## Tests

```fish
python3 -m unittest discover -s tests -v
```

The tests validate generated Lovelace configuration without contacting Home Assistant.
