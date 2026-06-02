"""Shared fixtures for validator tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from simulator.models.reading import SensorReading


def _ts(minute_offset: int) -> datetime:
    return datetime(2026, 3, 2, 8, 0, tzinfo=timezone.utc) + timedelta(
        minutes=minute_offset
    )


def make_iaq(
    minute: int,
    *,
    co2: float = 500,
    temperature: float = 22.0,
    humidity: float = 45.0,
    pm25: float = 8.0,
    pm10: float = 12.0,
    occupancy: int | None = None,
    zone_id: str = "z1",
    device_eui: str = "iaq-1",
) -> SensorReading:
    meta: dict[str, Any] = {"zone_id": zone_id}
    if occupancy is not None:
        meta["occupancy"] = occupancy
    return SensorReading(
        device_eui=device_eui,
        sensor_type="iaq",
        timestamp=_ts(minute),
        data={
            "co2": co2,
            "temperature": temperature,
            "humidity": humidity,
            "pm25": pm25,
            "pm10": pm10,
        },
        metadata=meta,
    )


def make_energy(
    minute: int,
    *,
    active_power: float = 5.0,
    active_energy: float = 0.0,
    device_eui: str = "em-main",
    submeter: str | None = None,
) -> SensorReading:
    meta: dict[str, Any] = {}
    if submeter:
        meta["submeter"] = submeter
    return SensorReading(
        device_eui=device_eui,
        sensor_type="energy_meter",
        timestamp=_ts(minute),
        data={"active_power": active_power, "active_energy": active_energy},
        metadata=meta,
    )


def good_iaq_series(minutes: int = 30) -> list[SensorReading]:
    """Smooth IAQ series where CO2 tracks occupancy."""
    out: list[SensorReading] = []
    co2 = 450.0
    for m in range(minutes):
        # occupancy ramps up then down
        occ = 0 if m < 5 else (5 if m < 15 else (3 if m < 22 else 0))
        # simple first-order toward target
        target = 450 + 80 * occ
        co2 += (target - co2) * 0.25
        out.append(make_iaq(m, co2=co2, occupancy=occ))
    return out
