# Building IoT & HVAC Simulator# Sensgreen Sensor Simulator



A physics-driven simulation engine for **smart-building IoT deployments**. It generates realistic, causally-correlated readings for IAQ sensors, energy meters, people counters, occupancy sensors, door contacts, and virtual HVAC controllers — then ships them via **MQTT (live)** or **CSV (historical)**.A configurable sensor simulation engine for **Sensgreen demo accounts**. It generates realistic readings for IAQ sensors, energy meters, occupancy sensors, entry/exit counters, HVAC virtual points, and device health metrics — and ships them via MQTT or CSV.



Built around a *room-is-authoritative* state engine: each zone owns its own physics (CO₂ mass-balance, temperature setpoint tracking, ventilation-driven decay, door-open boosts). Sensors are **observers** of that truth, with per-device personality (offset, drift, noise, near-window/door/HVAC bias) so two sensors in the same room realistically disagree.> Status: **scaffolding**. Project structure and placeholders only — sensor logic is not implemented yet.



> **Status:** P11 complete — 513 tests passing. True Room State Engine, virtual HVAC, declarative causal scenarios all live.## Run modes



---The simulator supports two top-level modes:



## What makes it realistic### 1. Live mode

- Generates realistic, time-evolving sensor readings.

| Capability | What it means |- Publishes them to the **Sensgreen MQTT broker** using the standard payload format:

|---|---|  ```json

| **True room state** | Zones own CO₂, temperature, humidity, occupancy, door, HVAC mode. Sensors only *observe*. |  {

| **First-order physics** | CO₂ mass balance with per-occupant generation + ventilation decay. Temperature tracks HVAC setpoint. |    "deviceEui": "...",

| **Device personalities** | 7 profiles (`normal`, `slightly_noisy`, `slightly_drifty`, `offset`, `near_door`, `near_window`, `near_hvac_supply`). Deterministic per-EUI seeding. |    "timestamp": 1772445600000,

| **Virtual HVAC** | Closed-loop controller emits `mode`, `setpoint`, `supply_temp`, `fan_speed`, `valve_position`, `ventilation_rate` — and writes back into room physics. |    "data": { "temperature": 23.4, "humidity": 55.2 }

| **Causal scenarios** | Declarative time-window rules: `morning_rush`, `lunch_rush`, `cleaning_routine`, `night_setback`. Filter by `room_type` or zone id. |  }

| **Outdoor coupling** | Door opens pull outdoor temperature into the room. Cold winter day → real cold draft. |  ```

| **Validation** | Physical, temporal, correlation, hierarchy, and scenario validators score every history run (0–100). |- Intended for end-to-end demos against a live Sensgreen environment.



---### 2. Historical mode

- Generates historical sensor data over a configurable time range.

## Run modes- Exports CSV files (primary format: `readings_long.csv`) that can be imported into the Sensgreen database.

- Intended for back-filling demo accounts with believable history.

### 1. Live mode

Generates time-evolving readings and publishes them to the **Sensgreen MQTT broker**:## Project structure



```json```

{simulator/

  "deviceEui": "F2-OFFICE-IAQ-01",  main.py              # CLI entry point

  "timestamp": 1772445600000,  config_loader.py     # Loads YAML/JSON configs (+ env overrides)

  "data": { "temperature": 22.6, "humidity": 48.3, "co2": 712 }  models/              # Canonical internal reading + domain types

}  sensors/             # Sensor implementations (IAQ, energy, occupancy, ...)

```  outputs/             # Output adapters (MQTT live, CSV historical)

  validators/          # Physical / temporal / correlation / hierarchy checks

### 2. Historical mode  integrations/        # External clients (MQTT broker, APIs)

Generates a configurable time range of history and exports:  utils/               # Shared helpers

configs/               # Scenario configuration files

- `readings_long.csv` — one row per metric reading (canonical)outputs/               # Generated CSVs and logs

- `uplinks_json.csv` — Sensgreen MQTT-style payloadstests/                 # Unit tests

- `devices.csv` — device manifest```

- `readings_internal.jsonl` — raw internal reading stream

- `validation_report.json` — quality score + findings## Setup



---```bash

python3.11 -m venv .venv

## Quick startsource .venv/bin/activate

pip install -r requirements.txt

```bashcp .env.example .env   # then edit values

python3.11 -m venv .venv```

source .venv/bin/activate

pip install -r requirements.txt## Run

cp .env.example .env   # then edit values

``````bash

# Live mode (publishes to MQTT — once implemented)

### CLIpython -m simulator.main --mode live --config configs/default.yaml



The CLI uses subcommands. Because there's no `__main__.py`, invoke it via `-c`:# Historical mode (exports CSVs — once implemented)

python -m simulator.main --mode historical --config configs/default.yaml

```bash```

# Validate a config without running it

python -c "from simulator.main import main; main(['dry-run-config', '--config', 'configs/realistic_mixed_use.yaml'])"## Test



# Generate a full day of history```bash

python -c "from simulator.main import main; main([pytest

  'generate-history',```

  '--config', 'configs/realistic_mixed_use.yaml',

  '--start',  '2026-06-02T06:00:00Z',## Web UI (FastAPI)

  '--end',    '2026-06-02T23:00:00Z',

  '--output-dir', 'outputs/realistic_day',An internal admin UI is scaffolded under `api/` (FastAPI backend) and

])"`web/` (Jinja templates + static assets). It is a thin layer over the

simulator engine — it never reimplements simulation, validation, or MQTT

# Run live (dry-run = console only, no MQTT)logic. See `UI_CONTEXT.md` for the design rules.

python -c "from simulator.main import main; main([

  'run-live', '--config', 'configs/realistic_mixed_use.yaml',### Run locally

  '--dry-run', '--realtime',

])"```bash

```uvicorn api.main:app --reload

```

### Web UI

Then open:

```bash

uvicorn api.main:app --reload- <http://localhost:8000/> — Dashboard with project cards

```- <http://localhost:8000/projects/new> — Create a new project

- <http://localhost:8000/health> — Liveness probe (`{"status": "ok"}`)

Open <http://localhost:8000/>:

- **Dashboard** — project cards### JSON API (MVP)

- **Devices** tab — rooms-first layout, profile inference, archetype generation

- **Bridge Test** — sample one of every device, validate Sensgreen mapping| Method | Path                       | Description           |

- **Live** — start/stop streaming with live reading panel| ------ | -------------------------- | --------------------- |

- **Scenarios** — preview which causal rules will fire| GET    | `/api/projects`            | List all projects     |

- **Events** — recent simulation events| POST   | `/api/projects`            | Create a project      |

| GET    | `/api/projects/{id}`       | Get one project       |

---

Project records are persisted as JSON files under `data/projects/` — this

## Example: realistic mixed-use buildingdirectory is gitignored. Replace `ProjectService` with a real database

later without touching routes or templates.

`configs/realistic_mixed_use.yaml` ships a 4-floor Istanbul building exercising **every** implemented sensor type, **6 of 7** personalities, and **every** HVAC mode:

### Stack

| Floor | Zone | Highlights |

|---|---|---|- FastAPI + Jinja2 templates

| 1 | Lobby | `entry_exit_counter`, busy main door, `near_door` IAQ, lighting submeter |- HTMX (CDN) for partial updates

| 2 | Open office | 2 IAQ (`near_window` + `near_hvac_supply`), AHU 8–19 h, 2 energy submeters |- Tailwind CSS (CDN) for styling

| 2 | Meeting Alpha / Beta | Sharp CO₂ spikes when occupied (small volume) |- No React, no Vue — by design (see `UI_CONTEXT.md` §3 / §4)

| 3 | Guest rooms ×3 | 24/7 fancoils, hotel-style |

| 4 | Server room | `mode_override: cool`, 19 °C setpoint, runs through nights and weekends |## Design principles

| 4 | Kitchen | `mode_override: fan_only`, 25 L/s/p extract, heavy door traffic |- Generate **one canonical internal reading** first; output adapters only convert.

| 4 | Warehouse | Loading door with long open dwell |- **Config-driven** behavior — no hardcoded demo data inside sensor classes.

- Realistic relationships between occupancy, CO₂, energy, HVAC, and people counting.

**37 devices, 10 zones.** A 16-hour run produces ~33 K readings with a validation score around 87.- Validation: physical validity, temporal consistency, correlations, hierarchy, scenarios.

- Type hints, dataclasses, and unit tests for every module.

---

## Project structure

```
simulator/
  main.py                  # CLI entry (argparse subcommands)
  config_loader.py         # YAML → typed config
  models/                  # Reading, Zone, Device dataclasses
  sensors/
    zone_state.py          # Authoritative room physics
    device_personality.py  # 7 named profiles, EUI-seeded RNG
    iaq_sensor_simulator.py
    hvac_simulator.py      # Closed-loop virtual HVAC
    energy_meter_simulator.py
    occupancy_sensor_simulator.py
    entry_exit_counter_simulator.py
    door_contact_simulator.py
  scenarios/
    causal.py              # TimeWindow × CausalEffect × CausalRule
  services/
    scenario_context.py    # Drives physics + scenarios per tick
    simulation_service.py  # Top-level runner
  outputs/                 # MQTT + CSV adapters
  validators/              # Physical / temporal / correlation / scenario
  devices/catalog.py       # Sensor metadata + field schemas
  integrations/            # Sensgreen metric mapper, MQTT client
api/                       # FastAPI backend
web/                       # Jinja templates + Tailwind + HTMX
configs/                   # YAML scenario configs
  realistic_mixed_use.yaml # 472-line showcase
  demo_office.yaml
  dubai_office.yaml
tests/                     # 513 tests
```

---

## Testing

```bash
pytest                      # full suite
pytest tests/test_zone_state.py -v
pytest -k causal_scenarios
```

Format & lint:
```bash
black .
ruff check .
```

---

## Design principles

- **One canonical internal reading.** Output adapters only translate.
- **Config-driven.** No hardcoded demo data inside sensor classes.
- **Composition over inheritance** for sensors.
- **Determinism is mandatory** — same seed + same config → same readings.
- **Realism through coupling**: occupancy → CO₂ → HVAC response → energy → door behaviour all share state.
- **Type hints, dataclasses, unit tests** for every module.
- **No secrets in code** — read from env.

---

## Domain coverage

| Metric | Unit | Source |
|---|---|---|
| CO₂ | ppm | IAQ (mass-balance from occupancy) |
| Temperature | °C | IAQ (HVAC setpoint tracking + outdoor coupling) |
| Humidity | %RH | IAQ |
| PM2.5 | µg/m³ | IAQ |
| TVOC | ppb | IAQ |
| Pressure | hPa | IAQ |
| Active power | W | Energy meter (per submeter) |
| Energy | kWh | Energy meter |
| Occupancy count | int | People counter / entry-exit / occupancy sensor |
| Door state | bool | Door contact (Poisson open events) |
| HVAC mode | enum | Virtual HVAC (`auto`, `cool`, `heat`, `fan_only`, `standby`, `off`) |
| Setpoint | °C | Virtual HVAC |
| Supply temp | °C | Virtual HVAC |
| Fan speed | % | Virtual HVAC |
| Valve position | % | Virtual HVAC |
| Ventilation rate | L/s/person | Virtual HVAC |

---

## License

Internal Sensgreen project. All rights reserved.
