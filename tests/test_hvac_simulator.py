"""Tests for P11.2 — HvacVirtualSimulator + wiring."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from simulator.devices.catalog import get_sensor_type
from simulator.integrations.sensgreen_metric_mapper import SensgreenMetricMapper
from simulator.models.config import DeviceConfig
from simulator.sensors.hvac_simulator import HvacVirtualSimulator, VALID_MODES
from simulator.sensors.zone_state import ZoneState
from simulator.services.sensor_factory import build_sensor


TS_DAY_HOT = datetime(2026, 6, 1, 14, 0, 0, tzinfo=timezone.utc)  # Mon 14:00
TS_NIGHT = datetime(2026, 6, 1, 23, 0, 0, tzinfo=timezone.utc)
TS_WEEKEND = datetime(2026, 6, 6, 14, 0, 0, tzinfo=timezone.utc)  # Sat 14:00
TS_DAY_COLD = datetime(2026, 1, 12, 9, 0, 0, tzinfo=timezone.utc)


def _device(zone_id: str = "z1", **meta) -> DeviceConfig:
    return DeviceConfig(
        device_eui="hvac-001", name="HVAC 1", type="hvac", zone_id=zone_id,
        metadata=meta,
    )


def _zone() -> ZoneState:
    return ZoneState(zone_id="z1", capacity=10, temperature_c=24.0)


# --- catalog -----------------------------------------------------------------
def test_catalog_hvac_is_now_implemented():
    st = get_sensor_type("hvac")
    assert st is not None and st.implemented is True


def test_factory_builds_hvac_simulator():
    dev = _device()
    sim = build_sensor(dev, seed=1)
    assert isinstance(sim, HvacVirtualSimulator)


# --- mode selection ----------------------------------------------------------
def test_business_hours_hot_outdoor_selects_cool():
    sim = HvacVirtualSimulator(_device(setpoint_c=22.0), seed=1)
    r = sim.sample(TS_DAY_HOT, _zone(), outdoor_c=33.0)
    assert r.data["mode"] == "cool"


def test_business_hours_cold_outdoor_selects_heat():
    sim = HvacVirtualSimulator(_device(setpoint_c=22.0), seed=1)
    r = sim.sample(TS_DAY_COLD, _zone(), outdoor_c=-3.0)
    assert r.data["mode"] == "heat"


def test_outside_business_hours_goes_to_standby():
    sim = HvacVirtualSimulator(_device(), seed=1)
    r = sim.sample(TS_NIGHT, _zone(), outdoor_c=18.0)
    assert r.data["mode"] == "standby"


def test_weekend_default_is_standby():
    sim = HvacVirtualSimulator(_device(), seed=1)
    r = sim.sample(TS_WEEKEND, _zone(), outdoor_c=30.0)
    assert r.data["mode"] == "standby"


def test_mode_override_takes_precedence():
    sim = HvacVirtualSimulator(_device(mode_override="heat"), seed=1)
    r = sim.sample(TS_DAY_HOT, _zone(), outdoor_c=33.0)
    assert r.data["mode"] == "heat"


# --- room driving ------------------------------------------------------------
def test_sample_drives_zone_hvac_fields():
    sim = HvacVirtualSimulator(_device(setpoint_c=21.0), seed=1)
    z = _zone()
    sim.sample(TS_DAY_HOT, z, outdoor_c=33.0)
    assert z.hvac_mode == "cool"
    assert z.hvac_setpoint_c == 21.0
    assert z.ventilation_l_s_per_person > 0


def test_standby_zeroes_ventilation():
    sim = HvacVirtualSimulator(_device(), seed=1)
    z = _zone()
    sim.sample(TS_NIGHT, z, outdoor_c=18.0)
    assert z.hvac_mode == "standby"
    assert z.ventilation_l_s_per_person == 0.0


# --- emitted frame shape -----------------------------------------------------
def test_reading_shape_has_all_keys():
    sim = HvacVirtualSimulator(_device(), seed=1)
    r = sim.sample(TS_DAY_HOT, _zone(), outdoor_c=30.0)
    assert set(r.data.keys()) >= {
        "mode", "setpoint_c", "supply_temp_c",
        "fan_speed_pct", "valve_open_pct", "ventilation_l_s_per_person",
    }
    assert r.sensor_type == "hvac"
    assert r.data["mode"] in VALID_MODES


def test_supply_temp_glides_toward_cool_extreme():
    sim = HvacVirtualSimulator(
        _device(setpoint_c=22.0, min_supply_temp_c=12.0), seed=1,
    )
    z = _zone()
    supplies = []
    for i in range(10):
        ts = TS_DAY_HOT + timedelta(minutes=i)
        supplies.append(sim.sample(ts, z, outdoor_c=33.0).data["supply_temp_c"])
    # Each subsequent value should head down toward 12°C.
    assert supplies[-1] < supplies[0]
    assert supplies[-1] < 18.0


def test_sample_handles_missing_zone():
    sim = HvacVirtualSimulator(_device(), seed=1)
    r = sim.sample(TS_DAY_HOT, None, outdoor_c=33.0)
    assert r.data["mode"] == "cool"


# --- metric mapper ------------------------------------------------------------
def test_metric_mapper_translates_new_hvac_keys():
    mapper = SensgreenMetricMapper(strict_mode=True)
    out = mapper.map("hvac", {
        "mode": "cool",
        "setpoint_c": 22.0,
        "supply_temp_c": 13.5,
        "fan_speed_pct": 80,
        "valve_open_pct": 75,
        "ventilation_l_s_per_person": 8.0,
    })
    assert out["ac_mode"] == "cool"
    assert out["setpoint"] == 22.0
    assert out["supply_air_temperature"] == 13.5
    assert out["fan_speed"] == 80
    assert out["valve_position"] == 75
    assert out["supply_air_flow"] == 8.0
