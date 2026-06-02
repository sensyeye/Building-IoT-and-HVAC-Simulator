"""Demo usefulness checks.

A dataset can be physically valid yet boring. This validator flags
demos that lack the variety needed for sales accounts, dashboards,
AI reports, and Alarm Engine testing.

A *good* demo dataset should include at least one of each:
  - normal baseline period
  - IAQ issue (e.g. CO2 spike)
  - energy waste / unusual energy event
  - clear occupancy pattern
  - device health / connectivity event
  - cross-sensor story (occupancy ↔ CO2, outdoor ↔ HVAC, etc.)
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..models.reading import SensorReading
from .base_validator import BaseValidator
from .validation_report import Finding


class DemoQualityValidator(BaseValidator):
    name = "demo_quality"

    def validate(self, readings: list[SensorReading]) -> list[Finding]:
        if not readings:
            return [self._missing("dataset is empty")]

        co2_values = [r.data["co2"] for r in readings
                      if r.sensor_type == "iaq" and "co2" in r.data]
        power_values = [float(r.data["active_power"]) for r in readings
                        if r.sensor_type == "energy_meter" and "active_power" in r.data]
        occupancies = [r.metadata.get("occupancy") for r in readings if r.metadata]
        health_signals = [r for r in readings if r.sensor_type == "device_health"]

        findings: list[Finding] = []

        # IAQ issue present?
        if not co2_values or max(co2_values) < 900:
            findings.append(self._missing(
                "no IAQ issue detected (max CO2 < 900 ppm)",
                suggested_fix="Add a meeting_room_poor_ventilation scenario.",
            ))

        # Energy variation?
        if power_values:
            spread = max(power_values) - min(power_values)
            if spread < 0.1:
                findings.append(self._missing(
                    "energy is essentially flat (no day/night variation)",
                    suggested_fix="Apply schedules or after_hours_energy_waste scenario.",
                ))
        else:
            findings.append(self._missing(
                "no energy meter readings present",
                suggested_fix="Add at least one energy_meter device.",
            ))

        # Occupancy pattern?
        non_null = [o for o in occupancies if o is not None]
        if not non_null or (max(non_null) - min(non_null)) < 1:
            findings.append(self._missing(
                "no clear occupancy pattern (range too small)",
                suggested_fix="Drive occupancy from a daily schedule.",
            ))

        # Device health story?
        if not health_signals and "gateway_outage" not in self.scenarios_active():
            findings.append(self._missing(
                "no device health metrics (RSSI/SNR/battery) — Alarm Engine demo will be thin",
                suggested_fix="Emit periodic device_health readings, or enable a connectivity scenario.",
            ))

        # Cross-sensor story?
        zones_with_co2 = {
            r.metadata.get("zone_id") for r in readings
            if r.sensor_type == "iaq" and "co2" in r.data and r.metadata.get("zone_id")
        }
        if not zones_with_co2:
            findings.append(self._missing(
                "no zone-tagged CO2 readings — cross-sensor stories impossible",
                suggested_fix="Populate metadata.zone_id and metadata.occupancy on IAQ readings.",
            ))

        return findings

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _missing(msg: str, suggested_fix: str = "") -> Finding:
        return Finding(
            severity="warning",
            category="demo_usefulness",
            entity_type="building",
            entity_id="<dataset>",
            message=msg,
            suggested_fix=suggested_fix
            or "Enrich the scenario configuration to make the demo more compelling.",
        )


__all__ = ["DemoQualityValidator"]
