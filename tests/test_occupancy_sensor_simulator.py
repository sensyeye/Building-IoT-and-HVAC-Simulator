"""Tests for ``OccupancySensorSimulator`` (PIR-style binary occupancy)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from simulator.models.config import DeviceConfig
from simulator.sensors import OccupancySensorSimulator
from simulator.services.sensor_factory import build_sensor, supported_device_types


def _device(**meta) -> DeviceConfig:
    return DeviceConfig(
        device_eui="occ-1",
        name="Meeting room PIR",
        type="occupancy_sensor",
        zone_id="z1",
        metadata=meta,
    )


def test_rejects_wrong_type():
    bad = DeviceConfig("x", "x", "iaq", "z1", {})
    with pytest.raises(ValueError):
        OccupancySensorSimulator(bad)


def test_emits_occupancy_and_count_when_occupied():
    sim = OccupancySensorSimulator(_device(false_negative_rate=0.0), seed=1)
    ts = datetime(2026, 5, 29, 10, 0, tzinfo=timezone.utc)
    r = sim.sample(ts, true_occupancy=5)
    assert r.sensor_type == "occupancy_sensor"
    assert r.data["occupancy"] is True
    assert r.data["occupant_count"] == 5
    assert r.metadata["true_occupancy"] == 5


def test_holds_after_detection_then_releases():
    sim = OccupancySensorSimulator(
        _device(hold_time_seconds=60, false_negative_rate=0.0, false_positive_rate=0.0),
        seed=1,
    )
    ts0 = datetime(2026, 5, 29, 10, 0, tzinfo=timezone.utc)
    r0 = sim.sample(ts0, true_occupancy=2)
    assert r0.data["occupancy"] is True

    # Within hold window with no real occupants → still True (latched).
    r1 = sim.sample(ts0 + timedelta(seconds=30), true_occupancy=0)
    assert r1.data["occupancy"] is True
    assert r1.metadata["held"] is True

    # Past hold window → False.
    r2 = sim.sample(ts0 + timedelta(seconds=120), true_occupancy=0)
    assert r2.data["occupancy"] is False


def test_zero_hold_means_no_latch():
    sim = OccupancySensorSimulator(
        _device(hold_time_seconds=0, false_negative_rate=0.0, false_positive_rate=0.0),
        seed=1,
    )
    ts = datetime(2026, 5, 29, 10, 0, tzinfo=timezone.utc)
    sim.sample(ts, true_occupancy=3)
    r = sim.sample(ts + timedelta(seconds=1), true_occupancy=0)
    assert r.data["occupancy"] is False


def test_rejects_negative_occupancy():
    sim = OccupancySensorSimulator(_device(), seed=1)
    with pytest.raises(ValueError):
        sim.sample(datetime.now(timezone.utc), true_occupancy=-1)


def test_report_count_can_be_disabled():
    sim = OccupancySensorSimulator(
        _device(report_count=False, false_negative_rate=0.0), seed=1
    )
    r = sim.sample(datetime.now(timezone.utc), true_occupancy=2)
    assert "occupant_count" not in r.data
    assert r.data["occupancy"] is True


def test_naive_timestamp_promoted_to_utc():
    sim = OccupancySensorSimulator(_device(false_negative_rate=0.0), seed=1)
    naive = datetime(2026, 5, 29, 10, 0)
    r = sim.sample(naive, true_occupancy=1)
    assert r.timestamp.tzinfo is not None


def test_registered_in_sensor_factory():
    assert "occupancy_sensor" in supported_device_types()
    sim = build_sensor(_device(), seed=42)
    assert isinstance(sim, OccupancySensorSimulator)


def test_false_positives_eventually_trigger_in_empty_room():
    sim = OccupancySensorSimulator(
        _device(false_positive_rate=0.5, hold_time_seconds=0), seed=7
    )
    ts0 = datetime(2026, 5, 29, 10, 0, tzinfo=timezone.utc)
    triggered = 0
    for i in range(50):
        r = sim.sample(ts0 + timedelta(seconds=i), true_occupancy=0)
        if r.data["occupancy"]:
            triggered += 1
    assert triggered > 0


def test_catalog_marks_occupancy_sensor_implemented():
    from simulator.devices.catalog import get_sensor_type

    st = get_sensor_type("occupancy_sensor")
    assert st is not None
    assert st.implemented is True
    keys = {f.key for f in st.metadata}
    assert {"hold_time_seconds", "false_negative_rate", "false_positive_rate"} <= keys
