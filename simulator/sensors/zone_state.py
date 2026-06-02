"""Room-level (zone) physical state and physics step.

The :class:`ZoneState` is the *single source of truth* for what is
actually happening in a room: temperature, humidity, CO₂, particulate
load, pressure, door status, etc. Sensors don't simulate physics
themselves anymore — they *observe* this state (P10.3) and add their
own per-device personality (P10.2).

The physics is intentionally simple but well-shaped:

* CO₂ obeys a first-order mass-balance:
    eq ≈ outdoor + (k_person · occupants · 10/capacity) / decay
  where the decay term is the air-exchange rate. An open door temporarily
  raises that decay rate (and pulls temperature toward outdoor).
* Temperature relaxes toward the base setpoint, plus a small per-person
  bump, plus an outdoor coupling that strengthens while the door is open.
* Humidity drifts toward base + a small occupancy nudge.
* PM2.5 / PM10 follow a gentle random walk; PM10 ≥ PM2.5.
* Pressure is a tight random walk around the standard atmosphere.

Everything is seeded so two runs with the same building seed produce the
same trace; two rooms in the same building still feel different because
each room gets a stable per-room sub-seed.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Final


# ---------------------------------------------------------------------------
# Physics constants (room-level, were previously embedded in the IAQ device)
# ---------------------------------------------------------------------------
OUTDOOR_CO2_PPM: Final[float] = 420.0
CO2_PER_PERSON_PPM_PER_MIN: Final[float] = 9.0
CO2_BASE_DECAY_PER_MIN: Final[float] = 0.08      # closed-door air-exchange
CO2_DOOR_OPEN_DECAY_PER_MIN: Final[float] = 0.40  # ~5× while a door is open
TEMP_DRIFT_PER_MIN: Final[float] = 0.05
TEMP_OUTDOOR_COUPLING_CLOSED: Final[float] = 0.003
TEMP_OUTDOOR_COUPLING_DOOR_OPEN: Final[float] = 0.05
HUM_DRIFT_PER_MIN: Final[float] = 0.20
DOOR_BOOST_DECAY_MIN: Final[float] = 5.0  # door event keeps boosting for ~5 min after close

# --- HVAC tuning (P11.1) ----------------------------------------------------
# Strong pull toward the HVAC setpoint when the system is actively
# conditioning. "auto" follows the room's natural base temperature.
HVAC_SETPOINT_TRACK_PER_MIN: Final[float] = 0.18
# How effective the mechanical ventilation is at flushing CO₂, expressed as
# extra fractional decay per minute when running at "design" rate (7 L/s/person).
HVAC_VENT_DECAY_REFERENCE_L_S_PER_PERSON: Final[float] = 7.0
HVAC_VENT_DECAY_AT_REFERENCE_PER_MIN: Final[float] = 0.10
# Modes that consume no mechanical ventilation.
HVAC_OFF_MODES: Final[tuple[str, ...]] = ("off", "standby")

# Physical clamps (sane defaults)
TEMP_MIN, TEMP_MAX = 5.0, 45.0
HUM_MIN, HUM_MAX = 10.0, 95.0
CO2_MIN, CO2_MAX = 400.0, 2000.0
VOC_MIN, VOC_MAX = 0.0, 10.0
PM25_MIN, PM25_MAX = 0.0, 500.0
PM10_MIN, PM10_MAX = 0.0, 1000.0
PRESSURE_MIN, PRESSURE_MAX = 95_000.0, 105_000.0


def _clamp(value: float, lo: float, hi: float) -> float:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


@dataclass
class ZoneState:
    """Mutable room state shared with every sensor in the room.

    The first six fields are the original (compat) shape. Everything
    below them is the new authoritative physical state added in P10.1.
    """

    zone_id: str
    occupancy: int = 0
    capacity: int = 10
    volume_m3: float = 60.0
    base_temperature_c: float = 22.0
    base_humidity_pct: float = 50.0

    # --- Authoritative physical state (P10.1) ---------------------------
    temperature_c: float | None = None
    humidity_pct: float | None = None
    co2_ppm: float = OUTDOOR_CO2_PPM + 50.0
    voc_mg_m3: float = 0.2
    pm25_ug_m3: float = 8.0
    pm10_ug_m3: float = 14.0
    pressure_pa: float = 101_325.0

    # --- Door / HVAC influence ------------------------------------------
    door_open: bool = False
    door_boost_remaining_min: float = 0.0
    ventilation_l_s_per_person: float = 7.0  # design baseline (ASHRAE-ish)
    hvac_mode: str = "auto"  # auto|cool|heat|off|standby
    hvac_setpoint_c: float | None = None  # None → tracks base_temperature_c

    # --- RNG: micro-noise that is the room's own (separate from devices) ---
    rng: random.Random = field(default_factory=random.Random)

    def __post_init__(self) -> None:
        # Bootstrap any unset temperature / humidity to the base setpoint.
        if self.temperature_c is None:
            self.temperature_c = self.base_temperature_c
        if self.humidity_pct is None:
            self.humidity_pct = self.base_humidity_pct

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------
    def open_door(self, *, boost_min: float = DOOR_BOOST_DECAY_MIN) -> None:
        """Mark the door as open and arm an after-close infiltration boost."""
        self.door_open = True
        self.door_boost_remaining_min = max(self.door_boost_remaining_min, boost_min)

    def close_door(self) -> None:
        """Mark the door as closed; the boost continues to decay over time."""
        self.door_open = False

    # ------------------------------------------------------------------
    # Physics
    # ------------------------------------------------------------------
    def step(
        self,
        dt_min: float,
        outdoor_c: float,
        *,
        occupancy: int | None = None,
        door_open: bool | None = None,
    ) -> None:
        """Advance the room's physical state by ``dt_min`` minutes.

        Parameters
        ----------
        dt_min:
            Elapsed time since the last step, in minutes. ``0`` is a no-op.
        outdoor_c:
            Outdoor temperature for this tick (°C).
        occupancy:
            Optional override; defaults to ``self.occupancy``.
        door_open:
            Optional override; if ``True``, ``open_door`` is invoked first.
        """
        if dt_min <= 0:
            return
        if occupancy is not None:
            self.occupancy = max(int(occupancy), 0)
        if door_open is True:
            self.open_door()
        elif door_open is False:
            self.close_door()

        n = max(self.occupancy, 0)
        capacity = max(self.capacity, 1)
        rng = self.rng

        # Effective air-exchange factor: closed-door baseline, lifted while
        # the door is open or right after it closes.
        boost = 0.0
        if self.door_open:
            boost = 1.0
        elif self.door_boost_remaining_min > 0:
            # Linear fade from the moment the door closed.
            boost = min(self.door_boost_remaining_min / DOOR_BOOST_DECAY_MIN, 1.0)
            self.door_boost_remaining_min = max(
                self.door_boost_remaining_min - dt_min, 0.0
            )

        decay_per_min = (
            CO2_BASE_DECAY_PER_MIN
            + (CO2_DOOR_OPEN_DECAY_PER_MIN - CO2_BASE_DECAY_PER_MIN) * boost
        )
        outdoor_coupling = (
            TEMP_OUTDOOR_COUPLING_CLOSED
            + (TEMP_OUTDOOR_COUPLING_DOOR_OPEN - TEMP_OUTDOOR_COUPLING_CLOSED) * boost
        )

        # --- HVAC contribution (P11.1) -----------------------------------
        # Mechanical ventilation adds to CO₂ decay independently of the
        # door. "off"/"standby" zero it out. "auto" assumes the system
        # ramps down to a baseline air-exchange when no demand exists.
        mode = (self.hvac_mode or "auto").lower()
        if mode in HVAC_OFF_MODES:
            vent_rate = 0.0
        elif mode == "auto":
            # Auto delivers the configured rate only when occupied; idle
            # rooms get a fraction of it.
            occ_factor = 0.3 if n == 0 else 1.0
            vent_rate = max(self.ventilation_l_s_per_person, 0.0) * occ_factor
        else:  # cool / heat / fan_only / etc.
            vent_rate = max(self.ventilation_l_s_per_person, 0.0)

        if vent_rate > 0 and HVAC_VENT_DECAY_REFERENCE_L_S_PER_PERSON > 0:
            decay_per_min += (
                HVAC_VENT_DECAY_AT_REFERENCE_PER_MIN
                * (vent_rate / HVAC_VENT_DECAY_REFERENCE_L_S_PER_PERSON)
            )

        # --- CO₂: first-order mass-balance --------------------------------
        buildup = (
            CO2_PER_PERSON_PPM_PER_MIN
            * n
            * (10.0 / capacity)
            * dt_min
        )
        decay = decay_per_min * (self.co2_ppm - OUTDOOR_CO2_PPM) * dt_min
        self.co2_ppm = _clamp(
            self.co2_ppm + buildup - decay + rng.gauss(0, 2.0),
            CO2_MIN,
            CO2_MAX,
        )

        # --- Temperature -------------------------------------------------
        # Base setpoint is the room's natural target; cool/heat overrides
        # it toward the HVAC setpoint with a strong tracking term.
        target_temp = self.base_temperature_c + 0.03 * n
        setpoint = (
            self.hvac_setpoint_c
            if self.hvac_setpoint_c is not None
            else self.base_temperature_c
        )
        hvac_pull = 0.0
        assert self.temperature_c is not None
        if mode in ("cool", "heat"):
            hvac_pull = (setpoint - self.temperature_c) * HVAC_SETPOINT_TRACK_PER_MIN

        delta = (
            (target_temp - self.temperature_c) * TEMP_DRIFT_PER_MIN
            + hvac_pull
            + (outdoor_c - self.temperature_c) * outdoor_coupling
        ) * dt_min + rng.gauss(0, 0.03)
        self.temperature_c = _clamp(self.temperature_c + delta, TEMP_MIN, TEMP_MAX)

        # --- Humidity ----------------------------------------------------
        assert self.humidity_pct is not None
        target_hum = self.base_humidity_pct + 0.2 * n
        self.humidity_pct = _clamp(
            self.humidity_pct
            + (target_hum - self.humidity_pct) * HUM_DRIFT_PER_MIN * dt_min
            + rng.gauss(0, 0.3),
            HUM_MIN,
            HUM_MAX,
        )

        # --- VOC ---------------------------------------------------------
        self.voc_mg_m3 = _clamp(
            self.voc_mg_m3
            + 0.005 * n * dt_min
            - 0.01 * dt_min
            + rng.gauss(0, 0.015),
            VOC_MIN,
            VOC_MAX,
        )

        # --- PM ----------------------------------------------------------
        self.pm25_ug_m3 = _clamp(
            self.pm25_ug_m3 + rng.gauss(0, 0.3) * math.sqrt(dt_min),
            PM25_MIN,
            PM25_MAX,
        )
        self.pm10_ug_m3 = _clamp(
            max(self.pm10_ug_m3 + rng.gauss(0, 0.5) * math.sqrt(dt_min), self.pm25_ug_m3),
            PM10_MIN,
            PM10_MAX,
        )

        # --- Pressure ----------------------------------------------------
        self.pressure_pa = _clamp(
            self.pressure_pa + rng.gauss(0, 3.0) * math.sqrt(dt_min),
            PRESSURE_MIN,
            PRESSURE_MAX,
        )


def make_room_rng(building_id: str, zone_id: str) -> random.Random:
    """Stable RNG for a room, derived from building + zone identifiers.

    Two different zones in the same building get clearly different
    streams, but every run with the same identifiers reproduces the
    same trace.
    """
    import hashlib

    digest = hashlib.sha256(f"{building_id}::{zone_id}".encode("utf-8")).digest()
    seed = int.from_bytes(digest[:8], "big")
    return random.Random(seed)


__all__ = [
    "ZoneState",
    "make_room_rng",
    "OUTDOOR_CO2_PPM",
    "CO2_PER_PERSON_PPM_PER_MIN",
    "CO2_BASE_DECAY_PER_MIN",
    "CO2_DOOR_OPEN_DECAY_PER_MIN",
    "DOOR_BOOST_DECAY_MIN",
]
