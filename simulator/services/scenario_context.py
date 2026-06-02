"""Shared environment / occupancy context for the simulation runners.

The IAQ, energy, and people simulators all need an *occupancy* signal
per zone, plus the energy simulator wants outdoor temperature. This
module gives runners a single place to get those values for any
``timestamp``.

Defaults are deliberately simple but can be overridden per zone via the
``ZoneConfig.metadata`` block:

```yaml
zones:
  - id: zone-open-space
    capacity: 25
    metadata:
      schedule_kind: open_office  # one of: open_office | meeting_room | always_on | empty
      business_hour_start: 8
      business_hour_end: 18
      weekend_factor: 0.05
```
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..models.config import BuildingConfig, ZoneConfig
from ..scenarios.causal import CausalScenario, apply_scenarios_to_zone
from ..sensors import ZoneState
from ..sensors.zone_state import make_room_rng


@dataclass
class OccupancyScheduler:
    """Compute expected occupancy for one zone at a given timestamp."""

    zone: ZoneConfig
    tz: ZoneInfo
    schedule_kind: str = "open_office"
    business_hour_start: int = 8
    business_hour_end: int = 18
    weekend_factor: float = 0.05

    @classmethod
    def from_zone(cls, zone: ZoneConfig, tz: ZoneInfo) -> "OccupancyScheduler":
        meta = zone.metadata or {}
        return cls(
            zone=zone,
            tz=tz,
            schedule_kind=str(meta.get("schedule_kind", "open_office")),
            business_hour_start=int(meta.get("business_hour_start", 8)),
            business_hour_end=int(meta.get("business_hour_end", 18)),
            weekend_factor=float(meta.get("weekend_factor", 0.05)),
        )

    def occupancy_at(self, ts: datetime) -> int:
        capacity = max(int(self.zone.capacity or 10), 1)
        local = ts.astimezone(self.tz)
        weekend = local.weekday() >= 5
        scale = self.weekend_factor if weekend else 1.0
        h = local.hour + local.minute / 60.0

        kind = self.schedule_kind
        if kind == "always_on":
            ratio = 0.6
        elif kind == "empty":
            ratio = 0.0
        elif kind == "meeting_room":
            # Sharp peaks at 10:00 and 14:00.
            ratio = max(
                _gaussian_pulse(h, 10.0, 0.6),
                _gaussian_pulse(h, 14.0, 0.6),
            )
            ratio *= 0.9
        else:  # open_office
            if self.business_hour_start <= h < self.business_hour_end:
                mid = (self.business_hour_start + self.business_hour_end) / 2.0
                half = max((self.business_hour_end - self.business_hour_start) / 2.0, 1.0)
                x = (h - mid) / half
                ratio = max(0.0, math.cos(0.5 * math.pi * x))
                ratio = 0.4 + 0.55 * ratio  # baseline 0.4, peak 0.95
            else:
                ratio = 0.0

        return max(0, int(round(capacity * ratio * scale)))


def _gaussian_pulse(h: float, peak: float, width: float) -> float:
    z = (h - peak) / max(width, 1e-3)
    return math.exp(-0.5 * z * z)


@dataclass
class ScenarioContext:
    """Environmental + occupancy context shared across sensors per tick.

    Built once per simulation run from the building config. Use
    :meth:`update` to refresh the per-zone state for a new timestamp.
    """

    building: BuildingConfig
    tz: ZoneInfo
    schedulers: dict[str, OccupancyScheduler]
    zone_states: dict[str, ZoneState]
    outdoor_min_c: float = 5.0
    outdoor_max_c: float = 28.0
    outdoor_peak_hour: float = 15.0
    last_update_ts: datetime | None = None
    # P11.3 — optional causal scenarios applied each tick before physics.
    causal_scenarios: tuple[CausalScenario, ...] = ()
    last_fired_rules: dict[str, list[str]] = field(default_factory=dict)
    _zone_room_types: dict[str, str | None] = field(default_factory=dict)

    @classmethod
    def from_building(
        cls,
        building: BuildingConfig,
        *,
        outdoor_min_c: float = 5.0,
        outdoor_max_c: float = 28.0,
        causal_scenarios: tuple[CausalScenario, ...] = (),
    ) -> "ScenarioContext":
        try:
            tz = ZoneInfo(building.timezone)
        except Exception:
            tz = ZoneInfo("UTC")
        schedulers = {z.id: OccupancyScheduler.from_zone(z, tz) for z in building.zones}
        zone_states = {
            z.id: ZoneState(
                zone_id=z.id,
                capacity=int(z.capacity or 10),
                volume_m3=float((z.area_m2 or 30.0) * 2.7),
                rng=make_room_rng(building.id, z.id),
            )
            for z in building.zones
        }
        room_types = {z.id: z.room_type for z in building.zones}
        return cls(
            building=building,
            tz=tz,
            schedulers=schedulers,
            zone_states=zone_states,
            outdoor_min_c=outdoor_min_c,
            outdoor_max_c=outdoor_max_c,
            causal_scenarios=tuple(causal_scenarios),
            _zone_room_types=room_types,
        )

    def update(self, ts: datetime) -> None:
        """Refresh per-zone occupancy + advance the room physics for this tick.

        P10.4: each call now also steps every ``ZoneState`` forward by
        the wall-clock interval since the previous ``update``. The
        first call seeds ``last_update_ts`` but does not advance physics
        (no dt is known yet) — it only sets the occupancy baseline so
        the next tick starts from a sensible state.
        """
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        outdoor_c = self.outdoor_temperature(ts)
        if self.last_update_ts is None:
            dt_min = 0.0
        else:
            dt_min = max((ts - self.last_update_ts).total_seconds() / 60.0, 0.0)

        for zone_id, sched in self.schedulers.items():
            state = self.zone_states[zone_id]
            state.occupancy = sched.occupancy_at(ts)
            # P11.3: apply causal scenario effects *before* physics so
            # this tick's air-exchange / setpoint already reflects them.
            if self.causal_scenarios:
                fired = apply_scenarios_to_zone(
                    self.causal_scenarios,
                    ts=ts, tz=self.tz,
                    zone_id=zone_id,
                    room_type=self._zone_room_types.get(zone_id),
                    zone=state,
                )
                if fired:
                    self.last_fired_rules[zone_id] = fired
                else:
                    self.last_fired_rules.pop(zone_id, None)
            if dt_min > 0:
                state.step(dt_min, outdoor_c=outdoor_c)
        self.last_update_ts = ts

    def outdoor_temperature(self, ts: datetime) -> float:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        local = ts.astimezone(self.tz)
        h = local.hour + local.minute / 60.0
        # Smooth diurnal sinusoid peaking at outdoor_peak_hour.
        amp = (self.outdoor_max_c - self.outdoor_min_c) / 2.0
        mid = (self.outdoor_max_c + self.outdoor_min_c) / 2.0
        return mid + amp * math.cos(2 * math.pi * (h - self.outdoor_peak_hour) / 24.0)

    def zone_state(self, zone_id: str) -> ZoneState:
        return self.zone_states[zone_id]

    def occupancy(self, zone_id: str) -> int:
        return self.zone_states[zone_id].occupancy

    def capacity(self, zone_id: str) -> int:
        return max(self.zone_states[zone_id].capacity, 1)


__all__ = ["OccupancyScheduler", "ScenarioContext"]
