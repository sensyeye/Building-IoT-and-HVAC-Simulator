"""Validation report objects + the ``run_validation`` entry point.

The report shape mirrors the spec in ``COPILOT_CONTEXT.md``:

```
{
  "simulation_id": "...",
  "overall_score": 0,
  "scores": {
    "physical_validity": 0,
    "temporal_consistency": 0,
    "cross_sensor_correlation": 0,
    "hierarchical_consistency": 0,
    "scenario_consistency": 0,
    "statistical_realism": 0,
    "demo_usefulness": 0
  },
  "intended_anomalies_detected": [],
  "unintended_inconsistencies": [],
  "findings": [...]
}
```
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Iterable, Literal

from ..models.reading import SensorReading
from .rule_loader import load_rules

Severity = Literal["critical", "warning", "info"]
Category = Literal[
    "physical_validity",
    "temporal_consistency",
    "cross_sensor_correlation",
    "hierarchical_consistency",
    "scenario_consistency",
    "statistical_realism",
    "demo_usefulness",
]
EntityType = Literal["building", "floor", "zone", "room", "device", "metric", "scenario"]


@dataclass
class Finding:
    severity: Severity
    category: Category
    entity_type: EntityType
    entity_id: str
    message: str
    start_timestamp: str | None = None
    end_timestamp: str | None = None
    suggested_fix: str = ""
    intended_anomaly: bool = False  # set during finalisation

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


CATEGORIES: tuple[Category, ...] = (
    "physical_validity",
    "temporal_consistency",
    "cross_sensor_correlation",
    "hierarchical_consistency",
    "scenario_consistency",
    "statistical_realism",
    "demo_usefulness",
)


@dataclass
class ValidationReport:
    simulation_id: str
    overall_score: float = 0.0
    scores: dict[str, float] = field(default_factory=lambda: {c: 100.0 for c in CATEGORIES})
    intended_anomalies_detected: list[dict[str, Any]] = field(default_factory=list)
    unintended_inconsistencies: list[dict[str, Any]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    def extend(self, findings: Iterable[Finding]) -> None:
        for f in findings:
            self.add(f)

    # -- finalisation ------------------------------------------------------

    def finalise(self, *, scenario_ids: set[str], rules: dict[str, Any]) -> None:
        intended_set = set(rules.get("intended_anomaly_scenarios", []))
        penalties = rules.get("severity_penalties", {"critical": 25, "warning": 5, "info": 0})
        weights = rules.get(
            "scoring_weights",
            {c: 1.0 / len(CATEGORIES) for c in CATEGORIES},
        )

        # Mark intended-anomaly findings: any finding whose category is
        # scenario_consistency *and* whose entity_id is an active scenario,
        # OR any finding whose suggested_fix references an active scenario.
        active_intended = scenario_ids & intended_set
        for f in self.findings:
            if (
                f.category == "scenario_consistency"
                and f.entity_id in active_intended
            ):
                f.intended_anomaly = True
            elif any(s in (f.suggested_fix or "") for s in active_intended):
                f.intended_anomaly = True

        # Per-category score = 100 - sum(penalties for *unintended* findings),
        # clipped to [0, 100].
        per_cat: dict[str, float] = {c: 100.0 for c in CATEGORIES}
        for f in self.findings:
            if f.intended_anomaly:
                continue
            penalty = float(penalties.get(f.severity, 0))
            per_cat[f.category] = max(0.0, per_cat[f.category] - penalty)
        self.scores = per_cat

        # Weighted overall.
        total = 0.0
        weight_sum = 0.0
        for cat, score in per_cat.items():
            w = float(weights.get(cat, 0.0))
            total += w * score
            weight_sum += w
        self.overall_score = round(total / weight_sum, 2) if weight_sum else 0.0

        # Bucketed lists for the JSON report.
        self.intended_anomalies_detected = [
            f.to_dict() for f in self.findings if f.intended_anomaly
        ]
        self.unintended_inconsistencies = [
            f.to_dict()
            for f in self.findings
            if not f.intended_anomaly and f.severity in ("critical", "warning")
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "simulation_id": self.simulation_id,
            "overall_score": self.overall_score,
            "scores": self.scores,
            "intended_anomalies_detected": self.intended_anomalies_detected,
            "unintended_inconsistencies": self.unintended_inconsistencies,
            "findings": [f.to_dict() for f in self.findings],
        }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_validation(
    readings: list[SensorReading],
    *,
    simulation_id: str = "sim",
    building: dict[str, Any] | None = None,
    hierarchy: dict[str, Any] | None = None,
    scenarios: list[str] | None = None,
) -> ValidationReport:
    """Run all validators against ``readings`` and return a report.

    Parameters
    ----------
    readings:
        Canonical SensorReading objects (the same ones used for MQTT/CSV).
    building:
        Optional building/zone metadata, e.g. ``{"type": "office",
        "zones": {"z1": {"type": "open_office", "capacity": 25}}}``.
    hierarchy:
        Optional energy/water meter parent→children map, e.g.
        ``{"main_kw": ["hvac_kw", "lighting_kw", "plug_kw"]}`` keyed by
        ``device_eui``.
    scenarios:
        Active scenario IDs (e.g. ``["meeting_room_poor_ventilation"]``).
    """
    # Imports here to avoid circulars during validator construction.
    from .correlation_validator import CorrelationValidator
    from .demo_quality_validator import DemoQualityValidator
    from .hierarchy_validator import HierarchyValidator
    from .physical_validator import PhysicalValidator
    from .scenario_validator import ScenarioValidator
    from .temporal_validator import TemporalValidator

    rules = load_rules("global_consistency_rules")
    scenario_ids = set(scenarios or [])
    ctx = {
        "building": building or {},
        "hierarchy": hierarchy or {},
        "scenarios": scenario_ids,
        "rules": rules,
    }

    report = ValidationReport(simulation_id=simulation_id)
    validators = [
        PhysicalValidator(ctx),
        TemporalValidator(ctx),
        CorrelationValidator(ctx),
        HierarchyValidator(ctx),
        ScenarioValidator(ctx),
        DemoQualityValidator(ctx),
    ]
    for v in validators:
        report.extend(v.validate(readings))

    report.finalise(scenario_ids=scenario_ids, rules=rules)
    return report


def _iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    return ts.isoformat()


__all__ = [
    "CATEGORIES",
    "Finding",
    "Severity",
    "ValidationReport",
    "run_validation",
    "_iso",
]
