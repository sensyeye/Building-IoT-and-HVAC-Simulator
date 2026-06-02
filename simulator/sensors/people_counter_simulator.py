"""People-counter / occupancy simulators.

Two related but distinct devices:

- :class:`PeopleCounterSimulator` — a presence/occupancy sensor reporting
  the number of people in a zone and a boolean ``occupancy`` flag. It
  consumes an externally-driven occupancy schedule (so a single
  ``ZoneState`` can be shared with the IAQ simulator).

- :class:`EntryExitCounterSimulator` — a door-mounted bidirectional
  counter reporting per-interval ``periodic_counter_in/out`` and
  cumulative ``total_counter_in/out``. Total counters are monotonic
  non-decreasing; estimated occupancy = ``total_in - total_out`` is
  clamped at zero.

Both simulators emit canonical :class:`SensorReading` instances and are
config-driven via ``device.metadata`` (no hardcoded magic numbers).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..models.config import DeviceConfig
from ..models.reading import SensorReading


# ---------------------------------------------------------------------------
# PeopleCounterSimulator
# ---------------------------------------------------------------------------

@dataclass
class _PeopleState:
    last_count: int = 0
    last_ts: datetime | None = None
    rng: random.Random = field(default_factory=random.Random)


class PeopleCounterSimulator:
    """Presence sensor reporting an integer ``people_count`` and a boolean
    ``occupancy`` flag.

    Unlike the entry/exit counter, this device samples the *current*
    headcount in a zone (e.g. PIR + radar / mmWave / camera). It expects
    the caller to pass the externally-driven occupancy via
    :meth:`sample`, and adds small stochastic miscounts to keep things
    realistic.

    Parameters
    ----------
    device:
        :class:`DeviceConfig` with ``type='people_counter'``. Tunables
        (all optional, all read from ``device.metadata``):

        - ``miscount_std`` (float): stddev of additive integer noise
          (default 0.4 — i.e. mostly exact, occasionally ±1).
        - ``capacity`` (int): zone capacity used as upper clamp.

    seed:
        Optional RNG seed.
    """

    def __init__(self, device: DeviceConfig, *, seed: int | None = None) -> None:
        if device.type != "people_counter":
            raise ValueError(
                f"PeopleCounterSimulator requires device.type='people_counter', "
                f"got '{device.type}'"
            )
        meta = device.metadata or {}
        self.device = device
        self.miscount_std = float(meta.get("miscount_std", 0.4))
        capacity = meta.get("capacity")
        self.capacity: int | None = int(capacity) if capacity is not None else None
        self._state = _PeopleState(rng=random.Random(seed))

    # -- public API --------------------------------------------------------

    @property
    def state(self) -> _PeopleState:
        return self._state

    def sample(self, timestamp: datetime, true_occupancy: int) -> SensorReading:
        """Return one ``people_counter`` reading.

        Parameters
        ----------
        timestamp:
            Timestamp of the reading (UTC assumed if naive).
        true_occupancy:
            Ground-truth headcount for the zone at this moment. The
            simulator perturbs this slightly to represent miscounts.
        """
        if true_occupancy < 0:
            raise ValueError("true_occupancy must be >= 0")

        ts = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
        rng = self._state.rng

        # Additive Gaussian miscount, rounded to int and clamped.
        observed = int(round(true_occupancy + rng.gauss(0, self.miscount_std)))
        observed = max(0, observed)
        if self.capacity is not None:
            observed = min(observed, self.capacity)

        self._state.last_count = observed
        self._state.last_ts = ts

        return SensorReading(
            device_eui=self.device.device_eui,
            sensor_type="people_counter",
            timestamp=ts,
            data={
                "people_count": observed,
                "occupancy": bool(observed > 0),
            },
            metadata={
                "zone_id": self.device.zone_id,
                "name": self.device.name,
                "true_occupancy": int(true_occupancy),
            },
        )


# ---------------------------------------------------------------------------
# EntryExitCounterSimulator
# ---------------------------------------------------------------------------

@dataclass
class _EntryExitState:
    total_in: int = 0
    total_out: int = 0
    estimated_occupancy: int = 0
    last_ts: datetime | None = None
    rng: random.Random = field(default_factory=random.Random)


class EntryExitCounterSimulator:
    """Door-mounted bidirectional counter.

    Emits four metrics:
      - ``periodic_counter_in``  : people entering during this interval
      - ``periodic_counter_out`` : people leaving during this interval
      - ``total_counter_in``     : cumulative entries (monotonic)
      - ``total_counter_out``    : cumulative exits (monotonic)

    Estimated occupancy ``= total_in - total_out`` is reported in
    metadata and is clamped at zero.

    Flow shape (when called without an explicit ``flow_in``/``flow_out``):

      - Morning ramp (``morning_peak_hour``, default 9): mostly *in*.
      - Evening ramp (``evening_peak_hour``, default 17): mostly *out*.
      - Lunch dip around midday adds a smaller bidirectional flow.
      - Night/weekends: near zero.

    All shape parameters are tunable via ``device.metadata``.
    """

    def __init__(self, device: DeviceConfig, *, seed: int | None = None) -> None:
        if device.type != "entry_exit_counter":
            raise ValueError(
                f"EntryExitCounterSimulator requires device.type='entry_exit_counter', "
                f"got '{device.type}'"
            )
        meta = device.metadata or {}
        self.device = device

        self.peak_flow_per_min = float(meta.get("peak_flow_per_min", 0.6))
        if self.peak_flow_per_min < 0:
            raise ValueError("peak_flow_per_min must be >= 0")

        self.morning_peak_hour = float(meta.get("morning_peak_hour", 9.0))
        self.evening_peak_hour = float(meta.get("evening_peak_hour", 17.0))
        self.lunch_peak_hour = float(meta.get("lunch_peak_hour", 12.5))
        self.peak_width_hours = float(meta.get("peak_width_hours", 1.5))

        self.weekend_factor = float(meta.get("weekend_factor", 0.05))
        self.lunch_strength = float(meta.get("lunch_strength", 0.25))
        # When True (default) the reading also exposes ``net_occupancy``
        # as a top-level data field so dashboards can subscribe to it
        # directly. Set False for raw-counter-only deployments.
        self.report_net_occupancy = bool(meta.get("report_net_occupancy", True))

        # Initial cumulative counts (e.g. continuing a deployment).
        self._state = _EntryExitState(
            total_in=int(meta.get("initial_total_in", 0)),
            total_out=int(meta.get("initial_total_out", 0)),
            rng=random.Random(seed),
        )
        self._state.estimated_occupancy = max(
            0, self._state.total_in - self._state.total_out
        )

    # -- public API --------------------------------------------------------

    @property
    def state(self) -> _EntryExitState:
        return self._state

    def sample(
        self,
        timestamp: datetime,
        *,
        flow_in: int | None = None,
        flow_out: int | None = None,
    ) -> SensorReading:
        """Advance state to ``timestamp`` and return one reading.

        Parameters
        ----------
        timestamp:
            Timestamp of the reading.
        flow_in, flow_out:
            Optional explicit per-interval counts (used by tests and
            scenario overrides). When omitted the simulator derives
            them from a daily flow profile.
        """
        ts = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
        dt_min = self._minutes_since_last(ts)

        if flow_in is None or flow_out is None:
            shaped_in, shaped_out = self._shape_flow(ts, dt_min)
            flow_in = shaped_in if flow_in is None else flow_in
            flow_out = shaped_out if flow_out is None else flow_out

        flow_in = max(0, int(flow_in))
        flow_out = max(0, int(flow_out))

        # Cap exits so estimated occupancy never goes negative.
        flow_out = min(flow_out, self._state.estimated_occupancy + flow_in)

        self._state.total_in += flow_in
        self._state.total_out += flow_out
        self._state.estimated_occupancy = max(
            0, self._state.total_in - self._state.total_out
        )
        self._state.last_ts = ts

        data: dict[str, Any] = {
            "periodic_counter_in": flow_in,
            "periodic_counter_out": flow_out,
            "total_counter_in": self._state.total_in,
            "total_counter_out": self._state.total_out,
        }
        if self.report_net_occupancy:
            data["net_occupancy"] = int(self._state.estimated_occupancy)

        return SensorReading(
            device_eui=self.device.device_eui,
            sensor_type="entry_exit_counter",
            timestamp=ts,
            data=data,
            metadata={
                "zone_id": self.device.zone_id,
                "name": self.device.name,
                "estimated_occupancy": self._state.estimated_occupancy,
            },
        )

    # -- internals ---------------------------------------------------------

    def _minutes_since_last(self, ts: datetime) -> float:
        if self._state.last_ts is None:
            return 1.0
        return max((ts - self._state.last_ts).total_seconds() / 60.0, 0.0)

    @staticmethod
    def _gaussian_pulse(hour: float, peak: float, width: float) -> float:
        """Returns a 0..1 bell curve centred at ``peak`` hour with given width."""
        if width <= 0:
            return 0.0
        z = (hour - peak) / width
        return math.exp(-0.5 * z * z)

    def _shape_flow(self, ts: datetime, dt_min: float) -> tuple[int, int]:
        """Generate (flow_in, flow_out) over ``dt_min`` minutes from a daily profile."""
        rng = self._state.rng
        hour_frac = ts.hour + ts.minute / 60.0 + ts.second / 3600.0

        morning = self._gaussian_pulse(hour_frac, self.morning_peak_hour, self.peak_width_hours)
        evening = self._gaussian_pulse(hour_frac, self.evening_peak_hour, self.peak_width_hours)
        lunch = self._gaussian_pulse(hour_frac, self.lunch_peak_hour, self.peak_width_hours / 2.0)

        weekend = ts.weekday() >= 5
        scale = self.weekend_factor if weekend else 1.0

        # Expected events over the interval. Lunch contributes equal in/out;
        # morning is mostly inbound; evening is mostly outbound.
        in_rate = self.peak_flow_per_min * (
            0.95 * morning + 0.05 * evening + self.lunch_strength * lunch
        )
        out_rate = self.peak_flow_per_min * (
            0.05 * morning + 0.95 * evening + self.lunch_strength * lunch
        )

        lam_in = max(0.0, in_rate * dt_min * scale)
        lam_out = max(0.0, out_rate * dt_min * scale)
        return _poisson(rng, lam_in), _poisson(rng, lam_out)


def _poisson(rng: random.Random, lam: float) -> int:
    """Tiny Knuth Poisson sampler — fine for the small means we use."""
    if lam <= 0:
        return 0
    if lam > 30:  # Gaussian fallback for large means
        return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))
    L = math.exp(-lam)
    k = 0
    p = 1.0
    while True:
        k += 1
        p *= rng.random()
        if p <= L:
            return k - 1


__all__ = [
    "EntryExitCounterSimulator",
    "PeopleCounterSimulator",
]
