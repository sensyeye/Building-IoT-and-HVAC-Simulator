"""Tests for ``DoorContactSimulator``."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from simulator.models.config import DeviceConfig
from simulator.sensors import DoorContactSimulator
from simulator.services.sensor_factory import build_sensor, supported_device_types


def _device(**meta) -> DeviceConfig:
    return DeviceConfig(
        device_eui="door-1",
        name="Meeting room door",
        type="door_contact",
        zone_id="z1",
        metadata=meta,
    )


def test_rejects_wrong_type():
    bad = DeviceConfig("x", "x", "iaq", "z1", {})
    with pytest.raises(ValueError):
        DoorContactSimulator(bad)


def test_emits_canonical_keys():
    sim = DoorContactSimulator(_device(), seed=1)
    r = sim.sample(datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc), true_occupancy=2)
    assert r.sensor_type == "door_contact"
    assert set(r.data.keys()) == {"door_state", "periodic_open_events", "total_open_events"}
    assert r.metadata["zone_id"] == "z1"


def test_no_events_when_rates_zero():
    sim = DoorContactSimulator(
        _device(base_open_rate_per_hour=0, occupied_open_rate_per_hour=0),
        seed=1,
    )
    ts = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    for i in range(20):
        r = sim.sample(ts + timedelta(minutes=i), true_occupancy=10)
        assert r.data["periodic_open_events"] == 0
        assert r.data["door_state"] is False
    assert r.data["total_open_events"] == 0


def test_occupied_room_eventually_opens_door():
    sim = DoorContactSimulator(
        _device(
            base_open_rate_per_hour=0,
            occupied_open_rate_per_hour=20,
            activity_peak_hour=12,
            activity_width_hours=12,  # flat envelope
        ),
        seed=42,
    )
    ts = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    total = 0
    for i in range(60):
        r = sim.sample(ts + timedelta(minutes=i), true_occupancy=5)
        total += r.data["periodic_open_events"]
    assert total > 0
    assert r.data["total_open_events"] == total


def test_total_open_events_is_monotonic():
    sim = DoorContactSimulator(
        _device(base_open_rate_per_hour=5, occupied_open_rate_per_hour=5),
        seed=3,
    )
    ts = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    last = 0
    for i in range(30):
        r = sim.sample(ts + timedelta(minutes=i), true_occupancy=2)
        assert r.data["total_open_events"] >= last
        last = r.data["total_open_events"]


def test_door_state_closes_after_dwell():
    sim = DoorContactSimulator(
        _device(
            base_open_rate_per_hour=0,
            occupied_open_rate_per_hour=1000,  # virtually guaranteed open
            open_duration_seconds_mean=5,
            open_duration_seconds_std=0,
        ),
        seed=1,
    )
    ts = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    r0 = sim.sample(ts, true_occupancy=10)
    assert r0.data["door_state"] is True
    # Long gap with zero rate (no occupants, zero base rate) → must close.
    sim2 = sim  # reuse state
    sim2.base_open_rate_per_hour = 0
    sim2.occupied_open_rate_per_hour = 0
    r1 = sim2.sample(ts + timedelta(seconds=60), true_occupancy=0)
    assert r1.data["door_state"] is False
    assert r1.data["periodic_open_events"] == 0


def test_rejects_negative_occupancy():
    sim = DoorContactSimulator(_device(), seed=1)
    with pytest.raises(ValueError):
        sim.sample(datetime.now(timezone.utc), true_occupancy=-1)


def test_naive_timestamp_promoted_to_utc():
    sim = DoorContactSimulator(_device(), seed=1)
    r = sim.sample(datetime(2026, 5, 29, 12, 0), true_occupancy=0)
    assert r.timestamp.tzinfo is not None


def test_initial_total_open_events_seeded():
    sim = DoorContactSimulator(_device(initial_total_open_events=42), seed=1)
    r = sim.sample(datetime.now(timezone.utc), true_occupancy=0)
    assert r.data["total_open_events"] >= 42


def test_registered_in_sensor_factory():
    assert "door_contact" in supported_device_types()
    sim = build_sensor(_device(), seed=7)
    assert isinstance(sim, DoorContactSimulator)


def test_catalog_marks_door_contact_implemented():
    from simulator.devices.catalog import get_sensor_type

    st = get_sensor_type("door_contact")
    assert st is not None
    assert st.implemented is True
    keys = {f.key for f in st.metadata}
    assert {
        "base_open_rate_per_hour",
        "occupied_open_rate_per_hour",
        "open_duration_seconds_mean",
    } <= keys


def test_metric_mapper_handles_door_contact():
    from simulator.integrations import SensgreenMetricMapper

    m = SensgreenMetricMapper()
    out = m.map(
        "door_contact",
        {"door_state": True, "periodic_open_events": 2, "total_open_events": 10},
    )
    assert out == {
        "open_status": True,
        "periodic_counter_in": 2,
        "total_counter_in": 10,
    }
