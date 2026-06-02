"""Tests for IaqSensorSimulator (v1)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from simulator.models.config import DeviceConfig
from simulator.sensors.iaq_sensor_simulator import IaqSensorSimulator, ZoneState


TS0 = datetime(2026, 3, 2, 8, 0, 0, tzinfo=timezone.utc)


def _device(zone_id: str = "z1") -> DeviceConfig:
    return DeviceConfig(
        device_eui="iaq-001",
        name="IAQ Test",
        type="iaq",
        zone_id=zone_id,
        metadata={"base_temperature_c": 22.0, "base_humidity_pct": 50.0},
    )


def _run(sim: IaqSensorSimulator, zone: ZoneState, n: int, dt_min: int = 1):
    readings = []
    for i in range(n):
        ts = TS0 + timedelta(minutes=i * dt_min)
        readings.append(sim.sample(ts, zone))
    return readings


def test_wrong_device_type_raises():
    bad = DeviceConfig(device_eui="x", name="x", type="energy_meter", zone_id="z1")
    with pytest.raises(ValueError, match="iaq"):
        IaqSensorSimulator(bad)


def test_zone_mismatch_raises():
    sim = IaqSensorSimulator(_device("z1"), seed=1)
    with pytest.raises(ValueError, match="Zone"):
        sim.sample(TS0, ZoneState(zone_id="z2"))


def test_metric_keys_present():
    sim = IaqSensorSimulator(_device(), seed=1)
    r = sim.sample(TS0, ZoneState(zone_id="z1", occupancy=0, capacity=10))
    expected = {
        "temperature",
        "humidity",
        "co2",
        "voc",
        "pm25",
        "pm10",
        "pressure",
        "battery",
    }
    assert set(r.data.keys()) == expected
    assert r.sensor_type == "iaq"
    assert r.device_eui == "iaq-001"


def test_value_ranges_over_long_run():
    sim = IaqSensorSimulator(_device(), seed=42)
    zone = ZoneState(zone_id="z1", occupancy=4, capacity=10)
    readings = _run(sim, zone, n=240)  # 4 hours @ 1 min

    for r in readings:
        d = r.data
        assert IaqSensorSimulator.TEMP_MIN <= d["temperature"] <= IaqSensorSimulator.TEMP_MAX
        assert 30.0 <= d["humidity"] <= 75.0  # default scenario clamp
        assert IaqSensorSimulator.CO2_MIN <= d["co2"] <= IaqSensorSimulator.CO2_MAX
        assert 0.0 <= d["voc"] <= IaqSensorSimulator.VOC_MAX
        assert 0.0 <= d["pm25"] <= IaqSensorSimulator.PM25_MAX
        assert d["pm10"] >= d["pm25"]
        assert IaqSensorSimulator.PRESSURE_MIN <= d["pressure"] <= IaqSensorSimulator.PRESSURE_MAX
        assert IaqSensorSimulator.BATTERY_MIN <= d["battery"] <= IaqSensorSimulator.BATTERY_MAX


def test_co2_increases_with_occupancy():
    sim = IaqSensorSimulator(_device(), seed=7)
    zone = ZoneState(zone_id="z1", occupancy=20, capacity=10)
    readings = _run(sim, zone, n=60)
    co2_start = readings[0].data["co2"]
    co2_end = readings[-1].data["co2"]
    assert co2_end > co2_start + 100, (
        f"CO2 should rise meaningfully with high occupancy; got {co2_start} -> {co2_end}"
    )


def test_co2_decays_when_empty():
    sim = IaqSensorSimulator(_device(), seed=7)
    busy = ZoneState(zone_id="z1", occupancy=20, capacity=10)
    _run(sim, busy, n=60)  # build up CO2
    co2_high = sim.state.co2_ppm

    empty = ZoneState(zone_id="z1", occupancy=0, capacity=10)
    _run(sim, empty, n=120)  # 2h empty
    co2_low = sim.state.co2_ppm

    assert co2_low < co2_high - 100
    assert co2_low >= IaqSensorSimulator.OUTDOOR_CO2_PPM - 5  # never below outdoor floor


def test_temperature_smoothness():
    sim = IaqSensorSimulator(_device(), seed=3)
    zone = ZoneState(zone_id="z1", occupancy=2, capacity=10)
    readings = _run(sim, zone, n=120)
    temps = [r.data["temperature"] for r in readings]
    diffs = [abs(b - a) for a, b in zip(temps, temps[1:])]
    # No sudden jumps between consecutive 1-minute samples.
    assert max(diffs) < 0.5, f"max temperature jump too large: {max(diffs)}"


def test_humidity_clamped_to_default_band():
    # Force an unrealistic base; humidity must still stay inside the band.
    dev = DeviceConfig(
        device_eui="iaq-002",
        name="IAQ Bad",
        type="iaq",
        zone_id="z1",
        metadata={"base_humidity_pct": 200.0},
    )
    sim = IaqSensorSimulator(dev, seed=1)
    readings = _run(sim, ZoneState(zone_id="z1", occupancy=5, capacity=10), n=30)
    for r in readings:
        assert 30.0 <= r.data["humidity"] <= 75.0


def test_humidity_scenario_override():
    dev = DeviceConfig(
        device_eui="iaq-003",
        name="IAQ Cleanroom",
        type="iaq",
        zone_id="z1",
        metadata={
            "base_humidity_pct": 45.0,
            "humidity_min": 40.0,
            "humidity_max": 60.0,
        },
    )
    sim = IaqSensorSimulator(dev, seed=1)
    readings = _run(sim, ZoneState(zone_id="z1", occupancy=0, capacity=10), n=60)
    for r in readings:
        assert 40.0 <= r.data["humidity"] <= 60.0


def test_battery_monotonic_non_increasing():
    sim = IaqSensorSimulator(_device(), seed=5)
    zone = ZoneState(zone_id="z1", occupancy=3, capacity=10)
    readings = _run(sim, zone, n=200)
    batts = [r.data["battery"] for r in readings]
    for a, b in zip(batts, batts[1:]):
        assert b <= a + 1e-9, f"battery should not increase: {a} -> {b}"


def test_reproducible_with_seed():
    a = _run(IaqSensorSimulator(_device(), seed=123), ZoneState("z1", 5, 10), n=20)
    b = _run(IaqSensorSimulator(_device(), seed=123), ZoneState("z1", 5, 10), n=20)
    assert [r.data for r in a] == [r.data for r in b]
