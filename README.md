# Sensgreen Sensor Simulator

A configurable sensor simulation engine for **Sensgreen demo accounts**. It generates realistic readings for IAQ sensors, energy meters, occupancy sensors, entry/exit counters, HVAC virtual points, and device health metrics — and ships them via MQTT or CSV.

> Status: **scaffolding**. Project structure and placeholders only — sensor logic is not implemented yet.

## Run modes

The simulator supports two top-level modes:

### 1. Live mode
- Generates realistic, time-evolving sensor readings.
- Publishes them to the **Sensgreen MQTT broker** using the standard payload format:
  ```json
  {
    "deviceEui": "...",
    "timestamp": 1772445600000,
    "data": { "temperature": 23.4, "humidity": 55.2 }
  }
  ```
- Intended for end-to-end demos against a live Sensgreen environment.

### 2. Historical mode
- Generates historical sensor data over a configurable time range.
- Exports CSV files (primary format: `readings_long.csv`) that can be imported into the Sensgreen database.
- Intended for back-filling demo accounts with believable history.

## Project structure

```
simulator/
  main.py              # CLI entry point
  config_loader.py     # Loads YAML/JSON configs (+ env overrides)
  models/              # Canonical internal reading + domain types
  sensors/             # Sensor implementations (IAQ, energy, occupancy, ...)
  outputs/             # Output adapters (MQTT live, CSV historical)
  validators/          # Physical / temporal / correlation / hierarchy checks
  integrations/        # External clients (MQTT broker, APIs)
  utils/               # Shared helpers
configs/               # Scenario configuration files
outputs/               # Generated CSVs and logs
tests/                 # Unit tests
```

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit values
```

## Run

```bash
# Live mode (publishes to MQTT — once implemented)
python -m simulator.main --mode live --config configs/default.yaml

# Historical mode (exports CSVs — once implemented)
python -m simulator.main --mode historical --config configs/default.yaml
```

## Test

```bash
pytest
```

## Web UI (FastAPI)

An internal admin UI is scaffolded under `api/` (FastAPI backend) and
`web/` (Jinja templates + static assets). It is a thin layer over the
simulator engine — it never reimplements simulation, validation, or MQTT
logic. See `UI_CONTEXT.md` for the design rules.

### Run locally

```bash
uvicorn api.main:app --reload
```

Then open:

- <http://localhost:8000/> — Dashboard with project cards
- <http://localhost:8000/projects/new> — Create a new project
- <http://localhost:8000/health> — Liveness probe (`{"status": "ok"}`)

### JSON API (MVP)

| Method | Path                       | Description           |
| ------ | -------------------------- | --------------------- |
| GET    | `/api/projects`            | List all projects     |
| POST   | `/api/projects`            | Create a project      |
| GET    | `/api/projects/{id}`       | Get one project       |

Project records are persisted as JSON files under `data/projects/` — this
directory is gitignored. Replace `ProjectService` with a real database
later without touching routes or templates.

### Stack

- FastAPI + Jinja2 templates
- HTMX (CDN) for partial updates
- Tailwind CSS (CDN) for styling
- No React, no Vue — by design (see `UI_CONTEXT.md` §3 / §4)

## Design principles
- Generate **one canonical internal reading** first; output adapters only convert.
- **Config-driven** behavior — no hardcoded demo data inside sensor classes.
- Realistic relationships between occupancy, CO₂, energy, HVAC, and people counting.
- Validation: physical validity, temporal consistency, correlations, hierarchy, scenarios.
- Type hints, dataclasses, and unit tests for every module.
