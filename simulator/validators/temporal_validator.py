"""Temporal consistency checks.

Detects unrealistic per-step jumps for metrics that have a configured
``max_step_per_minute``. Smooth random walks should pass; sudden spikes
should not.
"""

from __future__ import annotations

from typing import Any

from ..models.reading import SensorReading
from .base_validator import BaseValidator
from .physical_validator import _RULE_FILES
from .rule_loader import load_rules
from .validation_report import Finding, _iso


class TemporalValidator(BaseValidator):
    name = "temporal"

    def validate(self, readings: list[SensorReading]) -> list[Finding]:
        findings: list[Finding] = []
        rules_cache: dict[str, dict[str, Any]] = {}

        for device_id, series in self.group_by_device(readings).items():
            stem = _RULE_FILES.get(series[0].sensor_type)
            if not stem:
                continue
            if stem not in rules_cache:
                try:
                    rules_cache[stem] = load_rules(stem)
                except FileNotFoundError:
                    continue
            metric_rules = rules_cache[stem].get("metrics", {})

            prev_value: dict[str, float] = {}
            prev_ts = None
            for r in series:
                if prev_ts is not None:
                    dt_min = max((r.timestamp - prev_ts).total_seconds() / 60.0, 1e-6)
                else:
                    dt_min = 1.0

                for metric, value in r.data.items():
                    rule = metric_rules.get(metric, {})
                    max_step = rule.get("max_step_per_minute")
                    if max_step is None or not isinstance(value, (int, float)):
                        continue
                    if metric in prev_value:
                        delta = abs(value - prev_value[metric])
                        # Allow a generous slack proportional to elapsed time.
                        if delta > max_step * dt_min * 1.5:
                            findings.append(
                                Finding(
                                    severity="warning",
                                    category="temporal_consistency",
                                    entity_type="metric",
                                    entity_id=f"{device_id}/{metric}",
                                    message=(
                                        f"{metric} jumped {delta:.2f} in {dt_min:.1f} min "
                                        f"(max_step_per_minute={max_step})"
                                    ),
                                    start_timestamp=_iso(prev_ts),
                                    end_timestamp=_iso(r.timestamp),
                                    suggested_fix=(
                                        "Smooth generator output; ensure first-order "
                                        "drift instead of independent random samples."
                                    ),
                                )
                            )
                    prev_value[metric] = float(value)
                prev_ts = r.timestamp

        return findings


__all__ = ["TemporalValidator"]
