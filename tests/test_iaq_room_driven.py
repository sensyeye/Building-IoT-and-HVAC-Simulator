"""Tests for P10.3 — IaqSensorSimulator in room-driven mode + personality."""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

from simulator.models.config import DeviceConfig
from simulator.sensors.iaq_sensor_simulator import IaqSensorSimulator
from simulator.sensors.zone_state import ZoneState


TS0 = datetime(2026, 6, 1, 9, 0, 0, tzinfo=timezone.utc)


def _device(eui: str, *, profile: str | None = None) -> DeviceConfig:
    meta: dict = {"base_temperature_c": 22.0, "base_humidity_pct": 50.0}
    if profile:
        meta["personality"] = profile
    return DeviceConfig(
        device_eui=eui, name=eui, type="iaq", zone_id="z1", metadata=meta,
    )


def test_room_driven_reads_zone_values():
    """In room_driven mode, IAQ should reflect the zone's authoritative state."""
    sim = IaqSensorSimulator(_device("eui-A"), seed=1)
    zone = ZoneState(zone_id="z1", capacity=10, co2_ppm=1100.0, temperature_c=24.0,
                     humidity_pct=55.0)
    r = sim.sample(TS0, zone, room_driven=True)
    # Personality is "normal" so values land within tight noise of the truth.
    assert abs(r.data["co2"] - 1100.0) < 30.0
    assert abs(r.data["temperature"] - 24.0) < 1.0
    assert abs(r.data["humidity"] - 55.0) < 2.5


def test_room_driven_does_not_run_device_physics():
    """Without ticking the room, repeated samples shouldn't grow CO₂."""
    sim = IaqSensorSimulator(_device("eui-B"), seed=2)
    zone = ZoneState(zone_id="z1", occupancy=20, capacity=10, co2_ppm=500.0)
    readings = [
        sim.sample(TS0 + timedelta(minutes=i), zone, room_driven=True)
        for i in range(30)
    ]
    co2s = [r.data["co2"] for r in readings]
    # All within personality noise of the room's static 500 ppm.
    assert max(co2s) - min(co2s) < 60.0
    assert abs(statistics.mean(co2s) - 500.0) < 30.0


def test_two_devices_same_room_show_small_differences():
    """Acceptance from roadmap: same room, two devices ≠ identical readings."""
    a = IaqSensorSimulator(_device("eui-A1"), seed=10)
    b = IaqSensorSimulator(_device("eui-A2"), seed=11)
    zone = ZoneState(zone_id="z1", capacity=10, co2_ppm=900.0, temperature_c=22.5,
                     humidity_pct=48.0)
    ra = [a.sample(TS0 + timedelta(minutes=i), zone, room_driven=True).data
          for i in range(20)]
    rb = [b.sample(TS0 + timedelta(minutes=i), zone, room_driven=True).data
          for i in range(20)]
    mean_temp_a = statistics.mean(r["temperature"] for r in ra)
    mean_temp_b = statistics.mean(r["temperature"] for r in rb)
    mean_co2_a = statistics.mean(r["co2"] for r in ra)
    mean_co2_b = statistics.mean(r["co2"] for r in rb)
    # Different but close.
    assert ra != rb
    assert abs(mean_temp_a - mean_temp_b) < 1.0
    assert abs(mean_co2_a - mean_co2_b) < 80.0


def test_near_hvac_supply_device_reads_cooler_than_normal_sibling():
    normal = IaqSensorSimulator(_device("eui-N1"), seed=20)
    cool = IaqSensorSimulator(_device("eui-N2", profile="near_hvac_supply"), seed=21)
    zone = ZoneState(zone_id="z1", capacity=10, temperature_c=23.0)
    n_temps = [normal.sample(TS0 + timedelta(minutes=i), zone, room_driven=True)
               .data["temperature"] for i in range(60)]
    c_temps = [cool.sample(TS0 + timedelta(minutes=i), zone, room_driven=True)
               .data["temperature"] for i in range(60)]
    assert statistics.mean(c_temps) < statistics.mean(n_temps) - 0.3


def test_personality_metadata_is_picked_up_from_device():
    sim = IaqSensorSimulator(_device("eui-X", profile="near_door"), seed=1)
    assert sim.personality.profile == "near_door"


def test_legacy_standalone_mode_still_produces_data():
    """Default ``room_driven=False`` keeps the original test contract."""
    sim = IaqSensorSimulator(_device("eui-Legacy"), seed=99)
    zone = ZoneState(zone_id="z1", occupancy=5, capacity=10)
    readings = [sim.sample(TS0 + timedelta(minutes=i), zone) for i in range(10)]
    keys = {"temperature", "humidity", "co2", "voc", "pm25", "pm10", "pressure", "battery"}
    assert all(set(r.data.keys()) == keys for r in readings)
