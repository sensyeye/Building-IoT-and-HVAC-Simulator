# Sensgreen Sensor Simulator — Copilot Instructions

This is a Python project that simulates Sensgreen IAQ (Indoor Air Quality) sensor devices and their telemetry data.

## Project conventions
- Python 3.11+
- Source code lives under `src/sensgreen_simulator/`
- Tests live under `tests/`
- Use type hints and dataclasses where appropriate
- Format with `black`, lint with `ruff`
- Configuration via environment variables or a YAML/JSON config file

## Domain context
- Simulated metrics include: CO₂ (ppm), temperature (°C), humidity (%RH), PM2.5 (µg/m³), TVOC (ppb), and pressure (hPa)
- Each simulated device has a unique device ID and emits readings at a configurable interval
- Output transports: console (default), file (JSONL), MQTT (optional)

## Coding guidelines
- Prefer composition over inheritance
- Keep transport adapters behind a small interface so new transports can be added easily
- Avoid hard-coded secrets; read from env vars
