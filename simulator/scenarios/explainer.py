"""Scenario → reading explainer.

When a live session ticks, we want each reading shown in the dashboard
to be annotated with the scenario(s) that explain it. The explanation
is intentionally *static* and *deterministic*: it is not derived from
the actual numeric value, only from the (scenario, sensor_type, channel)
triple. This is enough for the dashboard's "why is CO₂ so high?" UX:

* Scenarios are intent labels in this codebase — they are not the
  literal cause of every digit. The simulator's sensor models do not
  yet ingest scenario state, so we cannot say "this exact CO₂ value
  came from this scenario function".
* But the *catalog* tells us which sensor channels each scenario is
  designed to influence, and that mapping is all the operator needs to
  triage a reading.

The data is hand-curated, lives next to the scenario catalog, and is
covered by tests so that adding a scenario without updating the
explainer fails loudly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping

from .catalog import Scenario, get_scenario, list_scenarios


# ---------------------------------------------------------------------------
# Explanation table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioImpact:
    """How a scenario is expected to be visible in a reading."""

    sensor_types: tuple[str, ...]  # which simulator sensor_type ids it touches
    channels: tuple[str, ...]      # which data keys to highlight (e.g. "co2_ppm")
    why: str                       # short human-readable explanation


# Each scenario id maps to ONE impact descriptor. Scenarios that affect
# nothing concrete (e.g. pure network outages) still get an entry so the
# UI can render a badge with the right text.
_IMPACTS: dict[str, ScenarioImpact] = {
    "meeting_room_poor_ventilation": ScenarioImpact(
        sensor_types=("iaq",),
        channels=("co2_ppm",),
        why="CO₂ is elevated because this zone is under-ventilated during occupied meetings.",
    ),
    "high_occupancy_lobby_event": ScenarioImpact(
        sensor_types=("iaq", "entry_exit_counter"),
        channels=("co2_ppm", "entries", "exits", "occupancy"),
        why="Lobby occupancy spike — expect entry/exit bursts and a CO₂ rise.",
    ),
    "overcrowding": ScenarioImpact(
        sensor_types=("iaq", "entry_exit_counter"),
        channels=("co2_ppm", "temperature_c", "occupancy"),
        why="Open-office density above design — CO₂, temperature and counts run hot.",
    ),
    "after_hours_energy_waste": ScenarioImpact(
        sensor_types=("energy_meter",),
        channels=("active_power_w", "active_energy_kwh"),
        why="Plug / lighting load is still drawing power after closing hours.",
    ),
    "meter_reset": ScenarioImpact(
        sensor_types=("energy_meter",),
        channels=("active_energy_kwh",),
        why="Cumulative energy counter rolled back to zero (firmware/calibration event).",
    ),
    "hvac_inefficiency_fault": ScenarioImpact(
        sensor_types=("iaq", "hvac"),
        channels=("temperature_c", "setpoint_c"),
        why="Setpoint vs. actual temperature is drifting — HVAC is recovering slowly.",
    ),
    "fresh_air_damper_stuck": ScenarioImpact(
        sensor_types=("iaq",),
        channels=("co2_ppm",),
        why="Fresh-air damper is stuck shut — CO₂ stays high after occupancy drops.",
    ),
    "cleaning_voc_spike": ScenarioImpact(
        sensor_types=("iaq",),
        channels=("tvoc_ppb",),
        why="TVOC spike from cleaning chemicals (off-hours).",
    ),
    "outdoor_pm_event": ScenarioImpact(
        sensor_types=("iaq",),
        channels=("pm2_5_ug_m3", "pm10_ug_m3"),
        why="Outdoor PM event — fine-particle reading mirrors what's outside.",
    ),
    "single_sensor_offline": ScenarioImpact(
        sensor_types=("iaq", "energy_meter", "entry_exit_counter"),
        channels=(),
        why="One device is intentionally silent in this window — gaps are expected.",
    ),
    "gateway_outage": ScenarioImpact(
        sensor_types=("iaq", "energy_meter", "entry_exit_counter"),
        channels=(),
        why="Gateway outage — all devices behind this gateway are silent.",
    ),
    "counter_drift": ScenarioImpact(
        sensor_types=("entry_exit_counter",),
        channels=("entries", "exits"),
        why="People counter is drifting — entry/exit totals diverge over time.",
    ),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_impact(scenario_id: str) -> ScenarioImpact | None:
    """Return the curated impact descriptor for ``scenario_id`` (or None)."""
    return _IMPACTS.get(scenario_id)


def known_impacts() -> set[str]:
    return set(_IMPACTS.keys())


def explain_reading(
    sensor_type: str,
    active_scenarios: Iterable[str],
    *,
    scenarios_by_id: Mapping[str, Scenario] | None = None,
) -> list[dict[str, str]]:
    """Return scenario annotations relevant to a ``sensor_type`` reading.

    Each entry is a small dict suitable for serialising over SSE::

        {"id": "...", "name": "...", "category": "...", "why": "..."}

    A scenario is included only when its impact mentions ``sensor_type``
    in :attr:`ScenarioImpact.sensor_types`. Scenarios with no impact
    record (e.g. an experimental scenario added without an explainer)
    are silently skipped — the test suite guarantees the catalog stays
    in sync.
    """
    out: list[dict[str, str]] = []
    for sid in active_scenarios:
        impact = _IMPACTS.get(sid)
        if impact is None:
            continue
        if sensor_type not in impact.sensor_types:
            continue
        meta = (
            scenarios_by_id.get(sid) if scenarios_by_id else get_scenario(sid)
        )
        if meta is None:
            continue
        out.append({
            "id": sid,
            "name": meta.name,
            "category": meta.category,
            "why": impact.why,
        })
    return out


def active_scenarios_at(
    ts: datetime, assignments: Iterable[Mapping[str, object]]
) -> list[str]:
    """Resolve which scenario ids are active at ``ts``.

    ``assignments`` is the per-project list stored under
    ``data/projects/<id>.scenarios.json`` — each entry looks like
    ``{"id": str, "enabled": bool, "start"?: iso8601, "end"?: iso8601}``.

    Open-ended windows are supported: missing ``start`` means "from
    forever ago", missing ``end`` means "until forever". A disabled
    assignment is never active even if its window covers ``ts``.
    """
    ts_utc = _to_utc(ts)
    out: list[str] = []
    seen: set[str] = set()
    for a in assignments:
        if not a.get("enabled"):
            continue
        sid = str(a.get("id") or "").strip()
        if not sid or sid in seen:
            continue
        start = _parse_iso(a.get("start"))
        end = _parse_iso(a.get("end"))
        if start is not None and ts_utc < start:
            continue
        if end is not None and ts_utc > end:
            continue
        out.append(sid)
        seen.add(sid)
    return out


def active_scenario_assignments_at(
    ts: datetime, assignments: Iterable[Mapping[str, object]]
) -> list[Mapping[str, object]]:
    """Like :func:`active_scenarios_at` but returns the full assignment
    records (including any targeting fields) instead of just ids.

    Used by the live session to honour ``target_hvac_zone_id`` /
    ``target_zone_ids`` while keeping the legacy id-only API intact.
    Duplicates by id are filtered, keeping the first occurrence.
    """
    ts_utc = _to_utc(ts)
    out: list[Mapping[str, object]] = []
    seen: set[str] = set()
    for a in assignments:
        if not a.get("enabled"):
            continue
        sid = str(a.get("id") or "").strip()
        if not sid or sid in seen:
            continue
        start = _parse_iso(a.get("start"))
        end = _parse_iso(a.get("end"))
        if start is not None and ts_utc < start:
            continue
        if end is not None and ts_utc > end:
            continue
        out.append(a)
        seen.add(sid)
    return out


def resolve_assignment_zone_targets(
    assignment: Mapping[str, object],
    zone_to_hvac: Mapping[str, str | None] | None = None,
) -> set[str] | None:
    """Return the set of zone ids an assignment applies to.

    ``None`` means "no targeting — applies to every zone" (legacy
    behaviour). An empty set means "targeting is set but resolves to no
    zones" — callers should treat that as "applies to nothing".

    ``zone_to_hvac`` maps each ``zone_id`` to its ``hvac_zone_id`` (or
    ``None``). It is required to expand a ``target_hvac_zone_id`` into
    the set of served zones.
    """
    target_zones_raw = assignment.get("target_zone_ids")
    target_hvac = assignment.get("target_hvac_zone_id")
    has_zone_list = isinstance(target_zones_raw, list) and any(
        str(z or "").strip() for z in target_zones_raw
    )
    has_hvac = bool(str(target_hvac or "").strip())
    if not has_zone_list and not has_hvac:
        return None
    out: set[str] = set()
    if has_zone_list:
        assert isinstance(target_zones_raw, list)
        for z in target_zones_raw:
            zs = str(z or "").strip()
            if zs:
                out.add(zs)
    if has_hvac and zone_to_hvac:
        hvac_id = str(target_hvac).strip()
        for zid, hid in zone_to_hvac.items():
            if hid is not None and str(hid) == hvac_id:
                out.add(zid)
    return out


def _parse_iso(value: object) -> datetime | None:
    if value in (None, "", False):
        return None
    if isinstance(value, datetime):
        return _to_utc(value)
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    # Accept both "2025-01-01T08:00" and full ISO with timezone.
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _to_utc(dt)


def _to_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


__all__ = [
    "ScenarioImpact",
    "active_scenario_assignments_at",
    "active_scenarios_at",
    "explain_reading",
    "get_impact",
    "known_impacts",
    "resolve_assignment_zone_targets",
]
