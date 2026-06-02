"""Catalog of canned simulation scenarios.

Scenarios are *intent labels* attached to a simulation run. They are
consumed by:

* :func:`simulator.validators.run_validation` — findings tagged with one
  of the ``intended_anomaly_scenarios`` from
  ``simulator/rules/global_consistency_rules.yaml`` are reported as
  intentional anomalies instead of bugs.
* :class:`simulator.services.live_session.LiveSession` — stored in the
  event log so the dashboard can show which scenarios were live when
  data was generated.

The catalog is intentionally a hand-rolled list of dataclasses (not a
database) so it can be edited by anyone who can read Python. New
scenarios should be added here, *not* via the API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Scenario:
    """One canned scenario definition.

    Attributes
    ----------
    id:
        Stable kebab-cased id used by validators and stored in project
        scenario files. Must match
        ``intended_anomaly_scenarios`` when ``intended`` is True.
    name:
        Human-readable label for the dashboard.
    description:
        One-line description shown under the toggle.
    category:
        Grouping for the UI; one of ``"occupancy"``, ``"environment"``,
        ``"energy"``, ``"hvac"``, ``"network"``.
    intended:
        Whether validation should treat related findings as *intentional*
        anomalies. Mirrors the rules-file taxonomy.
    """

    id: str
    name: str
    description: str
    category: str
    intended: bool = True


# The catalog. Keep ids in sync with
# `intended_anomaly_scenarios` in simulator/rules/global_consistency_rules.yaml.
CATALOG: tuple[Scenario, ...] = (
    Scenario(
        id="meeting_room_poor_ventilation",
        name="Meeting room — poor ventilation",
        description="CO₂ climbs above 1500 ppm during occupied meetings.",
        category="environment",
    ),
    Scenario(
        id="high_occupancy_lobby_event",
        name="Lobby event — high occupancy",
        description="Sudden spike of people in the reception/lobby zone.",
        category="occupancy",
    ),
    Scenario(
        id="overcrowding",
        name="Open-office overcrowding",
        description="Density above design target for several hours.",
        category="occupancy",
    ),
    Scenario(
        id="after_hours_energy_waste",
        name="After-hours energy waste",
        description="Plug/lighting load stays on after the building closes.",
        category="energy",
    ),
    Scenario(
        id="meter_reset",
        name="Energy meter reset",
        description="Cumulative active-energy counter rolls back to zero.",
        category="energy",
    ),
    Scenario(
        id="hvac_inefficiency_fault",
        name="HVAC inefficiency fault",
        description="Setpoint vs actual temperature drift; long recovery.",
        category="hvac",
    ),
    Scenario(
        id="fresh_air_damper_stuck",
        name="Fresh-air damper stuck",
        description="CO₂ does not recover after occupancy drops.",
        category="hvac",
    ),
    Scenario(
        id="cleaning_voc_spike",
        name="Cleaning crew — TVOC spike",
        description="Short, sharp TVOC spike outside business hours.",
        category="environment",
    ),
    Scenario(
        id="outdoor_pm_event",
        name="Outdoor PM event",
        description="Brief PM2.5 ingress mirrored across all IAQ devices.",
        category="environment",
    ),
    Scenario(
        id="single_sensor_offline",
        name="Single sensor offline",
        description="One device stops reporting for a defined window.",
        category="network",
    ),
    Scenario(
        id="gateway_outage",
        name="Gateway outage",
        description="All devices on a gateway go silent simultaneously.",
        category="network",
    ),
    Scenario(
        id="counter_drift",
        name="People-counter drift",
        description="Entry/exit counts drift apart by a steady offset.",
        category="occupancy",
    ),
)


_BY_ID: dict[str, Scenario] = {s.id: s for s in CATALOG}


def list_scenarios() -> tuple[Scenario, ...]:
    """Return the full catalog (stable order)."""
    return CATALOG


def get_scenario(scenario_id: str) -> Scenario | None:
    return _BY_ID.get(scenario_id)


def known_ids() -> set[str]:
    return set(_BY_ID.keys())


def filter_known(ids: Iterable[str]) -> list[str]:
    """Return only the ``ids`` that exist in the catalog (preserves order)."""
    out: list[str] = []
    seen: set[str] = set()
    for sid in ids:
        if sid in _BY_ID and sid not in seen:
            out.append(sid)
            seen.add(sid)
    return out


__all__ = [
    "CATALOG",
    "Scenario",
    "filter_known",
    "get_scenario",
    "known_ids",
    "list_scenarios",
]
