"""IAQ sensor simulator (v1).

Generates a small but realistic set of indoor-air-quality metrics for a
single zone. The intent is correctness of *shape* (smooth changes,
sensible ranges, reasonable response to occupancy) rather than full
physical fidelity.

Outputs canonical :class:`SensorReading` instances; conversion to
Sensgreen ids happens later in the output adapters.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..models.config import DeviceConfig
from ..models.reading import SensorReading
from .device_personality import DevicePersonality
from .zone_state import ZoneState


@dataclass
class _DeviceState:
    """Internal evolving state for a single IAQ device."""

    temperature_c: float
    humidity_pct: float
    co2_ppm: float
    voc_mg_m3: float
    pm25_ug_m3: float
    pm10_ug_m3: float
    pressure_pa: float
    battery_v: float
    last_ts: datetime | None = None
    rng: random.Random = field(default_factory=random.Random)


class IaqSensorSimulator:
    """Stateful simulator that produces one IAQ :class:`SensorReading` per tick.

    Parameters
    ----------
    device:
        The :class:`DeviceConfig` this simulator represents.
    seed:
        Random seed for reproducibility.
    base_temperature_c, base_humidity_pct:
        Initial / target values; can also be supplied via
        ``device.metadata`` keys ``base_temperature_c`` /
        ``base_humidity_pct``.
    humidity_min, humidity_max:
        Hard humidity clamp. Scenarios may override via
        ``device.metadata['humidity_min'/'humidity_max']``.
    """

    # Physical / heuristic constants
    OUTDOOR_CO2_PPM = 420.0
    # Realistic indoor buildup/decay tuned to match field data: occupied
    # rooms typically settle around 1000–1400 ppm and only occasionally
    # excurse above 1500 ppm. Equilibrium of the first-order model is
    #   eq = OUTDOOR + (CO2_PER_PERSON_PPM_PER_MIN * 10/capacity * N) / CO2_DECAY_PER_MIN
    # With the values below and N = capacity (full room): eq ≈ 1545 ppm,
    # but the first-order response rarely settles during a working day, so
    # typical peaks land in the 1300–1500 range. Noise + transient
    # overshoot push the worst samples slightly above 1500 occasionally;
    # scenarios can briefly approach the 2000 ppm ceiling.
    CO2_PER_PERSON_PPM_PER_MIN = 9.0
    CO2_DECAY_PER_MIN = 0.08            # fraction of (co2 - outdoor) lost / min
    TEMP_DRIFT_PER_MIN = 0.05          # how fast temp tracks the base value
    TEMP_NOISE = 0.05                  # °C per sample
    HUM_DRIFT_PER_MIN = 0.20
    HUM_NOISE = 0.5
    BATTERY_DRAIN_V_PER_MIN = 1.5e-5   # ~3.6 -> 3.0 V over ~9 months @ 1/min

    # Physical ranges for sanity / clamping
    TEMP_MIN, TEMP_MAX = 5.0, 45.0
    DEFAULT_HUM_MIN, DEFAULT_HUM_MAX = 30.0, 75.0
    CO2_MIN, CO2_MAX = 400.0, 2000.0
    VOC_MIN, VOC_MAX = 0.0, 10.0
    PM25_MIN, PM25_MAX = 0.0, 500.0
    PM10_MIN, PM10_MAX = 0.0, 1000.0
    PRESSURE_MIN, PRESSURE_MAX = 95_000.0, 105_000.0
    BATTERY_MIN, BATTERY_MAX = 2.5, 3.7

    def __init__(
        self,
        device: DeviceConfig,
        *,
        seed: int | None = None,
        base_temperature_c: float | None = None,
        base_humidity_pct: float | None = None,
        humidity_min: float | None = None,
        humidity_max: float | None = None,
    ) -> None:
        if device.type != "iaq":
            raise ValueError(
                f"IaqSensorSimulator requires device.type='iaq', got '{device.type}'"
            )
        self.device = device

        meta = device.metadata or {}
        self.base_temperature_c = float(
            base_temperature_c
            if base_temperature_c is not None
            else meta.get("base_temperature_c", 22.0)
        )
        self.base_humidity_pct = float(
            base_humidity_pct
            if base_humidity_pct is not None
            else meta.get("base_humidity_pct", 50.0)
        )
        self.humidity_min = float(
            humidity_min
            if humidity_min is not None
            else meta.get("humidity_min", self.DEFAULT_HUM_MIN)
        )
        self.humidity_max = float(
            humidity_max
            if humidity_max is not None
            else meta.get("humidity_max", self.DEFAULT_HUM_MAX)
        )
        if self.humidity_min >= self.humidity_max:
            raise ValueError("humidity_min must be < humidity_max")

        rng = random.Random(seed)
        self._state = _DeviceState(
            temperature_c=self.base_temperature_c,
            humidity_pct=self.base_humidity_pct,
            co2_ppm=self.OUTDOOR_CO2_PPM + 50.0,
            voc_mg_m3=0.2,
            pm25_ug_m3=8.0,
            pm10_ug_m3=14.0,
            pressure_pa=101_325.0,
            battery_v=3.6,
            rng=rng,
        )

        # P10.2: per-device measurement personality. Defaults to "normal"
        # unless the device metadata declares e.g. personality="near_door".
        self._personality = DevicePersonality.from_device(device)

    # -- public API --------------------------------------------------------

    @property
    def state(self) -> _DeviceState:
        """Expose internal state (mostly for tests)."""
        return self._state

    @property
    def personality(self) -> DevicePersonality:
        """Expose the per-device measurement personality (P10.2)."""
        return self._personality

    def sample(
        self,
        timestamp: datetime,
        zone: ZoneState,
        *,
        room_driven: bool = False,
    ) -> SensorReading:
        """Advance state to ``timestamp`` and return one IAQ reading.

        Parameters
        ----------
        timestamp:
            Wall-clock timestamp for the reading.
        zone:
            Room state. Authoritative when ``room_driven`` is True.
        room_driven:
            * ``False`` (default, back-compat): IAQ owns the physics. The
              old ``_step`` runs on the device's internal state; the
              personality then biases the reported values.
            * ``True`` (used by :class:`ScenarioContext` from P10.4): the
              physics already advanced on ``zone`` for this tick. IAQ
              just observes ``zone.*`` through its personality and
              mirrors the values onto ``_state`` for ``state`` consumers.
        """
        if zone.zone_id != self.device.zone_id:
            raise ValueError(
                f"Zone '{zone.zone_id}' does not match device.zone_id "
                f"'{self.device.zone_id}'"
            )

        ts = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
        dt_min = self._minutes_since_last(ts)

        if room_driven:
            self._observe_room(zone, dt_min)
        else:
            self._step(dt_min, zone)
        self._tick_battery(dt_min)
        self._state.last_ts = ts

        s = self._state
        p = self._personality

        # Apply personality once per metric, then clamp to physical / scenario
        # ranges. Battery is *not* observed: it's a device-internal quantity.
        temp = self._clamp(
            p.observe("temperature_c", s.temperature_c, dt_min),
            self.TEMP_MIN, self.TEMP_MAX,
        )
        hum = self._clamp(
            p.observe("humidity_pct", s.humidity_pct, dt_min),
            self.humidity_min, self.humidity_max,
        )
        co2 = self._clamp(
            p.observe("co2_ppm", s.co2_ppm, dt_min),
            self.CO2_MIN, self.CO2_MAX,
        )
        voc = self._clamp(
            p.observe("voc_mg_m3", s.voc_mg_m3, dt_min),
            self.VOC_MIN, self.VOC_MAX,
        )
        pm25 = self._clamp(
            p.observe("pm25_ug_m3", s.pm25_ug_m3, dt_min),
            self.PM25_MIN, self.PM25_MAX,
        )
        pm10 = self._clamp(
            max(p.observe("pm10_ug_m3", s.pm10_ug_m3, dt_min), pm25),
            self.PM10_MIN, self.PM10_MAX,
        )
        press = self._clamp(
            p.observe("pressure_pa", s.pressure_pa, dt_min),
            self.PRESSURE_MIN, self.PRESSURE_MAX,
        )

        data: dict[str, Any] = {
            "temperature": round(temp, 2),
            "humidity": round(hum, 2),
            "co2": round(co2, 1),
            "voc": round(voc, 3),
            "pm25": round(pm25, 2),
            "pm10": round(pm10, 2),
            "pressure": round(press, 1),
            "battery": round(s.battery_v, 3),
        }
        return SensorReading(
            device_eui=self.device.device_eui,
            sensor_type="iaq",
            timestamp=ts,
            data=data,
            metadata={
                "zone_id": self.device.zone_id,
                "occupancy": zone.occupancy,
                "name": self.device.name,
            },
        )

    # -- internals ---------------------------------------------------------

    def _minutes_since_last(self, ts: datetime) -> float:
        if self._state.last_ts is None:
            return 1.0  # treat first tick as a 1-minute step
        delta = (ts - self._state.last_ts).total_seconds() / 60.0
        # Guard against zero/negative steps; smoothness assumes forward time.
        return max(delta, 0.0)

    def _step(self, dt_min: float, zone: ZoneState) -> None:
        s = self._state
        rng = s.rng

        # --- CO2: rises with occupancy, decays towards outdoor otherwise.
        capacity = max(zone.capacity, 1)
        # Buildup scales with occupancy share and inversely with capacity proxy.
        buildup = (
            self.CO2_PER_PERSON_PPM_PER_MIN
            * max(zone.occupancy, 0)
            * (10.0 / capacity)
            * dt_min
        )
        decay = self.CO2_DECAY_PER_MIN * (s.co2_ppm - self.OUTDOOR_CO2_PPM) * dt_min
        s.co2_ppm = self._clamp(
            s.co2_ppm + buildup - decay + rng.gauss(0, 4.0),
            self.CO2_MIN,
            self.CO2_MAX,
        )

        # --- Temperature: drifts smoothly toward base + a tiny bump per occupant.
        target_temp = self.base_temperature_c + 0.03 * max(zone.occupancy, 0)
        s.temperature_c = self._clamp(
            s.temperature_c
            + (target_temp - s.temperature_c) * self.TEMP_DRIFT_PER_MIN * dt_min
            + rng.gauss(0, self.TEMP_NOISE),
            self.TEMP_MIN,
            self.TEMP_MAX,
        )

        # --- Humidity: drifts toward base; occupancy nudges it up slightly;
        # then clamped to scenario / default range.
        target_hum = self.base_humidity_pct + 0.2 * max(zone.occupancy, 0)
        s.humidity_pct = self._clamp(
            s.humidity_pct
            + (target_hum - s.humidity_pct) * self.HUM_DRIFT_PER_MIN * dt_min
            + rng.gauss(0, self.HUM_NOISE),
            self.humidity_min,
            self.humidity_max,
        )

        # --- VOC: small random walk biased by occupancy.
        s.voc_mg_m3 = self._clamp(
            s.voc_mg_m3
            + 0.005 * max(zone.occupancy, 0) * dt_min
            - 0.01 * dt_min
            + rng.gauss(0, 0.02),
            self.VOC_MIN,
            self.VOC_MAX,
        )

        # --- PM2.5 / PM10: gentle random walks; PM10 stays ≥ PM2.5.
        s.pm25_ug_m3 = self._clamp(
            s.pm25_ug_m3 + rng.gauss(0, 0.4) * math.sqrt(dt_min),
            self.PM25_MIN,
            self.PM25_MAX,
        )
        s.pm10_ug_m3 = self._clamp(
            max(s.pm10_ug_m3 + rng.gauss(0, 0.6) * math.sqrt(dt_min), s.pm25_ug_m3),
            self.PM10_MIN,
            self.PM10_MAX,
        )

        # --- Pressure: tight random walk around standard atmosphere.
        s.pressure_pa = self._clamp(
            s.pressure_pa + rng.gauss(0, 5.0) * math.sqrt(dt_min),
            self.PRESSURE_MIN,
            self.PRESSURE_MAX,
        )

    def _observe_room(self, zone: ZoneState, dt_min: float) -> None:
        """Mirror authoritative room state into the device's _state.

        Used in ``room_driven`` mode (P10.4): the room already advanced
        its physics for this tick, so the device just copies the new
        truth values. The personality layer runs in :meth:`sample`.
        """
        s = self._state
        s.temperature_c = float(zone.temperature_c if zone.temperature_c is not None
                                else self.base_temperature_c)
        s.humidity_pct = float(zone.humidity_pct if zone.humidity_pct is not None
                               else self.base_humidity_pct)
        s.co2_ppm = float(zone.co2_ppm)
        s.voc_mg_m3 = float(zone.voc_mg_m3)
        s.pm25_ug_m3 = float(zone.pm25_ug_m3)
        s.pm10_ug_m3 = float(zone.pm10_ug_m3)
        s.pressure_pa = float(zone.pressure_pa)

    def _tick_battery(self, dt_min: float) -> None:
        """Monotonic slow battery drain, independent of the physics path."""
        s = self._state
        s.battery_v = self._clamp(
            s.battery_v - self.BATTERY_DRAIN_V_PER_MIN * dt_min,
            self.BATTERY_MIN,
            self.BATTERY_MAX,
        )

    @staticmethod
    def _clamp(value: float, lo: float, hi: float) -> float:
        if value < lo:
            return lo
        if value > hi:
            return hi
        return value


__all__ = ["IaqSensorSimulator", "ZoneState"]
