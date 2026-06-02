"""Per-device measurement personality.

Two physically identical sensors mounted next to each other rarely read
exactly the same thing: one sits slightly above the supply diffuser and
reads colder, the other is closer to the door and sees more CO₂ swing,
a third is just a touch noisier than the rest of the fleet.

:class:`DevicePersonality` captures this in a deterministic, per-device,
per-metric way. The personality is **observation-only**: it does not
change the room's true state, it changes what the device *reports*.

The personality seed is derived from the device EUI so the same device
always behaves the same way across runs.

Profiles
--------
``normal``
    Baseline behaviour. Small zero-mean noise on every metric.
``slightly_noisy``
    ~3× the noise of ``normal``, no bias.
``slightly_drifty``
    Slow, signed drift on temperature/humidity that wanders over hours.
``offset``
    Steady per-device bias (e.g. a sensor that always reads ~0.6 °C high).
``near_door``
    Larger transient swings on CO₂ and temperature (proxy for sitting
    near a door). Adds a small extra noise envelope.
``near_window``
    Slight bias toward outdoor temperature is not modelled here (the
    room handles outdoor coupling); we just add a touch more thermal
    noise and a small humidity offset.
``near_hvac_supply``
    Reads cooler than the room mean and is a bit drier; small steady
    offset on temperature and humidity.

Usage
-----
    p = DevicePersonality.from_device(device, profile="near_door")
    measured_co2 = p.observe("co2_ppm", true_value=room.co2_ppm, dt_min=1.0)
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from typing import Final

from ..models.config import DeviceConfig


KNOWN_PROFILES: Final[tuple[str, ...]] = (
    "normal",
    "slightly_noisy",
    "slightly_drifty",
    "offset",
    "near_door",
    "near_window",
    "near_hvac_supply",
)


# Per-profile knobs.  ``offset`` and ``noise`` are signed/positive sigmas
# applied per metric. ``drift_rate`` is how fast (per minute) a small
# additional slow-wandering bias evolves.
_PROFILE_TABLE: Final[dict[str, dict[str, dict[str, float]]]] = {
    "normal": {
        "temperature_c":  {"offset_sigma": 0.10, "noise": 0.05, "drift_rate": 0.0},
        "humidity_pct":   {"offset_sigma": 0.50, "noise": 0.40, "drift_rate": 0.0},
        "co2_ppm":        {"offset_sigma": 5.0,  "noise": 4.0,  "drift_rate": 0.0},
        "voc_mg_m3":      {"offset_sigma": 0.02, "noise": 0.02, "drift_rate": 0.0},
        "pm25_ug_m3":     {"offset_sigma": 0.30, "noise": 0.40, "drift_rate": 0.0},
        "pm10_ug_m3":     {"offset_sigma": 0.40, "noise": 0.60, "drift_rate": 0.0},
        "pressure_pa":    {"offset_sigma": 8.0,  "noise": 5.0,  "drift_rate": 0.0},
    },
    "slightly_noisy": {
        "temperature_c":  {"offset_sigma": 0.10, "noise": 0.18, "drift_rate": 0.0},
        "humidity_pct":   {"offset_sigma": 0.50, "noise": 1.30, "drift_rate": 0.0},
        "co2_ppm":        {"offset_sigma": 5.0,  "noise": 14.0, "drift_rate": 0.0},
        "voc_mg_m3":      {"offset_sigma": 0.02, "noise": 0.06, "drift_rate": 0.0},
        "pm25_ug_m3":     {"offset_sigma": 0.30, "noise": 1.20, "drift_rate": 0.0},
        "pm10_ug_m3":     {"offset_sigma": 0.40, "noise": 1.80, "drift_rate": 0.0},
        "pressure_pa":    {"offset_sigma": 8.0,  "noise": 15.0, "drift_rate": 0.0},
    },
    "slightly_drifty": {
        "temperature_c":  {"offset_sigma": 0.10, "noise": 0.05, "drift_rate": 0.0008},
        "humidity_pct":   {"offset_sigma": 0.50, "noise": 0.40, "drift_rate": 0.004},
        "co2_ppm":        {"offset_sigma": 5.0,  "noise": 4.0,  "drift_rate": 0.05},
        "voc_mg_m3":      {"offset_sigma": 0.02, "noise": 0.02, "drift_rate": 0.0},
        "pm25_ug_m3":     {"offset_sigma": 0.30, "noise": 0.40, "drift_rate": 0.0},
        "pm10_ug_m3":     {"offset_sigma": 0.40, "noise": 0.60, "drift_rate": 0.0},
        "pressure_pa":    {"offset_sigma": 8.0,  "noise": 5.0,  "drift_rate": 0.0},
    },
    "offset": {
        # Larger steady per-device offsets, baseline noise.
        "temperature_c":  {"offset_sigma": 0.60, "noise": 0.05, "drift_rate": 0.0},
        "humidity_pct":   {"offset_sigma": 3.0,  "noise": 0.40, "drift_rate": 0.0},
        "co2_ppm":        {"offset_sigma": 30.0, "noise": 4.0,  "drift_rate": 0.0},
        "voc_mg_m3":      {"offset_sigma": 0.10, "noise": 0.02, "drift_rate": 0.0},
        "pm25_ug_m3":     {"offset_sigma": 1.5,  "noise": 0.40, "drift_rate": 0.0},
        "pm10_ug_m3":     {"offset_sigma": 2.0,  "noise": 0.60, "drift_rate": 0.0},
        "pressure_pa":    {"offset_sigma": 20.0, "noise": 5.0,  "drift_rate": 0.0},
    },
    "near_door": {
        # Door-side sensors see larger transient swings; we represent that
        # as somewhat noisier readings on temperature & CO₂, plus a small
        # bias.
        "temperature_c":  {"offset_sigma": 0.30, "noise": 0.20, "drift_rate": 0.0},
        "humidity_pct":   {"offset_sigma": 1.0,  "noise": 0.80, "drift_rate": 0.0},
        "co2_ppm":        {"offset_sigma": 10.0, "noise": 18.0, "drift_rate": 0.0},
        "voc_mg_m3":      {"offset_sigma": 0.02, "noise": 0.04, "drift_rate": 0.0},
        "pm25_ug_m3":     {"offset_sigma": 0.50, "noise": 0.80, "drift_rate": 0.0},
        "pm10_ug_m3":     {"offset_sigma": 0.70, "noise": 1.20, "drift_rate": 0.0},
        "pressure_pa":    {"offset_sigma": 8.0,  "noise": 5.0,  "drift_rate": 0.0},
    },
    "near_window": {
        # Slightly more thermal noise (radiation, drafts) and a small
        # humidity bias.
        "temperature_c":  {"offset_sigma": 0.40, "noise": 0.15, "drift_rate": 0.0},
        "humidity_pct":   {"offset_sigma": 2.0,  "noise": 0.60, "drift_rate": 0.0},
        "co2_ppm":        {"offset_sigma": 5.0,  "noise": 4.0,  "drift_rate": 0.0},
        "voc_mg_m3":      {"offset_sigma": 0.02, "noise": 0.02, "drift_rate": 0.0},
        "pm25_ug_m3":     {"offset_sigma": 0.30, "noise": 0.40, "drift_rate": 0.0},
        "pm10_ug_m3":     {"offset_sigma": 0.40, "noise": 0.60, "drift_rate": 0.0},
        "pressure_pa":    {"offset_sigma": 8.0,  "noise": 5.0,  "drift_rate": 0.0},
    },
    "near_hvac_supply": {
        # Reads cooler & drier than the room mean.
        "temperature_c":  {"offset_sigma": 0.10, "noise": 0.08, "drift_rate": 0.0,
                            "bias": -0.8},
        "humidity_pct":   {"offset_sigma": 0.50, "noise": 0.50, "drift_rate": 0.0,
                            "bias": -3.0},
        "co2_ppm":        {"offset_sigma": 5.0,  "noise": 4.0,  "drift_rate": 0.0},
        "voc_mg_m3":      {"offset_sigma": 0.02, "noise": 0.02, "drift_rate": 0.0},
        "pm25_ug_m3":     {"offset_sigma": 0.30, "noise": 0.40, "drift_rate": 0.0},
        "pm10_ug_m3":     {"offset_sigma": 0.40, "noise": 0.60, "drift_rate": 0.0},
        "pressure_pa":    {"offset_sigma": 8.0,  "noise": 5.0,  "drift_rate": 0.0},
    },
}


def normalize_profile(name: str | None) -> str:
    if not name:
        return "normal"
    n = str(name).strip().lower()
    return n if n in _PROFILE_TABLE else "normal"


def _seed_from_eui(eui: str, salt: str = "") -> int:
    digest = hashlib.sha256(f"{eui}::{salt}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


@dataclass
class DevicePersonality:
    """Steady offsets + per-tick noise + slow drift, per metric."""

    profile: str
    device_eui: str
    # Per-metric steady offset, drawn once from a Gaussian at __post_init__.
    offsets: dict[str, float] = field(default_factory=dict)
    # Slow-wandering bias (random walk) per metric.
    drift: dict[str, float] = field(default_factory=dict)
    # Internal RNG, seeded from device EUI for reproducibility.
    # ``None`` triggers a deterministic seed in ``__post_init__``.
    rng: random.Random | None = None

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def __post_init__(self) -> None:
        profile = normalize_profile(self.profile)
        self.profile = profile
        if self.rng is None:
            self.rng = random.Random(_seed_from_eui(self.device_eui, "personality"))

        table = _PROFILE_TABLE[profile]
        # One stable offset per metric, drawn from offset_sigma + optional bias.
        offset_rng = random.Random(_seed_from_eui(self.device_eui, f"offset::{profile}"))
        for metric, knobs in table.items():
            sigma = float(knobs.get("offset_sigma", 0.0))
            bias = float(knobs.get("bias", 0.0))
            self.offsets.setdefault(metric, bias + offset_rng.gauss(0, sigma))
            self.drift.setdefault(metric, 0.0)

    @classmethod
    def from_device(
        cls,
        device: DeviceConfig,
        *,
        profile: str | None = None,
    ) -> "DevicePersonality":
        # Profile precedence: explicit arg > device.metadata > "normal".
        meta = device.metadata or {}
        chosen = profile or meta.get("personality") or "normal"
        return cls(profile=str(chosen), device_eui=device.device_eui)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def observe(self, metric: str, true_value: float, dt_min: float = 1.0) -> float:
        """Return what this device would *report* given the room's true value."""
        table = _PROFILE_TABLE[self.profile]
        knobs = table.get(metric)
        if knobs is None:
            return float(true_value)

        # Advance slow drift (mean-reverting random walk).
        drift_rate = float(knobs.get("drift_rate", 0.0))
        if drift_rate > 0 and dt_min > 0:
            cur = self.drift[metric]
            # Brownian step pulled gently back toward 0 so it doesn't run away.
            cur = cur * 0.999 + self.rng.gauss(0, drift_rate) * (dt_min ** 0.5)
            self.drift[metric] = cur

        noise = float(knobs.get("noise", 0.0))
        measured = (
            float(true_value)
            + self.offsets.get(metric, 0.0)
            + self.drift.get(metric, 0.0)
            + (self.rng.gauss(0, noise) if noise > 0 else 0.0)
        )
        return measured


__all__ = [
    "DevicePersonality",
    "KNOWN_PROFILES",
    "normalize_profile",
]
