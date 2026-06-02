"""Door contact (reed switch) simulator.

Models a magnetic door/window contact that emits:

- ``door_state`` (bool): True = open, False = closed (current snapshot)
- ``periodic_open_events`` (int): number of *open* transitions during
  this interval (door went closed→open)
- ``total_open_events`` (int): monotonic cumulative open transitions

Open events are drawn from a Poisson process whose rate is shaped by:

- ``base_open_rate_per_hour`` (when the room is unoccupied) — e.g. a
  cleaner walks through, a draught nudges a balanced door
- ``occupied_open_rate_per_hour`` (per-occupant rate while occupied) —
  e.g. people entering/leaving a meeting room
- a Gaussian daily activity envelope centred on ``activity_peak_hour``
  with width ``activity_width_hours`` (mostly damps night-time events)
- ``weekend_factor`` for Sat/Sun

When an open event fires, the door stays open for a duration drawn from
``open_duration_seconds_mean`` ± its stddev (clamped to ``[1, 3600]``),
then auto-closes. Multiple overlapping opens within an interval bump
the periodic counter but the snapshot stays True until the *last* close.

All parameters live in ``device.metadata`` — nothing is hardcoded.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..models.config import DeviceConfig
from ..models.reading import SensorReading


@dataclass
class _DoorState:
    is_open: bool = False
    open_until: datetime | None = None
    total_open_events: int = 0
    last_ts: datetime | None = None
    rng: random.Random = field(default_factory=random.Random)


class DoorContactSimulator:
    """Reed-switch style door contact.

    Parameters (all optional, read from ``device.metadata``):

    - ``base_open_rate_per_hour`` (float, default 0.2): opens/hr when
      the zone is unoccupied (cleaners, draughts).
    - ``occupied_open_rate_per_hour`` (float, default 1.5): opens per
      *occupant* per hour while the zone is occupied.
    - ``open_duration_seconds_mean`` (float, default 6.0): mean dwell
      time of an open transition.
    - ``open_duration_seconds_std`` (float, default 3.0): stddev of
      dwell time.
    - ``activity_peak_hour`` (float, default 13.0): centre of the daily
      activity envelope.
    - ``activity_width_hours`` (float, default 6.0): width (sigma) of
      the envelope. Larger = flatter day.
    - ``weekend_factor`` (float, default 0.2): multiplier on Sat/Sun.
    - ``initial_total_open_events`` (int, default 0): seed for the
      cumulative counter (e.g. continuing a deployment).
    """

    def __init__(self, device: DeviceConfig, *, seed: int | None = None) -> None:
        if device.type != "door_contact":
            raise ValueError(
                f"DoorContactSimulator requires device.type='door_contact', "
                f"got '{device.type}'"
            )
        meta = device.metadata or {}
        self.device = device

        self.base_open_rate_per_hour = max(0.0, float(meta.get("base_open_rate_per_hour", 0.2)))
        self.occupied_open_rate_per_hour = max(
            0.0, float(meta.get("occupied_open_rate_per_hour", 1.5))
        )
        self.open_duration_seconds_mean = max(
            1.0, float(meta.get("open_duration_seconds_mean", 6.0))
        )
        self.open_duration_seconds_std = max(
            0.0, float(meta.get("open_duration_seconds_std", 3.0))
        )
        self.activity_peak_hour = float(meta.get("activity_peak_hour", 13.0))
        self.activity_width_hours = max(0.1, float(meta.get("activity_width_hours", 6.0)))
        self.weekend_factor = max(0.0, float(meta.get("weekend_factor", 0.2)))

        self._state = _DoorState(
            total_open_events=int(meta.get("initial_total_open_events", 0)),
            rng=random.Random(seed),
        )

    @property
    def state(self) -> _DoorState:
        return self._state

    def sample(self, timestamp: datetime, true_occupancy: int = 0) -> SensorReading:
        if true_occupancy < 0:
            raise ValueError("true_occupancy must be >= 0")
        ts = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
        rng = self._state.rng

        dt_min = self._minutes_since_last(ts)
        envelope = self._activity_envelope(ts)
        weekend = ts.weekday() >= 5
        scale = self.weekend_factor if weekend else 1.0

        # Expected open events over the interval (lambda for Poisson).
        rate_per_hr = (
            self.base_open_rate_per_hour
            + self.occupied_open_rate_per_hour * float(true_occupancy)
        )
        lam = max(0.0, rate_per_hr * (dt_min / 60.0) * envelope * scale)
        new_events = _poisson(rng, lam)

        if new_events > 0:
            self._state.total_open_events += new_events
            # Door is open if any of the newly-fired events is still
            # within its dwell window at ``ts``. We simulate the *last*
            # such event's close time deterministically.
            last_close = ts
            for _ in range(new_events):
                dwell = max(
                    1.0,
                    rng.gauss(
                        self.open_duration_seconds_mean,
                        self.open_duration_seconds_std,
                    ),
                )
                dwell = min(dwell, 3600.0)
                # Event happens at a uniform offset within the interval;
                # close = event_time + dwell, capped at ts + dwell.
                from datetime import timedelta as _td
                close = ts + _td(seconds=dwell)
                if close > last_close:
                    last_close = close
            self._state.open_until = last_close
            self._state.is_open = True
        else:
            # No new event — close if dwell elapsed.
            if self._state.open_until is not None and ts >= self._state.open_until:
                self._state.is_open = False
                self._state.open_until = None

        self._state.last_ts = ts

        return SensorReading(
            device_eui=self.device.device_eui,
            sensor_type="door_contact",
            timestamp=ts,
            data={
                "door_state": bool(self._state.is_open),
                "periodic_open_events": int(new_events),
                "total_open_events": int(self._state.total_open_events),
            },
            metadata={
                "zone_id": self.device.zone_id,
                "name": self.device.name,
                "true_occupancy": int(true_occupancy),
            },
        )

    # -- internals ---------------------------------------------------------

    def _minutes_since_last(self, ts: datetime) -> float:
        if self._state.last_ts is None:
            return 1.0
        return max((ts - self._state.last_ts).total_seconds() / 60.0, 0.0)

    def _activity_envelope(self, ts: datetime) -> float:
        hour_frac = ts.hour + ts.minute / 60.0 + ts.second / 3600.0
        z = (hour_frac - self.activity_peak_hour) / self.activity_width_hours
        return math.exp(-0.5 * z * z)


def _poisson(rng: random.Random, lam: float) -> int:
    """Tiny Knuth Poisson sampler — fine for the small means we use."""
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))
    L = math.exp(-lam)
    k = 0
    p = 1.0
    while True:
        k += 1
        p *= rng.random()
        if p <= L:
            return k - 1


__all__ = ["DoorContactSimulator"]
