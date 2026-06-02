"""Physical validity checks.

Detects impossible values (out of physical_min/max ranges), monotonicity
violations, and structural rules like PM10 ≥ PM2.5.
"""

from __future__ import annotations

from typing import Any

from ..models.reading import SensorReading
from .base_validator import BaseValidator
from .rule_loader import load_rules
from .validation_report import Finding, _iso


# Map sensor_type -> rule file stem.
_RULE_FILES: dict[str, str] = {
    "iaq": "iaq_rules",
    "energy_meter": "energy_rules",
    "people_counter": "people_counter_rules",
    "entry_exit_counter": "people_counter_rules",
    "occupancy_sensor": "occupancy_rules",
    "hvac": "hvac_rules",
    "device_health": "device_health_rules",
}


class PhysicalValidator(BaseValidator):
    name = "physical"

    def validate(self, readings: list[SensorReading]) -> list[Finding]:
        findings: list[Finding] = []
        rules_cache: dict[str, dict[str, Any]] = {}

        # Group by device for monotonicity checks.
        per_device = self.group_by_device(readings)
        for device_id, series in per_device.items():
            sensor_type = series[0].sensor_type
            stem = _RULE_FILES.get(sensor_type)
            if not stem:
                continue
            if stem not in rules_cache:
                try:
                    rules_cache[stem] = load_rules(stem)
                except FileNotFoundError:
                    continue
            rules = rules_cache[stem]
            metric_rules = rules.get("metrics", {})
            findings.extend(self._check_series(device_id, series, metric_rules))

        return findings

    # ---- per-device series check ----------------------------------------

    def _check_series(
        self,
        device_id: str,
        series: list[SensorReading],
        metric_rules: dict[str, Any],
    ) -> list[Finding]:
        findings: list[Finding] = []
        # Track previous values for monotonicity.
        prev: dict[str, float] = {}

        for r in series:
            for metric, value in r.data.items():
                rule = metric_rules.get(metric)
                if not rule:
                    continue

                # Range checks --------------------------------------------
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    if "physical_min" in rule and value < rule["physical_min"]:
                        findings.append(
                            self._mk_finding(
                                "critical",
                                device_id,
                                metric,
                                f"{metric}={value} below physical_min={rule['physical_min']}",
                                r,
                                rule,
                            )
                        )
                    if "physical_max" in rule and value > rule["physical_max"]:
                        findings.append(
                            self._mk_finding(
                                "critical",
                                device_id,
                                metric,
                                f"{metric}={value} above physical_max={rule['physical_max']}",
                                r,
                                rule,
                            )
                        )

                # PM10 ≥ PM2.5 -------------------------------------------
                must_be_ge = rule.get("must_be_ge")
                if must_be_ge and must_be_ge in r.data:
                    other = r.data[must_be_ge]
                    if isinstance(other, (int, float)) and isinstance(value, (int, float)):
                        if value < other:
                            findings.append(
                                Finding(
                                    severity="warning",
                                    category="physical_validity",
                                    entity_type="metric",
                                    entity_id=f"{device_id}/{metric}",
                                    message=f"{metric}={value} < {must_be_ge}={other}",
                                    start_timestamp=_iso(r.timestamp),
                                    end_timestamp=_iso(r.timestamp),
                                    suggested_fix=(
                                        f"Ensure generator keeps {metric} >= {must_be_ge}."
                                    ),
                                )
                            )

                # Monotonicity -------------------------------------------
                if metric in prev and isinstance(value, (int, float)):
                    last = prev[metric]
                    if rule.get("monotonic_non_decreasing") and value < last:
                        scenario = rule.get("allow_decrease_with_scenario")
                        if not (scenario and self.has_scenario(scenario)):
                            findings.append(
                                self._mk_finding(
                                    "critical",
                                    device_id,
                                    metric,
                                    f"{metric} decreased ({last}->{value}); expected non-decreasing",
                                    r,
                                    rule,
                                    suggested_fix=(
                                        f"If intentional, enable scenario '{scenario}'."
                                        if scenario
                                        else "Investigate generator for decreasing cumulative metric."
                                    ),
                                )
                            )
                    if rule.get("monotonic_non_increasing") and value > last + 1e-9:
                        scenario = rule.get("allow_increase_with_scenario")
                        if not (scenario and self.has_scenario(scenario)):
                            findings.append(
                                self._mk_finding(
                                    "warning",
                                    device_id,
                                    metric,
                                    f"{metric} increased ({last}->{value}); expected non-increasing",
                                    r,
                                    rule,
                                    suggested_fix=(
                                        f"If intentional, enable scenario '{scenario}'."
                                        if scenario
                                        else "Investigate spurious increases (e.g. battery jumps)."
                                    ),
                                )
                            )
                prev[metric] = float(value) if isinstance(value, (int, float)) else prev.get(metric, 0.0)

        return findings

    # ---- helpers --------------------------------------------------------

    @staticmethod
    def _mk_finding(
        severity: str,
        device_id: str,
        metric: str,
        message: str,
        reading: SensorReading,
        rule: dict[str, Any],
        suggested_fix: str = "",
    ) -> Finding:
        return Finding(
            severity=severity,  # type: ignore[arg-type]
            category="physical_validity",
            entity_type="metric",
            entity_id=f"{device_id}/{metric}",
            message=message,
            start_timestamp=_iso(reading.timestamp),
            end_timestamp=_iso(reading.timestamp),
            suggested_fix=suggested_fix
            or "Review generator output and metric rule thresholds.",
        )


__all__ = ["PhysicalValidator"]
