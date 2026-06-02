# Sensgreen Sensor Simulator - Copilot Context

This project is a configurable sensor simulation engine for Sensgreen demo accounts.

The simulator has two main modes:

1. Live mode:
   - Generates realistic sensor readings.
   - Publishes readings to the Sensgreen MQTT Broker.
   - Uses Sensgreen MQTT payload format:
     {
       "deviceEui": "...",
       "timestamp": 1772445600000,
       "data": {
         "temperature": 23.4,
         "humidity": 55.2
       }
     }

2. Historical mode:
   - Generates historical sensor data.
   - Exports CSV files that can be imported into Sensgreen database.
   - Main export format is readings_long.csv.

The simulator must support:
- IAQ sensors
- Energy meters
- Occupancy sensors
- Entry/exit people counters
- HVAC virtual points
- Device health metrics

Core design principles:
- Generate one canonical internal reading first.
- Convert internal readings to MQTT payloads or CSV rows using output adapters.
- Do not duplicate simulation logic in output adapters.
- Use config-driven behavior.
- Use realistic relationships between occupancy, CO2, energy, HVAC, and people counting.
- Include validation checks for physical validity, temporal consistency, correlation, hierarchy consistency, and scenario consistency.

Important:
- Do not hardcode demo data inside sensor classes.
- Keep code modular and testable.
- Prefer simple Python classes and dataclasses.
- Add type hints.
- Add unit tests for each module.