"""PIR-style occupancy sensor.

Emits a binary ``occupancy`` flag plus an integer ``occupant_count``
observation. Unlike :class:`PeopleCounterSimulator` (which models a
mmWave/camera headcount with miscounts), this simulator models a much
simpler PIR/ultrasonic detector:

- It samples the *true* zone occupancy supplied by the caller.
- If true occupancy > 0, it (usually) reports ``occupancy=True`` and
  resets a *hold timer*; while the hold timer is active the sensor
  keeps reporting ``True`` even if true occupancy momentarily drops
  to zero (matches the way real PIRs latch for a configurable window).
- Small ``false_negative_rate`` lets the sensor occasionally miss a
  detection on a single tick.
- Small ``false_positive_rate`` lets the sensor occasionally trigger
  in an empty room (radiator click, draught, etc.).

``occupant_count`` is *not* a high-fidelity headcount — it's just the
true value snapped to the sensor's resolution (default: 1 if any
occupancy is detected, 0 otherwise). This keeps it useful for richness
level 3 ("IAQ + Occupancy") without overlapping the people counter.

All parameters are read from ``device.metadata``; nothing is hardcoded.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..models.config import DeviceConfig
from ..models.reading import SensorReading


@dataclass
class _OccupancyState:
    last_ts: datetime | None = None
    hold_until: datetime | None = None
    last_occupied: bool = False
    rng: random.Random = field(default_factory=random.Random)


class OccupancySensorSimulator:
    """PIR/ultrasonic-style binary occupancy sensor.

    Parameters (all optional, read from ``device.metadata``):

    - ``hold_time_seconds`` (int, default 300): how long the sensor
      keeps reporting ``occupancy=True`` after the last detection.
    - ``false_negative_rate`` (float 0..1, default 0.01): chance the
      sensor fails to detect a real occupant on a tick.
    - ``false_positive_rate`` (float 0..1, default 0.002): chance the
      sensor reports occupancy in an empty room.
    - ``report_count`` (bool, default True): include an integer
      ``occupant_count`` in the payload. Set to False to publish only
      the binary flag (cheaper PIR units).
    """

    def __init__(self, device: DeviceConfig, *, seed: int | None = None) -> None:
        if device.type != "occupancy_sensor":
            raise ValueError(
                f"OccupancySensorSimulator requires device.type='occupancy_sensor', "
                f"got '{device.type}'"
            )
        meta = device.metadata or {}
        self.device = device
        self.hold_time_seconds = max(0, int(meta.get("hold_time_seconds", 300)))
        self.false_negative_rate = max(0.0, min(1.0, float(meta.get("false_negative_rate", 0.01))))
        self.false_positive_rate = max(0.0, min(1.0, float(meta.get("false_positive_rate", 0.002))))
        self.report_count = bool(meta.get("report_count", True))
        self._state = _OccupancyState(rng=random.Random(seed))

    @property
    def state(self) -> _OccupancyState:
        return self._state

    def sample(self, timestamp: datetime, true_occupancy: int) -> SensorReading:
        if true_occupancy < 0:
            raise ValueError("true_occupancy must be >= 0")

        ts = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
        rng = self._state.rng

        truly_occupied = true_occupancy > 0

        if truly_occupied:
            # Real occupants — usually detected, sometimes missed.
            detected = rng.random() >= self.false_negative_rate
        else:
            # Empty room — occasional false trigger.
            detected = rng.random() < self.false_positive_rate

        if detected:
            self._state.hold_until = ts + timedelta(seconds=self.hold_time_seconds)

        held = (
            self._state.hold_until is not None
            and ts <= self._state.hold_until
        )
        reported_occupied = detected or held

        if self.report_count:
            occupant_count = int(true_occupancy) if reported_occupied else 0
            # If we're only held (no detection this tick), don't overstate.
            if reported_occupied and not detected and not truly_occupied:
                occupant_count = 1
        else:
            occupant_count = 0

        self._state.last_ts = ts
        self._state.last_occupied = reported_occupied

        data: dict[str, object] = {"occupancy": bool(reported_occupied)}
        if self.report_count:
            data["occupant_count"] = occupant_count

        return SensorReading(
            device_eui=self.device.device_eui,
            sensor_type="occupancy_sensor",
            timestamp=ts,
            data=data,
            metadata={
                "zone_id": self.device.zone_id,
                "name": self.device.name,
                "true_occupancy": int(true_occupancy),
                "held": bool(held and not detected),
            },
        )


__all__ = ["OccupancySensorSimulator"]
