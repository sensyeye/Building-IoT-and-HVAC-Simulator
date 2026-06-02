"""HVAC virtual-point simulator.

Emits a small but realistic telemetry frame that mirrors what a typical
BACnet/IP roof-top unit or fancoil controller would publish:

- ``mode`` (str): one of ``auto`` | ``cool`` | ``heat`` | ``off`` | ``fan_only``
- ``setpoint_c`` (float): the *commanded* temperature setpoint
- ``supply_temp_c`` (float): the temperature of the air being delivered
  to the room. Cooler than setpoint in cool mode, warmer in heat mode,
  drifts toward room temperature in fan/off.
- ``fan_speed_pct`` (int 0–100): scales with mode + load
- ``valve_open_pct`` (int 0–100): cooling/heating coil position
- ``ventilation_l_s_per_person`` (float): outdoor-air rate currently
  being supplied

The simulator also **drives** the room: it writes ``hvac_mode``,
``hvac_setpoint_c`` and ``ventilation_l_s_per_person`` onto the shared
:class:`ZoneState` so the next physics tick reflects the command. This
makes the virtual HVAC a true closed-loop actor instead of a passive
metric source.

Scheduling
----------
By default, the unit follows a simple daily schedule:

- 07:00–19:00 weekdays → ``cool`` in summer (outdoor > setpoint+2),
  ``heat`` in winter (outdoor < setpoint−2), ``auto`` otherwise
- Outside business hours → ``standby`` (no setpoint pull, partial vent)
- Weekends → ``standby`` unless ``run_weekends=True``

Everything is overridable via ``device.metadata``.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..models.config import DeviceConfig
from ..models.reading import SensorReading
from .zone_state import ZoneState


VALID_MODES: tuple[str, ...] = ("auto", "cool", "heat", "off", "fan_only", "standby")


@dataclass
class _HvacState:
    last_ts: datetime | None = None
    mode: str = "auto"
    setpoint_c: float = 22.0
    supply_temp_c: float = 22.0
    fan_speed_pct: float = 0.0
    valve_open_pct: float = 0.0
    rng: random.Random = field(default_factory=random.Random)


class HvacVirtualSimulator:
    """Closed-loop HVAC controller + telemetry source.

    Parameters (all optional, via ``device.metadata``):

    - ``setpoint_c`` (float, default 22.0): commanded setpoint when
      cooling/heating. Auto/standby still publish this for the UI.
    - ``cool_threshold_c`` (float, default ``setpoint_c + 2``): outdoor
      temperature above which the unit goes into ``cool``.
    - ``heat_threshold_c`` (float, default ``setpoint_c - 2``): outdoor
      temperature below which the unit goes into ``heat``.
    - ``business_hour_start`` (int, default 7): start of run window.
    - ``business_hour_end`` (int, default 19): end of run window.
    - ``run_weekends`` (bool, default False).
    - ``ventilation_l_s_per_person`` (float, default 8.0): design rate
      delivered to the room when actively running.
    - ``min_supply_temp_c`` (float, default 12.0): coil-leaving
      temperature when fully cooling.
    - ``max_supply_temp_c`` (float, default 38.0): coil-leaving
      temperature when fully heating.
    - ``mode_override`` (str): force a mode regardless of schedule
      (useful for scenarios). Must be in :data:`VALID_MODES`.
    """

    def __init__(self, device: DeviceConfig, *, seed: int | None = None) -> None:
        if device.type != "hvac":
            raise ValueError(
                f"HvacVirtualSimulator requires device.type='hvac', got '{device.type}'"
            )
        meta = device.metadata or {}
        self.device = device

        self.setpoint_c = float(meta.get("setpoint_c", 22.0))
        self.cool_threshold_c = float(meta.get("cool_threshold_c", self.setpoint_c + 2.0))
        self.heat_threshold_c = float(meta.get("heat_threshold_c", self.setpoint_c - 2.0))
        self.business_hour_start = int(meta.get("business_hour_start", 7))
        self.business_hour_end = int(meta.get("business_hour_end", 19))
        self.run_weekends = bool(meta.get("run_weekends", False))
        self.ventilation_l_s_per_person = max(
            0.0, float(meta.get("ventilation_l_s_per_person", 8.0))
        )
        self.min_supply_temp_c = float(meta.get("min_supply_temp_c", 12.0))
        self.max_supply_temp_c = float(meta.get("max_supply_temp_c", 38.0))
        override = meta.get("mode_override")
        self.mode_override = (
            str(override).lower() if override and str(override).lower() in VALID_MODES
            else None
        )

        self._state = _HvacState(
            setpoint_c=self.setpoint_c,
            supply_temp_c=self.setpoint_c,
            rng=random.Random(seed),
        )

    # ------------------------------------------------------------------
    @property
    def state(self) -> _HvacState:
        return self._state

    # ------------------------------------------------------------------
    # Public sampling API
    # ------------------------------------------------------------------
    def sample(
        self,
        timestamp: datetime,
        zone: ZoneState | None,
        *,
        outdoor_c: float,
    ) -> SensorReading:
        """Decide a mode, command the room, return one telemetry frame."""
        ts = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
        s = self._state

        mode = self._decide_mode(ts, outdoor_c, zone)
        s.mode = mode
        s.setpoint_c = self.setpoint_c

        # Supply temperature: head toward the appropriate extreme when
        # actively conditioning, otherwise toward the room (or setpoint
        # if we have no room reference).
        room_temp = (
            zone.temperature_c
            if (zone is not None and zone.temperature_c is not None)
            else self.setpoint_c
        )
        if mode == "cool":
            target_supply = self.min_supply_temp_c
            s.fan_speed_pct = 80.0
            s.valve_open_pct = 75.0
        elif mode == "heat":
            target_supply = self.max_supply_temp_c
            s.fan_speed_pct = 80.0
            s.valve_open_pct = 75.0
        elif mode == "fan_only":
            target_supply = float(room_temp)
            s.fan_speed_pct = 50.0
            s.valve_open_pct = 0.0
        elif mode == "auto":
            target_supply = self.setpoint_c
            s.fan_speed_pct = 40.0
            s.valve_open_pct = 20.0
        else:  # standby / off
            target_supply = float(room_temp)
            s.fan_speed_pct = 0.0
            s.valve_open_pct = 0.0

        # First-order glide of supply temperature toward target.
        s.supply_temp_c += (target_supply - s.supply_temp_c) * 0.4 + s.rng.gauss(0, 0.1)

        # Drive the room so the next physics step reflects this command.
        if zone is not None:
            zone.hvac_mode = mode
            zone.hvac_setpoint_c = self.setpoint_c
            zone.ventilation_l_s_per_person = (
                self.ventilation_l_s_per_person
                if mode in ("cool", "heat", "fan_only", "auto")
                else 0.0
            )

        s.last_ts = ts

        data: dict[str, Any] = {
            "mode": mode,
            "setpoint_c": round(self.setpoint_c, 1),
            "supply_temp_c": round(s.supply_temp_c, 1),
            "fan_speed_pct": int(round(s.fan_speed_pct)),
            "valve_open_pct": int(round(s.valve_open_pct)),
            "ventilation_l_s_per_person": round(self.ventilation_l_s_per_person, 1)
            if mode in ("cool", "heat", "fan_only", "auto") else 0.0,
        }
        return SensorReading(
            device_eui=self.device.device_eui,
            sensor_type="hvac",
            timestamp=ts,
            data=data,
            metadata={
                "zone_id": self.device.zone_id,
                "name": self.device.name,
            },
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _decide_mode(
        self,
        ts: datetime,
        outdoor_c: float,
        zone: ZoneState | None,
    ) -> str:
        if self.mode_override is not None:
            return self.mode_override

        hour = ts.hour + ts.minute / 60.0
        weekday = ts.weekday() < 5
        in_hours = self.business_hour_start <= hour < self.business_hour_end
        running = (weekday and in_hours) or (self.run_weekends and in_hours)

        if not running:
            return "standby"

        if outdoor_c >= self.cool_threshold_c:
            return "cool"
        if outdoor_c <= self.heat_threshold_c:
            return "heat"
        return "auto"


__all__ = ["HvacVirtualSimulator", "VALID_MODES"]
