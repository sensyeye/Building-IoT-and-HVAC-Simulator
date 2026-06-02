"""Hierarchical consistency checks.

Validates that energy / water / chilled-water main meters approximately
equal the sum of their sub-meters within a configured residual band.

Hierarchy is provided in ``ctx['hierarchy']`` as a mapping of
``parent_device_eui -> [child_device_eui, ...]``.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from statistics import fmean
from typing import Any

from ..models.reading import SensorReading
from .base_validator import BaseValidator
from .rule_loader import load_rules
from .validation_report import Finding, _iso


def _bucket_minute(ts) -> Any:
    return ts.replace(second=0, microsecond=0)


class HierarchyValidator(BaseValidator):
    name = "hierarchy"

    def validate(self, readings: list[SensorReading]) -> list[Finding]:
        hierarchy: dict[str, list[str]] = self.ctx.get("hierarchy", {}) or {}
        if not hierarchy:
            return []

        rules = load_rules("energy_rules").get("hierarchy", {})
        residual_min = float(rules.get("residual_min", -0.10))
        residual_max = float(rules.get("residual_max", 0.30))

        # Index active_power per device per minute bucket.
        power_idx: dict[str, dict[Any, float]] = defaultdict(dict)
        for r in readings:
            if r.sensor_type != "energy_meter":
                continue
            if "active_power" in r.data:
                power_idx[r.device_eui][_bucket_minute(r.timestamp)] = float(
                    r.data["active_power"]
                )

        findings: list[Finding] = []
        for parent, children in hierarchy.items():
            parent_series = power_idx.get(parent, {})
            if not parent_series:
                continue

            ratios: list[float] = []
            child_count = 0
            for ts, parent_kw in parent_series.items():
                child_sum = 0.0
                seen = 0
                for child in children:
                    cv = power_idx.get(child, {}).get(ts)
                    if cv is not None:
                        child_sum += cv
                        seen += 1
                if seen == 0:
                    continue
                child_count = max(child_count, seen)
                if parent_kw <= 0:
                    continue
                # residual = (parent - children) / parent
                ratios.append((parent_kw - child_sum) / parent_kw)

            if not ratios:
                continue
            mean_ratio = fmean(ratios)
            if mean_ratio < residual_min:
                findings.append(
                    Finding(
                        severity="critical",
                        category="hierarchical_consistency",
                        entity_type="device",
                        entity_id=parent,
                        message=(
                            f"Main meter '{parent}' lower than sum of children "
                            f"(mean residual={mean_ratio:.2%}, min allowed={residual_min:.0%})"
                        ),
                        suggested_fix=(
                            "Children sum exceeds parent — check submeter scaling or units."
                        ),
                    )
                )
            elif mean_ratio > residual_max:
                findings.append(
                    Finding(
                        severity="warning",
                        category="hierarchical_consistency",
                        entity_type="device",
                        entity_id=parent,
                        message=(
                            f"Main meter '{parent}' has large unexplained residual "
                            f"(mean={mean_ratio:.2%}, max allowed={residual_max:.0%})"
                        ),
                        suggested_fix=(
                            "Add missing submeters (other_kw) or tighten residual rule."
                        ),
                    )
                )

        return findings


__all__ = ["HierarchyValidator"]
