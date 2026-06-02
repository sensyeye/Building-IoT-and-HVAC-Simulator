"""Tests for P11.1 — HVAC effect on room physics."""

from __future__ import annotations

import random

import pytest

from simulator.sensors.zone_state import ZoneState


def _state(**kw) -> ZoneState:
    defaults = dict(
        zone_id="z1", capacity=10, volume_m3=60.0,
        base_temperature_c=22.0, base_humidity_pct=50.0,
        rng=random.Random(7),
    )
    defaults.update(kw)
    return ZoneState(**defaults)


def test_hvac_cool_pulls_temperature_down_to_setpoint():
    s = _state(temperature_c=26.0)
    s.hvac_mode = "cool"
    s.hvac_setpoint_c = 22.0
    for _ in range(60):
        s.step(1.0, outdoor_c=32.0)
    assert 21.0 < s.temperature_c < 23.0


def test_hvac_heat_pulls_temperature_up_to_setpoint():
    s = _state(temperature_c=15.0)
    s.hvac_mode = "heat"
    s.hvac_setpoint_c = 23.0
    for _ in range(60):
        s.step(1.0, outdoor_c=2.0)
    assert 22.0 < s.temperature_c < 24.0


def test_hvac_off_lets_temperature_drift_with_outdoor():
    s = _state(temperature_c=22.0)
    s.hvac_mode = "off"
    s.hvac_setpoint_c = 22.0  # ignored when off
    # Hot outdoor with HVAC off → room warms over time (slowly).
    for _ in range(180):
        s.step(1.0, outdoor_c=35.0)
    assert s.temperature_c > 22.5


def test_hvac_ventilation_flushes_co2():
    base = _state(co2_ppm=1400.0)
    vent = _state(co2_ppm=1400.0)
    base.hvac_mode = "off"
    vent.hvac_mode = "cool"
    vent.ventilation_l_s_per_person = 14.0  # 2× design rate
    base.occupancy = vent.occupancy = 5
    for _ in range(15):
        base.step(1.0, outdoor_c=20.0)
        vent.step(1.0, outdoor_c=20.0)
    assert vent.co2_ppm < base.co2_ppm - 60


def test_hvac_off_with_zero_ventilation_lets_co2_build():
    s = _state(co2_ppm=600.0)
    s.hvac_mode = "off"
    s.occupancy = 12
    for _ in range(60):
        s.step(1.0, outdoor_c=20.0)
    # No HVAC + closed door + 12 occupants → CO₂ should build noticeably.
    assert s.co2_ppm > 900


def test_hvac_auto_mode_uses_full_vent_when_occupied_partial_when_empty():
    occupied = _state(co2_ppm=1400.0)
    empty = _state(co2_ppm=1400.0)
    occupied.hvac_mode = empty.hvac_mode = "auto"
    occupied.occupancy = 8
    empty.occupancy = 0
    for _ in range(15):
        occupied.step(1.0, outdoor_c=20.0)
        empty.step(1.0, outdoor_c=20.0)
    # Both decay, but the occupied room (full ventilation) decays faster
    # *relative to its buildup* — and the empty room has no buildup at all.
    # Sanity: both well below initial.
    assert occupied.co2_ppm < 1400.0
    assert empty.co2_ppm < 1400.0
    # Empty room with partial auto vent decays less aggressively than a
    # fully-ventilated occupied room would on the *same* starting state +
    # zero occupancy. We approximate by running the same starting state
    # under "cool" with full vent and showing it's even lower.
    full = _state(co2_ppm=1400.0)
    full.hvac_mode = "cool"
    full.occupancy = 0
    for _ in range(15):
        full.step(1.0, outdoor_c=20.0)
    assert full.co2_ppm < empty.co2_ppm


def test_hvac_setpoint_none_falls_back_to_base_temperature():
    s = _state(temperature_c=25.0)
    s.hvac_mode = "cool"
    s.hvac_setpoint_c = None  # default → base 22°C
    for _ in range(60):
        s.step(1.0, outdoor_c=32.0)
    assert 21.0 < s.temperature_c < 23.5


def test_unknown_hvac_mode_treated_as_active_ventilation():
    """Custom modes like 'fan_only' get full ventilation, no setpoint pull."""
    s = _state(temperature_c=24.0, co2_ppm=1300.0)
    s.hvac_mode = "fan_only"
    s.occupancy = 3
    for _ in range(15):
        s.step(1.0, outdoor_c=24.0)
    # CO₂ should decay (vent active) but no strong temperature pull.
    assert s.co2_ppm < 1300.0
    assert 23.0 < s.temperature_c < 25.0
