"""Scenario consistency checks.

For each *active* scenario, verify the dataset shows the expected
fingerprint. Findings here become "intended_anomalies_detected" entries
in the final report (because the scenario is enabled on purpose) — but
if the scenario is configured and **not** found, that is a real
generation bug and is reported as a warning.
"""

from __future__ import annotations

from typing import Iterable

from ..models.reading import SensorReading
from .base_validator import BaseValidator
from .validation_report import Finding, _iso


class ScenarioValidator(BaseValidator):
    name = "scenario"

    def validate(self, readings: list[SensorReading]) -> list[Finding]:
        active = self.scenarios_active()
        if not active:
            return []

        findings: list[Finding] = []
        for scenario in active:
            handler = getattr(self, f"_check_{scenario}", None)
            if handler is None:
                # Unknown scenario: emit an info-level finding, do not penalise.
                findings.append(
                    Finding(
                        severity="info",
                        category="scenario_consistency",
                        entity_type="scenario",
                        entity_id=scenario,
                        message=f"No validator implemented for scenario '{scenario}' (skipped).",
                    )
                )
                continue
            findings.extend(handler(readings))
        return findings

    # -- scenario checks ---------------------------------------------------

    def _check_meeting_room_poor_ventilation(
        self, readings: list[SensorReading]
    ) -> list[Finding]:
        co2_max = max(
            (r.data["co2"] for r in readings
             if r.sensor_type == "iaq" and "co2" in r.data),
            default=None,
        )
        if co2_max is None or co2_max < 1000:
            return [
                Finding(
                    severity="warning",
                    category="scenario_consistency",
                    entity_type="scenario",
                    entity_id="meeting_room_poor_ventilation",
                    message=(
                        f"Scenario configured but max CO2 only {co2_max} ppm "
                        f"(expected ≥ 1000)."
                    ),
                    suggested_fix="Increase occupancy or reduce ventilation in meeting room.",
                )
            ]
        return [
            Finding(
                severity="info",
                category="scenario_consistency",
                entity_type="scenario",
                entity_id="meeting_room_poor_ventilation",
                message=f"Scenario fingerprint detected: max CO2={co2_max:.0f} ppm.",
            )
        ]

    def _check_after_hours_energy_waste(
        self, readings: list[SensorReading]
    ) -> list[Finding]:
        late_high = any(
            r.sensor_type == "energy_meter"
            and "active_power" in r.data
            and float(r.data["active_power"]) > 1.0
            and r.timestamp.hour in range(0, 6)
            for r in readings
        )
        if not late_high:
            return [
                Finding(
                    severity="warning",
                    category="scenario_consistency",
                    entity_type="scenario",
                    entity_id="after_hours_energy_waste",
                    message="Scenario configured but no after-hours energy spike found.",
                    suggested_fix="Inject sustained kW between 00:00 and 06:00.",
                )
            ]
        return [
            Finding(
                severity="info",
                category="scenario_consistency",
                entity_type="scenario",
                entity_id="after_hours_energy_waste",
                message="Scenario fingerprint detected: energy use during 00:00-06:00.",
            )
        ]

    def _check_meter_reset(self, readings: list[SensorReading]) -> list[Finding]:
        # Just acknowledge — physical validator will skip monotonicity for it.
        return [
            Finding(
                severity="info",
                category="scenario_consistency",
                entity_type="scenario",
                entity_id="meter_reset",
                message="Scenario active: monotonicity violations on cumulative meters allowed.",
            )
        ]

    def _check_overcrowding(self, readings: list[SensorReading]) -> list[Finding]:
        # Acknowledged so capacity overflows don't penalise the score.
        return [
            Finding(
                severity="info",
                category="scenario_consistency",
                entity_type="scenario",
                entity_id="overcrowding",
                message="Scenario active: occupancy may exceed zone capacity.",
            )
        ]

    def _check_gateway_outage(self, readings: list[SensorReading]) -> list[Finding]:
        return [
            Finding(
                severity="info",
                category="scenario_consistency",
                entity_type="scenario",
                entity_id="gateway_outage",
                message="Scenario active: simultaneous device silence is expected.",
            )
        ]

    def _check_single_sensor_offline(
        self, readings: list[SensorReading]
    ) -> list[Finding]:
        return [
            Finding(
                severity="info",
                category="scenario_consistency",
                entity_type="scenario",
                entity_id="single_sensor_offline",
                message="Scenario active: one device may stop reporting.",
            )
        ]


__all__ = ["ScenarioValidator"]
