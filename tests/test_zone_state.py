"""Tests for P10.1 — ZoneState as authoritative physical state."""

from __future__ import annotations

import random

import pytest

from simulator.sensors.zone_state import (
    CO2_MAX,
    CO2_MIN,
    OUTDOOR_CO2_PPM,
    ZoneState,
    make_room_rng,
)


def _state(**kw) -> ZoneState:
    defaults = dict(
        zone_id="z1",
        capacity=10,
        volume_m3=60.0,
        base_temperature_c=22.0,
        base_humidity_pct=50.0,
        rng=random.Random(123),
    )
    defaults.update(kw)
    return ZoneState(**defaults)


def test_defaults_bootstrap_to_base_setpoint():
    s = _state()
    assert s.temperature_c == pytest.approx(22.0)
    assert s.humidity_pct == pytest.approx(50.0)
    assert s.co2_ppm > OUTDOOR_CO2_PPM
    assert s.door_open is False


def test_step_zero_dt_is_noop():
    s = _state()
    before = (s.temperature_c, s.humidity_pct, s.co2_ppm)
    s.step(0.0, outdoor_c=15.0)
    after = (s.temperature_c, s.humidity_pct, s.co2_ppm)
    assert before == after


def test_occupancy_raises_co2_over_time():
    s = _state()
    s.occupancy = 8
    start = s.co2_ppm
    for _ in range(60):
        s.step(1.0, outdoor_c=15.0)
    assert s.co2_ppm > start + 200  # clear, sustained build-up


def test_empty_room_decays_toward_outdoor():
    s = _state(co2_ppm=1500.0)
    s.occupancy = 0
    for _ in range(120):
        s.step(1.0, outdoor_c=15.0)
    assert s.co2_ppm < 700  # decayed substantially toward 420


def test_door_open_accelerates_co2_decay():
    closed = _state(co2_ppm=1500.0)
    opened = _state(co2_ppm=1500.0)
    opened.open_door()

    for _ in range(15):
        closed.step(1.0, outdoor_c=15.0)
        opened.step(1.0, outdoor_c=15.0)

    # Open door must flush CO₂ faster than the closed-door baseline.
    assert opened.co2_ppm < closed.co2_ppm - 80


def test_door_open_pulls_temperature_toward_outdoor():
    warm_closed = _state(temperature_c=22.0)
    warm_opened = _state(temperature_c=22.0)
    warm_opened.open_door()

    for _ in range(20):
        warm_closed.step(1.0, outdoor_c=5.0)
        warm_opened.step(1.0, outdoor_c=5.0)

    assert warm_opened.temperature_c < warm_closed.temperature_c


def test_door_close_keeps_short_afterboost_then_fades():
    s = _state(co2_ppm=1200.0)
    s.open_door()
    s.step(1.0, outdoor_c=15.0)
    s.close_door()
    assert s.door_boost_remaining_min > 0
    # Step long enough for the boost to fully decay.
    for _ in range(10):
        s.step(1.0, outdoor_c=15.0)
    assert s.door_boost_remaining_min == 0.0


def test_temperature_drifts_toward_setpoint_when_displaced():
    s = _state(temperature_c=16.0)
    for _ in range(120):
        s.step(1.0, outdoor_c=16.0)
    # Should head toward base ~22°C; allow for outdoor pull and noise.
    assert 19.5 < s.temperature_c < 23.0


def test_humidity_tracks_base():
    s = _state(humidity_pct=30.0)
    for _ in range(60):
        s.step(1.0, outdoor_c=20.0)
    assert s.humidity_pct > 40.0


def test_values_remain_within_physical_bounds():
    s = _state(co2_ppm=1900.0, temperature_c=44.0)
    s.occupancy = 50
    for _ in range(500):
        s.step(1.0, outdoor_c=40.0)
    assert CO2_MIN <= s.co2_ppm <= CO2_MAX
    assert 5.0 <= s.temperature_c <= 45.0
    assert 10.0 <= s.humidity_pct <= 95.0
    assert s.pm10_ug_m3 >= s.pm25_ug_m3


def test_step_is_deterministic_for_same_seed():
    a = _state(rng=random.Random(999))
    b = _state(rng=random.Random(999))
    a.occupancy = b.occupancy = 5
    for _ in range(30):
        a.step(1.0, outdoor_c=18.0)
        b.step(1.0, outdoor_c=18.0)
    assert a.co2_ppm == b.co2_ppm
    assert a.temperature_c == b.temperature_c


def test_make_room_rng_is_stable_and_zone_unique():
    a1 = make_room_rng("bldg-1", "zone-A").random()
    a2 = make_room_rng("bldg-1", "zone-A").random()
    b1 = make_room_rng("bldg-1", "zone-B").random()
    assert a1 == a2          # stable across calls
    assert a1 != b1          # different per zone
