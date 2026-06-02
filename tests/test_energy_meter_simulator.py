"""Tests for :class:`EnergyMeterSimulator`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from simulator.models.config import DeviceConfig
from simulator.sensors import EnergyContext, EnergyMeterSimulator


def _device(submeter: str = "main", **meta) -> DeviceConfig:
    md = {"submeter": submeter, "nominal_kw": 10.0, **meta}
    return DeviceConfig(
        device_eui=f"em-{submeter}",
        name=f"{submeter} meter",
        type="energy_meter",
        zone_id="z1",
        metadata=md,
    )


def _run(
    sim: EnergyMeterSimulator,
    *,
    start: datetime,
    minutes: int,
    step_min: int = 1,
    ctx_for=None,
):
    out = []
    for m in range(0, minutes, step_min):
        ts = start + timedelta(minutes=m)
        ctx = ctx_for(ts) if ctx_for else EnergyContext()
        out.append(sim.sample(ts, ctx))
    return out


# ---------------------------------------------------------------------------
# Construction / validation
# ---------------------------------------------------------------------------

def test_rejects_wrong_device_type():
    bad = DeviceConfig("x", "x", "iaq", "z1", {})
    with pytest.raises(ValueError):
        EnergyMeterSimulator(bad)


def test_rejects_invalid_pf_band():
    with pytest.raises(ValueError):
        EnergyMeterSimulator(_device(power_factor_min=0.9, power_factor_nominal=0.95, power_factor_max=0.8))


# ---------------------------------------------------------------------------
# Acceptance test 1: active_energy is monotonic non-decreasing
# ---------------------------------------------------------------------------

def test_active_energy_is_monotonic():
    sim = EnergyMeterSimulator(_device("hvac"), seed=0)
    start = datetime(2026, 4, 6, 0, 0, tzinfo=timezone.utc)  # Mon midnight
    readings = _run(
        sim,
        start=start,
        minutes=24 * 60,
        step_min=5,
        ctx_for=lambda ts: EnergyContext(
            outdoor_temperature_c=10 + 8 * (ts.hour / 24),
            occupancy=8 if 9 <= ts.hour < 17 else 0,
            occupancy_capacity=20,
        ),
    )
    energies = [r.data["active_energy"] for r in readings]
    for prev, curr in zip(energies, energies[1:]):
        assert curr >= prev, f"active_energy decreased: {prev} -> {curr}"
    # And it should actually grow over the day.
    assert energies[-1] > energies[0]


# ---------------------------------------------------------------------------
# Acceptance test 2: weekday daytime > night for every submeter
# ---------------------------------------------------------------------------

def _avg_power_in_window(sim, day_start, hour_lo, hour_hi, occ):
    """Average active_power across the [hour_lo, hour_hi) window of a day."""
    powers = []
    for h in range(hour_lo, hour_hi):
        for m in (0, 15, 30, 45):
            ts = day_start.replace(hour=h, minute=m)
            r = sim.sample(
                ts,
                EnergyContext(
                    outdoor_temperature_c=5.0,  # cold -> HVAC has demand to show
                    occupancy=occ if hour_lo >= 8 else 0,
                    occupancy_capacity=20,
                ),
            )
            powers.append(r.data["active_power"])
    return sum(powers) / len(powers)


@pytest.mark.parametrize("submeter", ["main", "hvac", "lighting", "plug"])
def test_weekday_daytime_power_higher_than_night(submeter):
    monday = datetime(2026, 4, 6, 0, 0, tzinfo=timezone.utc)  # Monday

    # Two independent simulators so we don't carry state between windows.
    sim_night = EnergyMeterSimulator(_device(submeter), seed=1)
    sim_day = EnergyMeterSimulator(_device(submeter), seed=1)

    night_avg = _avg_power_in_window(sim_night, monday, 1, 5, occ=0)
    day_avg = _avg_power_in_window(sim_day, monday, 10, 14, occ=15)

    assert day_avg > night_avg * 1.5, (
        f"{submeter}: expected daytime power >> night (day={day_avg:.3f}, "
        f"night={night_avg:.3f})"
    )


# ---------------------------------------------------------------------------
# Acceptance test 3: value ranges
# ---------------------------------------------------------------------------

def test_value_ranges_stay_realistic():
    sim = EnergyMeterSimulator(_device("main"), seed=2)
    start = datetime(2026, 4, 6, 0, 0, tzinfo=timezone.utc)
    readings = _run(
        sim,
        start=start,
        minutes=24 * 60,
        step_min=1,
        ctx_for=lambda ts: EnergyContext(
            outdoor_temperature_c=15.0,
            occupancy=10 if 9 <= ts.hour < 17 else 0,
            occupancy_capacity=20,
        ),
    )

    for r in readings:
        d = r.data
        # Power
        assert d["active_power"] >= 0.0
        assert d["active_power"] <= 12.0  # nominal_kw=10 with some headroom
        assert d["apparent_power"] >= d["active_power"] - 1e-6  # |S| >= |P|
        # Power factor band
        assert 0.85 <= d["power_factor"] <= 1.0
        # Voltages within ±5% of 230V nominal
        for k in ("voltage_1", "voltage_2", "voltage_3"):
            assert 218.0 <= d[k] <= 242.0, f"{k}={d[k]} out of range"
        # Currents non-negative and bounded by P/(3*V*pf_min) headroom
        for k in ("current_1", "current_2", "current_3"):
            assert d[k] >= 0.0
            assert d[k] < 100.0  # 10kW/(3*230*0.85) ≈ 17A; well below 100
        # Frequency tight band
        assert 49.5 <= d["frequency"] <= 50.5


def test_hvac_responds_to_outdoor_temperature():
    """Cold day should drive higher HVAC power than mild day at same hour."""
    monday = datetime(2026, 4, 6, 12, 0, tzinfo=timezone.utc)
    sim_cold = EnergyMeterSimulator(_device("hvac"), seed=3)
    sim_mild = EnergyMeterSimulator(_device("hvac"), seed=3)
    # warm up to steady state
    for m in range(30):
        ts = monday + timedelta(minutes=m - 30)
        sim_cold.sample(ts, EnergyContext(outdoor_temperature_c=-5.0, occupancy=10, occupancy_capacity=20))
        sim_mild.sample(ts, EnergyContext(outdoor_temperature_c=22.0, occupancy=10, occupancy_capacity=20))

    cold = sim_cold.sample(monday, EnergyContext(outdoor_temperature_c=-5.0, occupancy=10, occupancy_capacity=20))
    mild = sim_mild.sample(monday, EnergyContext(outdoor_temperature_c=22.0, occupancy=10, occupancy_capacity=20))
    assert cold.data["active_power"] > mild.data["active_power"]


def test_plug_responds_to_occupancy():
    monday = datetime(2026, 4, 6, 12, 0, tzinfo=timezone.utc)
    sim_empty = EnergyMeterSimulator(_device("plug"), seed=4)
    sim_full = EnergyMeterSimulator(_device("plug"), seed=4)
    for m in range(30):
        ts = monday + timedelta(minutes=m - 30)
        sim_empty.sample(ts, EnergyContext(occupancy=0, occupancy_capacity=20))
        sim_full.sample(ts, EnergyContext(occupancy=18, occupancy_capacity=20))

    r_empty = sim_empty.sample(monday, EnergyContext(occupancy=0, occupancy_capacity=20))
    r_full = sim_full.sample(monday, EnergyContext(occupancy=18, occupancy_capacity=20))
    assert r_full.data["active_power"] > r_empty.data["active_power"] * 2


def test_reading_metadata_includes_submeter_and_zone():
    sim = EnergyMeterSimulator(_device("hvac"), seed=5)
    r = sim.sample(
        datetime(2026, 4, 6, 12, 0, tzinfo=timezone.utc),
        EnergyContext(occupancy=5, occupancy_capacity=10),
    )
    assert r.sensor_type == "energy_meter"
    assert r.metadata["submeter"] == "hvac"
    assert r.metadata["zone_id"] == "z1"
    assert set(r.data.keys()) == {
        "active_power",
        "apparent_power",
        "active_energy",
        "voltage_1",
        "voltage_2",
        "voltage_3",
        "current_1",
        "current_2",
        "current_3",
        "power_factor",
        "frequency",
    }
