"""Energy meter simulator (v1).

Generates realistic 3-phase electrical metering readings using hidden
state. The shape (not the absolute precision) is what matters: the
output should look plausible to dashboards, AI insights, and the
validators in :mod:`simulator.validators`.

Design
------
- Stateful per device. ``sample(timestamp, context)`` advances state and
  returns one canonical :class:`SensorReading` of ``sensor_type="energy_meter"``.
- Behaviour is driven by the device's *submeter role*
  (``device.metadata['submeter']``):

  * ``hvac``     — responds to outdoor temperature *and* occupancy
  * ``lighting`` — responds mainly to schedule (business hours)
  * ``plug``     — responds mainly to occupancy
  * ``main`` / ``other`` — blended

- Cumulative ``active_energy`` (kWh) is monotonic non-decreasing.
- ``voltage_*`` and ``frequency`` are stable with small Gaussian noise.
- ``power_factor`` stays in ``[pf_min, pf_max]`` (default 0.85–1.0)
  unless a scenario override is supplied via metadata.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from ..models.config import DeviceConfig
from ..models.reading import SensorReading

SubmeterRole = Literal["main", "hvac", "lighting", "plug", "other"]


@dataclass
class EnergyContext:
    """Environment & occupancy info passed to :meth:`EnergyMeterSimulator.sample`.

    Attributes
    ----------
    outdoor_temperature_c:
        Outside dry-bulb air temperature. Drives HVAC load.
    occupancy:
        Current people count for the served zone/floor/building.
    occupancy_capacity:
        Nominal capacity used to normalise occupancy to ``[0, 1]``.
    is_business_hours:
        Optional explicit override. When ``None`` the simulator derives
        this from ``timestamp`` (Mon–Fri, 08:00–18:00 local-ish).
    """

    outdoor_temperature_c: float = 22.0
    occupancy: int = 0
    occupancy_capacity: int = 10
    is_business_hours: bool | None = None


@dataclass
class _MeterState:
    active_energy_kwh: float
    last_active_power_kw: float
    last_ts: datetime | None = None
    rng: random.Random = field(default_factory=random.Random)


class EnergyMeterSimulator:
    """Stateful 3-phase energy meter simulator.

    Parameters
    ----------
    device:
        :class:`DeviceConfig` with ``type='energy_meter'``. Tunables are
        read from ``device.metadata``:

        - ``submeter`` (str): one of ``main``/``hvac``/``lighting``/``plug``/``other``
        - ``nominal_kw`` (float): peak active power in kW
        - ``voltage_nominal`` (float): per-phase line-to-neutral voltage (default 230)
        - ``frequency_nominal`` (float): default 50.0 Hz
        - ``power_factor_nominal`` (float): default 0.95
        - ``power_factor_min`` / ``power_factor_max`` (floats): default 0.85 / 1.0
        - ``business_hour_start`` / ``business_hour_end`` (ints): default 8 / 18
        - ``base_load_factor`` (float in [0,1]): minimum night load ratio
    seed:
        Optional RNG seed for reproducibility.
    """

    # Defaults
    DEFAULT_VOLTAGE = 230.0
    DEFAULT_FREQUENCY = 50.0
    DEFAULT_PF = 0.95
    DEFAULT_PF_MIN = 0.85
    DEFAULT_PF_MAX = 1.0

    VOLTAGE_NOISE_V = 0.5
    FREQ_NOISE_HZ = 0.02
    POWER_NOISE_FRAC = 0.03  # ±3% white noise on active_power
    PHASE_IMBALANCE_FRAC = 0.04  # ±4% per-phase variation

    def __init__(self, device: DeviceConfig, *, seed: int | None = None) -> None:
        if device.type != "energy_meter":
            raise ValueError(
                f"EnergyMeterSimulator requires device.type='energy_meter', "
                f"got '{device.type}'"
            )
        self.device = device
        meta = device.metadata or {}

        self.submeter: SubmeterRole = str(meta.get("submeter", "main"))  # type: ignore[assignment]
        self.nominal_kw = float(meta.get("nominal_kw", 10.0))
        if self.nominal_kw <= 0:
            raise ValueError("nominal_kw must be > 0")

        self.voltage_nominal = float(meta.get("voltage_nominal", self.DEFAULT_VOLTAGE))
        self.frequency_nominal = float(
            meta.get("frequency_nominal", self.DEFAULT_FREQUENCY)
        )
        self.pf_nominal = float(meta.get("power_factor_nominal", self.DEFAULT_PF))
        self.pf_min = float(meta.get("power_factor_min", self.DEFAULT_PF_MIN))
        self.pf_max = float(meta.get("power_factor_max", self.DEFAULT_PF_MAX))
        if not (0.0 < self.pf_min <= self.pf_nominal <= self.pf_max <= 1.0):
            raise ValueError(
                "Require 0 < power_factor_min <= nominal <= max <= 1"
            )

        self.business_hour_start = int(meta.get("business_hour_start", 8))
        self.business_hour_end = int(meta.get("business_hour_end", 18))
        self.base_load_factor = float(meta.get("base_load_factor", 0.05))

        rng = random.Random(seed)
        self._state = _MeterState(
            active_energy_kwh=float(meta.get("initial_energy_kwh", 0.0)),
            last_active_power_kw=self.nominal_kw * self.base_load_factor,
            rng=rng,
        )

    # -- public API --------------------------------------------------------

    @property
    def state(self) -> _MeterState:
        return self._state

    def sample(
        self, timestamp: datetime, context: EnergyContext | None = None
    ) -> SensorReading:
        """Advance state to ``timestamp`` and return one energy meter reading."""
        ctx = context or EnergyContext()
        ts = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
        dt_min = self._minutes_since_last(ts)
        dt_h = dt_min / 60.0

        load_factor = self._compute_load_factor(ts, ctx)
        active_power_kw = max(0.0, self.nominal_kw * load_factor)

        # Smooth power with a small first-order filter so consecutive
        # samples do not jump abruptly (helps temporal validator).
        prev = self._state.last_active_power_kw
        smoothing = math.exp(-dt_min / 5.0)  # 5-min time constant
        active_power_kw = smoothing * prev + (1.0 - smoothing) * active_power_kw

        # Power factor (clamped to configured band) and apparent power.
        pf = self._sample_power_factor()
        apparent_power_kva = active_power_kw / max(pf, 1e-3)

        # Voltages & frequency: tight Gaussian noise around nominals.
        rng = self._state.rng
        v1 = self.voltage_nominal + rng.gauss(0, self.VOLTAGE_NOISE_V)
        v2 = self.voltage_nominal + rng.gauss(0, self.VOLTAGE_NOISE_V)
        v3 = self.voltage_nominal + rng.gauss(0, self.VOLTAGE_NOISE_V)
        frequency = self.frequency_nominal + rng.gauss(0, self.FREQ_NOISE_HZ)

        # Per-phase currents derived from balanced 3-phase relation:
        #   P_total = sqrt(3) * V_LL * I * pf
        # We use line-to-neutral V here, so equivalent: per-phase
        #   I_phase = P_total[W] / (3 * V_LN * pf)
        # Then add small imbalance per phase.
        total_w = active_power_kw * 1000.0
        denom = 3.0 * self.voltage_nominal * max(pf, 1e-3)
        i_balanced = total_w / denom if denom > 0 else 0.0
        imb = self.PHASE_IMBALANCE_FRAC
        i1 = max(0.0, i_balanced * (1.0 + rng.uniform(-imb, imb)))
        i2 = max(0.0, i_balanced * (1.0 + rng.uniform(-imb, imb)))
        i3 = max(0.0, i_balanced * (1.0 + rng.uniform(-imb, imb)))

        # Cumulative energy: monotonic non-decreasing. Use the *current*
        # active_power_kw (post-smoothing) to integrate over dt_h.
        if dt_h > 0:
            self._state.active_energy_kwh += active_power_kw * dt_h

        self._state.last_active_power_kw = active_power_kw
        self._state.last_ts = ts

        data: dict[str, Any] = {
            "active_power": round(active_power_kw, 4),
            "apparent_power": round(apparent_power_kva, 4),
            "active_energy": round(self._state.active_energy_kwh, 4),
            "voltage_1": round(v1, 2),
            "voltage_2": round(v2, 2),
            "voltage_3": round(v3, 2),
            "current_1": round(i1, 3),
            "current_2": round(i2, 3),
            "current_3": round(i3, 3),
            "power_factor": round(pf, 3),
            "frequency": round(frequency, 3),
        }
        return SensorReading(
            device_eui=self.device.device_eui,
            sensor_type="energy_meter",
            timestamp=ts,
            data=data,
            metadata={
                "zone_id": self.device.zone_id,
                "name": self.device.name,
                "submeter": self.submeter,
            },
        )

    # -- internals ---------------------------------------------------------

    def _minutes_since_last(self, ts: datetime) -> float:
        if self._state.last_ts is None:
            return 1.0
        return max((ts - self._state.last_ts).total_seconds() / 60.0, 0.0)

    def _is_business_hours(self, ts: datetime, ctx: EnergyContext) -> bool:
        if ctx.is_business_hours is not None:
            return ctx.is_business_hours
        # Treat ts as already in the building's local-ish timezone.
        if ts.weekday() >= 5:  # Sat/Sun
            return False
        return self.business_hour_start <= ts.hour < self.business_hour_end

    def _schedule_factor(self, ts: datetime, ctx: EnergyContext) -> float:
        """0..1 ramp: low at night, smooth ramp around business hours."""
        if not self._is_business_hours(ts, ctx):
            return self.base_load_factor
        # Smooth half-sine peaking at midpoint of business window.
        mid = (self.business_hour_start + self.business_hour_end) / 2.0
        half = max((self.business_hour_end - self.business_hour_start) / 2.0, 1.0)
        x = (ts.hour + ts.minute / 60.0 - mid) / half  # in [-1, 1]
        shape = max(0.0, math.cos(0.5 * math.pi * x))  # 0..1
        return self.base_load_factor + (1.0 - self.base_load_factor) * (0.4 + 0.6 * shape)

    def _occupancy_factor(self, ctx: EnergyContext) -> float:
        cap = max(ctx.occupancy_capacity, 1)
        return max(0.0, min(1.0, ctx.occupancy / cap))

    def _hvac_temp_factor(self, ctx: EnergyContext) -> float:
        """HVAC load grows with |T_outside - 22 °C|, saturating around ±15 °C."""
        delta = abs(ctx.outdoor_temperature_c - 22.0)
        return min(1.0, delta / 15.0)

    def _compute_load_factor(self, ts: datetime, ctx: EnergyContext) -> float:
        """Combine submeter-specific drivers into a 0..1 load factor."""
        sched = self._schedule_factor(ts, ctx)
        occ = self._occupancy_factor(ctx)
        temp = self._hvac_temp_factor(ctx)

        if self.submeter == "hvac":
            # HVAC: outdoor temp + occupancy, gated by schedule (mostly off at night).
            lf = sched * (0.4 + 0.4 * temp + 0.2 * occ)
        elif self.submeter == "lighting":
            # Lighting: schedule-dominant with small occupancy boost.
            lf = sched * (0.85 + 0.15 * occ)
        elif self.submeter == "plug":
            # Plug load: occupancy-dominant but only if people are around.
            lf = self.base_load_factor + (1.0 - self.base_load_factor) * occ * (
                0.7 + 0.3 * sched
            )
        else:  # main / other / unknown -> blend
            lf = 0.5 * sched + 0.3 * occ + 0.2 * (self.base_load_factor + 0.5 * temp)

        # White noise (multiplicative, ±POWER_NOISE_FRAC).
        noise = self._state.rng.gauss(0, self.POWER_NOISE_FRAC)
        lf *= 1.0 + noise
        return max(0.0, min(1.0, lf))

    def _sample_power_factor(self) -> float:
        rng = self._state.rng
        # Gaussian around nominal, clamped to [pf_min, pf_max].
        pf = rng.gauss(self.pf_nominal, 0.02)
        if pf < self.pf_min:
            pf = self.pf_min
        if pf > self.pf_max:
            pf = self.pf_max
        return pf


__all__ = ["EnergyMeterSimulator", "EnergyContext"]
