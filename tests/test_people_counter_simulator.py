"""Tests for ``PeopleCounterSimulator`` and ``EntryExitCounterSimulator``."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from simulator.models.config import DeviceConfig
from simulator.sensors import EntryExitCounterSimulator, PeopleCounterSimulator


# ---------------------------------------------------------------------------
# PeopleCounterSimulator
# ---------------------------------------------------------------------------

def _pc_device(**meta) -> DeviceConfig:
    return DeviceConfig(
        device_eui="pc-1",
        name="Lobby presence",
        type="people_counter",
        zone_id="z1",
        metadata=meta,
    )


def test_people_counter_rejects_wrong_type():
    bad = DeviceConfig("x", "x", "iaq", "z1", {})
    with pytest.raises(ValueError):
        PeopleCounterSimulator(bad)


def test_people_counter_zero_when_empty():
    sim = PeopleCounterSimulator(_pc_device(miscount_std=0.0), seed=0)
    r = sim.sample(datetime(2026, 4, 6, 10, tzinfo=timezone.utc), true_occupancy=0)
    assert r.data["people_count"] == 0
    assert r.data["occupancy"] is False


def test_people_counter_occupancy_flag_true_when_present():
    sim = PeopleCounterSimulator(_pc_device(miscount_std=0.0), seed=0)
    r = sim.sample(datetime(2026, 4, 6, 10, tzinfo=timezone.utc), true_occupancy=4)
    assert r.data["people_count"] == 4
    assert r.data["occupancy"] is True


def test_people_counter_never_negative():
    sim = PeopleCounterSimulator(_pc_device(miscount_std=2.0), seed=42)
    start = datetime(2026, 4, 6, 8, tzinfo=timezone.utc)
    for m in range(200):
        ts = start + timedelta(minutes=m)
        # Drive with a true occupancy of 0 — noise must NOT push us below zero.
        r = sim.sample(ts, true_occupancy=0)
        assert r.data["people_count"] >= 0


def test_people_counter_capacity_clamp():
    sim = PeopleCounterSimulator(_pc_device(miscount_std=0.0, capacity=5), seed=0)
    r = sim.sample(datetime(2026, 4, 6, 10, tzinfo=timezone.utc), true_occupancy=20)
    assert r.data["people_count"] == 5


def test_people_counter_metadata():
    sim = PeopleCounterSimulator(_pc_device(), seed=0)
    r = sim.sample(datetime(2026, 4, 6, 10, tzinfo=timezone.utc), true_occupancy=3)
    assert r.sensor_type == "people_counter"
    assert r.metadata["zone_id"] == "z1"
    assert r.metadata["true_occupancy"] == 3


# ---------------------------------------------------------------------------
# EntryExitCounterSimulator
# ---------------------------------------------------------------------------

def _ec_device(**meta) -> DeviceConfig:
    md = {"peak_flow_per_min": 0.8, **meta}
    return DeviceConfig(
        device_eui="ec-1",
        name="Main entrance",
        type="entry_exit_counter",
        zone_id="building",
        metadata=md,
    )


def _run_day(sim, day: datetime, step_min: int = 5):
    out = []
    for m in range(0, 24 * 60, step_min):
        out.append(sim.sample(day + timedelta(minutes=m)))
    return out


def test_entry_exit_rejects_wrong_type():
    bad = DeviceConfig("x", "x", "iaq", "z1", {})
    with pytest.raises(ValueError):
        EntryExitCounterSimulator(bad)


def test_total_counters_are_monotonic():
    sim = EntryExitCounterSimulator(_ec_device(), seed=1)
    monday = datetime(2026, 4, 6, 0, 0, tzinfo=timezone.utc)
    readings = _run_day(sim, monday, step_min=2)

    prev_in = -1
    prev_out = -1
    for r in readings:
        ti = r.data["total_counter_in"]
        to = r.data["total_counter_out"]
        assert ti >= prev_in, f"total_counter_in went backwards: {prev_in} -> {ti}"
        assert to >= prev_out, f"total_counter_out went backwards: {prev_out} -> {to}"
        prev_in, prev_out = ti, to


def test_estimated_occupancy_never_negative():
    # Force a huge synthetic outflow against a tiny inflow; the simulator
    # must cap exits so estimated_occupancy stays >= 0.
    sim = EntryExitCounterSimulator(_ec_device(), seed=2)
    start = datetime(2026, 4, 6, 0, 0, tzinfo=timezone.utc)
    for m in range(60):
        r = sim.sample(start + timedelta(minutes=m), flow_in=1, flow_out=10)
        assert r.metadata["estimated_occupancy"] >= 0
        assert r.data["total_counter_out"] <= r.data["total_counter_in"] + sim.state.estimated_occupancy + 1


def test_periodic_counts_non_negative_and_integer():
    sim = EntryExitCounterSimulator(_ec_device(), seed=3)
    monday = datetime(2026, 4, 6, 0, 0, tzinfo=timezone.utc)
    for r in _run_day(sim, monday, step_min=5):
        for k in ("periodic_counter_in", "periodic_counter_out"):
            v = r.data[k]
            assert isinstance(v, int)
            assert v >= 0


def test_morning_more_in_than_out():
    sim = EntryExitCounterSimulator(_ec_device(), seed=4)
    monday = datetime(2026, 4, 6, 0, 0, tzinfo=timezone.utc)
    readings = _run_day(sim, monday, step_min=5)
    morning = [r for r in readings if 7 <= r.timestamp.hour < 11]
    assert morning, "expected morning readings"
    in_sum = sum(r.data["periodic_counter_in"] for r in morning)
    out_sum = sum(r.data["periodic_counter_out"] for r in morning)
    assert in_sum > 2 * out_sum, (
        f"morning should be entry-dominated (in={in_sum}, out={out_sum})"
    )


def test_evening_more_out_than_in():
    sim = EntryExitCounterSimulator(_ec_device(), seed=5)
    monday = datetime(2026, 4, 6, 0, 0, tzinfo=timezone.utc)
    readings = _run_day(sim, monday, step_min=5)
    evening = [r for r in readings if 15 <= r.timestamp.hour < 19]
    assert evening
    in_sum = sum(r.data["periodic_counter_in"] for r in evening)
    out_sum = sum(r.data["periodic_counter_out"] for r in evening)
    assert out_sum > 2 * in_sum, (
        f"evening should be exit-dominated (in={in_sum}, out={out_sum})"
    )


def test_estimated_occupancy_returns_to_zero_by_end_of_day():
    """Over a full day, in/out totals should roughly balance. Exits are
    capped so they cannot exceed entries; we assert the day ends with
    a small residual rather than a huge one."""
    sim = EntryExitCounterSimulator(_ec_device(peak_flow_per_min=1.5), seed=6)
    monday = datetime(2026, 4, 6, 0, 0, tzinfo=timezone.utc)
    last = _run_day(sim, monday, step_min=2)[-1]
    occ_end = last.metadata["estimated_occupancy"]
    total_in = last.data["total_counter_in"]
    # Residual occupancy should be a small fraction of daily traffic.
    assert occ_end <= max(5, total_in * 0.20)


def test_explicit_flow_overrides():
    sim = EntryExitCounterSimulator(_ec_device(), seed=7)
    ts = datetime(2026, 4, 6, 9, 0, tzinfo=timezone.utc)
    r = sim.sample(ts, flow_in=4, flow_out=1)
    assert r.data["periodic_counter_in"] == 4
    assert r.data["periodic_counter_out"] == 1
    assert r.data["total_counter_in"] == 4
    assert r.data["total_counter_out"] == 1
    assert r.metadata["estimated_occupancy"] == 3


def test_reading_shape():
    sim = EntryExitCounterSimulator(_ec_device(), seed=8)
    r = sim.sample(datetime(2026, 4, 6, 9, 0, tzinfo=timezone.utc))
    assert r.sensor_type == "entry_exit_counter"
    assert set(r.data.keys()) == {
        "periodic_counter_in",
        "periodic_counter_out",
        "total_counter_in",
        "total_counter_out",
        "net_occupancy",
    }
    assert r.metadata["zone_id"] == "building"
    assert "estimated_occupancy" in r.metadata


def test_net_occupancy_matches_metadata():
    sim = EntryExitCounterSimulator(_ec_device(), seed=9)
    ts = datetime(2026, 4, 6, 9, 0, tzinfo=timezone.utc)
    r = sim.sample(ts, flow_in=7, flow_out=2)
    assert r.data["net_occupancy"] == r.metadata["estimated_occupancy"] == 5
    r2 = sim.sample(ts + timedelta(minutes=1), flow_in=0, flow_out=3)
    assert r2.data["net_occupancy"] == r2.metadata["estimated_occupancy"] == 2


def test_net_occupancy_can_be_disabled():
    sim = EntryExitCounterSimulator(_ec_device(report_net_occupancy=False), seed=10)
    r = sim.sample(datetime(2026, 4, 6, 9, 0, tzinfo=timezone.utc), flow_in=3, flow_out=1)
    assert "net_occupancy" not in r.data
    # Metadata still carries it for downstream consumers.
    assert r.metadata["estimated_occupancy"] == 2


def test_metric_mapper_translates_net_occupancy():
    from simulator.integrations import SensgreenMetricMapper

    m = SensgreenMetricMapper()
    out = m.map(
        "entry_exit_counter",
        {
            "periodic_counter_in": 5,
            "periodic_counter_out": 2,
            "total_counter_in": 100,
            "total_counter_out": 80,
            "net_occupancy": 20,
        },
    )
    assert out["people_count"] == 20
    assert out["periodic_counter_in"] == 5
    assert out["total_counter_out"] == 80
