"""Causal scenarios (P11.3).

A *causal scenario* is a declarative bundle of triggers that mutate
:class:`ZoneState` at specific times of day, on specific weekdays, or
when other conditions hold. Unlike :mod:`micro_events` — which adds
short transient deltas onto IAQ readings — causal scenarios change the
**room's true state** (occupancy, door, HVAC mode, base air load) so
every device sampling that room reflects the same world.

Concepts
--------
- :class:`TimeWindow` selects ticks by hour-of-day and weekday mask.
- :class:`CausalEffect` is a single mutation (set/scale/bump).
- :class:`CausalRule` ties one window + a list of effects to one or
  more zones (by id, by room_type, or "*").
- :class:`CausalScenario` is a named bundle of rules with a friendly
  description for the UI.

Effects are intentionally tiny so they compose:

============================  ====================================
``set_hvac_mode``             Overwrite ``hvac_mode``
``set_hvac_setpoint``         Overwrite ``hvac_setpoint_c``
``scale_occupancy``           Multiply occupancy by a factor
``set_occupancy_floor``       Ensure occupancy ≥ given value
``set_occupancy_cap``         Ensure occupancy ≤ given value
``force_door_open``           Pin door open (or release)
``bump_pm25``                 Additively add to PM2.5 (e.g. dust)
``bump_voc``                  Additively add to VOC (e.g. cleaning)
============================  ====================================

Usage from :class:`ScenarioContext` is opt-in: pass ``scenarios=[…]``
when constructing it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from ..sensors.zone_state import ZoneState


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------
ALL_WEEKDAYS = frozenset(range(7))
WEEKDAYS = frozenset(range(5))
WEEKENDS = frozenset({5, 6})


@dataclass(frozen=True)
class TimeWindow:
    """Half-open window ``[start_hour, end_hour)`` on selected weekdays.

    ``end_hour`` may be < ``start_hour`` to wrap across midnight (e.g.
    22 → 6 means "from 22:00 to 06:00 next morning").
    """

    start_hour: float
    end_hour: float
    weekdays: frozenset[int] = ALL_WEEKDAYS

    def contains(self, ts: datetime, tz: ZoneInfo) -> bool:
        local = ts.astimezone(tz)
        if local.weekday() not in self.weekdays:
            return False
        h = local.hour + local.minute / 60.0
        if self.start_hour <= self.end_hour:
            return self.start_hour <= h < self.end_hour
        # Wraps midnight: in either tail.
        return h >= self.start_hour or h < self.end_hour


@dataclass
class CausalEffect:
    """A single mutation to apply to a :class:`ZoneState`."""

    kind: str
    value: Any = None

    def apply(self, zone: ZoneState) -> None:
        k = self.kind
        v = self.value
        if k == "set_hvac_mode":
            zone.hvac_mode = str(v)
            if v in ("off", "standby"):
                zone.ventilation_l_s_per_person = 0.0
        elif k == "set_hvac_setpoint":
            zone.hvac_setpoint_c = float(v)
        elif k == "scale_occupancy":
            zone.occupancy = max(int(round(zone.occupancy * float(v))), 0)
        elif k == "set_occupancy_floor":
            zone.occupancy = max(zone.occupancy, int(v))
        elif k == "set_occupancy_cap":
            zone.occupancy = min(zone.occupancy, int(v))
        elif k == "force_door_open":
            if bool(v):
                zone.open_door()
            else:
                zone.close_door()
        elif k == "bump_pm25":
            zone.pm25_ug_m3 = min(zone.pm25_ug_m3 + float(v), 500.0)
            zone.pm10_ug_m3 = max(zone.pm10_ug_m3, zone.pm25_ug_m3)
        elif k == "bump_voc":
            zone.voc_mg_m3 = min(zone.voc_mg_m3 + float(v), 10.0)
        # Unknown effect kinds are silently ignored so scenarios stay
        # forward-compatible.


# ---------------------------------------------------------------------------
# Rules & Scenarios
# ---------------------------------------------------------------------------
ZoneFilter = Callable[[str, str | None], bool]


def _zone_filter(
    *,
    zone_ids: Iterable[str] | None = None,
    room_types: Iterable[str] | None = None,
) -> ZoneFilter:
    zone_set = {z for z in zone_ids} if zone_ids else None
    type_set = {t for t in room_types} if room_types else None

    def matches(zid: str, rtype: str | None) -> bool:
        if zone_set is not None and zid not in zone_set:
            return False
        if type_set is not None and (rtype or "") not in type_set:
            return False
        return True

    return matches


@dataclass
class CausalRule:
    """One time-window + effects + zone selector."""

    window: TimeWindow
    effects: tuple[CausalEffect, ...]
    zone_ids: tuple[str, ...] | None = None
    room_types: tuple[str, ...] | None = None
    name: str = ""

    def matches_zone(self, zone_id: str, room_type: str | None) -> bool:
        return _zone_filter(zone_ids=self.zone_ids, room_types=self.room_types)(
            zone_id, room_type
        )


@dataclass
class CausalScenario:
    """A named bundle of rules."""

    id: str
    name: str
    description: str = ""
    rules: tuple[CausalRule, ...] = field(default_factory=tuple)

    def applicable_rules(
        self, ts: datetime, tz: ZoneInfo, zone_id: str, room_type: str | None,
    ) -> list[CausalRule]:
        return [
            r for r in self.rules
            if r.window.contains(ts, tz) and r.matches_zone(zone_id, room_type)
        ]


# ---------------------------------------------------------------------------
# Built-in scenarios
# ---------------------------------------------------------------------------
def morning_rush() -> CausalScenario:
    return CausalScenario(
        id="morning_rush",
        name="Morning rush",
        description=(
            "08:00–09:30 on weekdays — open offices and mall entrances see "
            "a sharp occupancy spike; HVAC ramps to cool/heat."
        ),
        rules=(
            CausalRule(
                name="open_office_spike",
                window=TimeWindow(8.0, 9.5, WEEKDAYS),
                effects=(
                    CausalEffect("scale_occupancy", 1.4),
                    CausalEffect("set_hvac_mode", "cool"),
                ),
                room_types=("open_office", "meeting_room", "mall_entrance"),
            ),
        ),
    )


def lunch_rush() -> CausalScenario:
    return CausalScenario(
        id="lunch_rush",
        name="Lunch rush",
        description=(
            "12:00–13:30 — cafeterias, restaurant kitchens, and mall "
            "entrances spike; kitchens see a VOC bump from cooking."
        ),
        rules=(
            CausalRule(
                name="dining_spike",
                window=TimeWindow(12.0, 13.5, WEEKDAYS),
                effects=(
                    CausalEffect("scale_occupancy", 1.6),
                    CausalEffect("set_hvac_mode", "cool"),
                ),
                room_types=("cafeteria", "mall_entrance", "restaurant_kitchen"),
            ),
            CausalRule(
                name="kitchen_voc_bump",
                window=TimeWindow(11.5, 14.0, WEEKDAYS),
                effects=(
                    CausalEffect("bump_voc", 0.6),
                    CausalEffect("bump_pm25", 4.0),
                ),
                room_types=("restaurant_kitchen",),
            ),
        ),
    )


def cleaning_routine() -> CausalScenario:
    return CausalScenario(
        id="cleaning_routine",
        name="Evening cleaning routine",
        description=(
            "20:00–22:00 — one cleaner in each room, doors briefly pinned "
            "open, dust + cleaning-product VOC bump."
        ),
        rules=(
            CausalRule(
                name="cleaner_present",
                window=TimeWindow(20.0, 22.0, ALL_WEEKDAYS),
                effects=(
                    CausalEffect("set_occupancy_floor", 1),
                    CausalEffect("set_occupancy_cap", 2),
                    CausalEffect("force_door_open", True),
                    CausalEffect("bump_pm25", 2.0),
                    CausalEffect("bump_voc", 0.4),
                ),
                room_types=(
                    "open_office", "meeting_room", "hotel_guest_room",
                    "warehouse_zone",
                ),
            ),
        ),
    )


def night_setback() -> CausalScenario:
    return CausalScenario(
        id="night_setback",
        name="Night setback",
        description=(
            "22:00–06:00 — HVAC drops to standby with a relaxed setpoint, "
            "occupancy clamped to zero in non-hotel rooms."
        ),
        rules=(
            CausalRule(
                name="hvac_setback",
                window=TimeWindow(22.0, 6.0, ALL_WEEKDAYS),
                effects=(
                    CausalEffect("set_hvac_mode", "standby"),
                    CausalEffect("set_hvac_setpoint", 19.0),
                ),
                room_types=(
                    "open_office", "meeting_room", "cafeteria",
                    "mall_entrance", "server_room", "warehouse_zone",
                ),
            ),
            CausalRule(
                name="empty_offices",
                window=TimeWindow(22.0, 6.0, ALL_WEEKDAYS),
                effects=(
                    CausalEffect("set_occupancy_cap", 0),
                ),
                room_types=(
                    "open_office", "meeting_room", "cafeteria",
                    "mall_entrance",
                ),
            ),
        ),
    )


def list_builtin_scenarios() -> list[CausalScenario]:
    return [
        morning_rush(),
        lunch_rush(),
        cleaning_routine(),
        night_setback(),
    ]


def get_builtin_scenario(scenario_id: str) -> CausalScenario | None:
    for s in list_builtin_scenarios():
        if s.id == scenario_id:
            return s
    return None


# ---------------------------------------------------------------------------
# Application helper
# ---------------------------------------------------------------------------
def apply_scenarios_to_zone(
    scenarios: Iterable[CausalScenario],
    *,
    ts: datetime,
    tz: ZoneInfo,
    zone_id: str,
    room_type: str | None,
    zone: ZoneState,
) -> list[str]:
    """Apply every matching effect from every scenario to ``zone``.

    Returns the names of the rules that fired (for debugging /
    explainer surfaces).
    """
    fired: list[str] = []
    for sc in scenarios:
        for rule in sc.applicable_rules(ts, tz, zone_id, room_type):
            for eff in rule.effects:
                eff.apply(zone)
            fired.append(f"{sc.id}:{rule.name}")
    return fired


__all__ = [
    "ALL_WEEKDAYS", "WEEKDAYS", "WEEKENDS",
    "TimeWindow", "CausalEffect", "CausalRule", "CausalScenario",
    "morning_rush", "lunch_rush", "cleaning_routine", "night_setback",
    "list_builtin_scenarios", "get_builtin_scenario",
    "apply_scenarios_to_zone",
]
