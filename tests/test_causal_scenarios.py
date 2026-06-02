"""Tests for P11.3 — causal scenarios."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from simulator.models.config import BuildingConfig, ZoneConfig
from simulator.scenarios.causal import (
    CausalEffect,
    CausalRule,
    CausalScenario,
    TimeWindow,
    WEEKDAYS,
    apply_scenarios_to_zone,
    cleaning_routine,
    get_builtin_scenario,
    list_builtin_scenarios,
    lunch_rush,
    morning_rush,
    night_setback,
)
from simulator.sensors.zone_state import ZoneState
from simulator.services.scenario_context import ScenarioContext


UTC = ZoneInfo("UTC")


def _zone(**kw) -> ZoneState:
    base = dict(zone_id="z1", capacity=20, volume_m3=60.0,
                base_temperature_c=22.0, base_humidity_pct=50.0)
    base.update(kw)
    return ZoneState(**base)


# ---------------------------------------------------------------------------
# TimeWindow
# ---------------------------------------------------------------------------
def test_time_window_basic_contains():
    w = TimeWindow(8.0, 9.5, WEEKDAYS)
    monday = datetime(2026, 6, 1, 8, 30, tzinfo=timezone.utc)  # Mon 08:30
    saturday = datetime(2026, 6, 6, 8, 30, tzinfo=timezone.utc)
    assert w.contains(monday, UTC) is True
    assert w.contains(saturday, UTC) is False


def test_time_window_wraps_midnight():
    w = TimeWindow(22.0, 6.0)
    assert w.contains(datetime(2026, 6, 1, 23, 0, tzinfo=timezone.utc), UTC)
    assert w.contains(datetime(2026, 6, 2, 3, 0, tzinfo=timezone.utc), UTC)
    assert not w.contains(datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc), UTC)


# ---------------------------------------------------------------------------
# CausalEffect.apply
# ---------------------------------------------------------------------------
def test_effect_set_hvac_mode_zeroes_vent_when_off():
    z = _zone()
    z.ventilation_l_s_per_person = 8.0
    CausalEffect("set_hvac_mode", "off").apply(z)
    assert z.hvac_mode == "off"
    assert z.ventilation_l_s_per_person == 0.0


def test_effect_set_hvac_setpoint():
    z = _zone()
    CausalEffect("set_hvac_setpoint", 19.5).apply(z)
    assert z.hvac_setpoint_c == 19.5


def test_effect_scale_occupancy_rounds():
    z = _zone()
    z.occupancy = 10
    CausalEffect("scale_occupancy", 1.4).apply(z)
    assert z.occupancy == 14


def test_effect_set_occupancy_floor_and_cap():
    z = _zone()
    z.occupancy = 5
    CausalEffect("set_occupancy_floor", 8).apply(z)
    assert z.occupancy == 8
    CausalEffect("set_occupancy_cap", 3).apply(z)
    assert z.occupancy == 3


def test_effect_force_door_open_and_close():
    z = _zone()
    CausalEffect("force_door_open", True).apply(z)
    assert z.door_open is True
    CausalEffect("force_door_open", False).apply(z)
    assert z.door_open is False


def test_effect_bump_pm25_drags_pm10_along():
    z = _zone()
    z.pm25_ug_m3 = 8.0
    z.pm10_ug_m3 = 10.0
    CausalEffect("bump_pm25", 5.0).apply(z)
    assert z.pm25_ug_m3 == 13.0
    assert z.pm10_ug_m3 >= 13.0


def test_unknown_effect_is_noop():
    z = _zone()
    before = (z.occupancy, z.hvac_mode)
    CausalEffect("not_a_real_kind", 99).apply(z)
    after = (z.occupancy, z.hvac_mode)
    assert before == after


# ---------------------------------------------------------------------------
# Built-ins
# ---------------------------------------------------------------------------
def test_builtin_list_has_four_scenarios():
    scenarios = list_builtin_scenarios()
    ids = {s.id for s in scenarios}
    assert ids == {"morning_rush", "lunch_rush", "cleaning_routine", "night_setback"}


def test_get_builtin_scenario_lookup():
    assert get_builtin_scenario("lunch_rush").id == "lunch_rush"
    assert get_builtin_scenario("does_not_exist") is None


def test_morning_rush_fires_on_open_office_monday_8am():
    z = _zone()
    z.occupancy = 10
    ts = datetime(2026, 6, 1, 8, 15, tzinfo=timezone.utc)
    fired = apply_scenarios_to_zone(
        [morning_rush()], ts=ts, tz=UTC,
        zone_id="z1", room_type="open_office", zone=z,
    )
    assert any("morning_rush" in f for f in fired)
    assert z.occupancy == 14  # 10 × 1.4
    assert z.hvac_mode == "cool"


def test_lunch_rush_kitchen_bumps_voc_and_pm():
    z = _zone()
    z.voc_mg_m3 = 0.2
    z.pm25_ug_m3 = 6.0
    ts = datetime(2026, 6, 1, 12, 30, tzinfo=timezone.utc)
    apply_scenarios_to_zone(
        [lunch_rush()], ts=ts, tz=UTC,
        zone_id="z1", room_type="restaurant_kitchen", zone=z,
    )
    assert z.voc_mg_m3 > 0.7
    assert z.pm25_ug_m3 > 9.0


def test_night_setback_pins_office_to_zero_and_sets_standby():
    z = _zone()
    z.occupancy = 5
    ts = datetime(2026, 6, 2, 2, 0, tzinfo=timezone.utc)  # Tue 02:00
    apply_scenarios_to_zone(
        [night_setback()], ts=ts, tz=UTC,
        zone_id="z1", room_type="open_office", zone=z,
    )
    assert z.occupancy == 0
    assert z.hvac_mode == "standby"


def test_cleaning_routine_forces_door_and_floor_one_person():
    z = _zone()
    z.occupancy = 0
    ts = datetime(2026, 6, 1, 20, 30, tzinfo=timezone.utc)
    apply_scenarios_to_zone(
        [cleaning_routine()], ts=ts, tz=UTC,
        zone_id="z1", room_type="hotel_guest_room", zone=z,
    )
    assert z.occupancy >= 1
    assert z.door_open is True


# ---------------------------------------------------------------------------
# Integration through ScenarioContext
# ---------------------------------------------------------------------------
def _building() -> BuildingConfig:
    return BuildingConfig(
        id="bldg-1", name="B1", timezone="UTC",
        zones=[
            ZoneConfig(id="of-1", name="Office", capacity=20, area_m2=40.0,
                       room_type="open_office"),
            ZoneConfig(id="kt-1", name="Kitchen", capacity=10, area_m2=20.0,
                       room_type="restaurant_kitchen"),
        ],
    )


def test_context_with_scenarios_applies_morning_rush():
    ctx = ScenarioContext.from_building(
        _building(), causal_scenarios=(morning_rush(),),
    )
    ts = datetime(2026, 6, 1, 8, 30, tzinfo=timezone.utc)
    ctx.update(ts)  # first call seeds
    ctx.update(datetime(2026, 6, 1, 8, 31, tzinfo=timezone.utc))
    office = ctx.zone_state("of-1")
    assert office.hvac_mode == "cool"
    assert "of-1" in ctx.last_fired_rules


def test_context_without_scenarios_unchanged():
    ctx = ScenarioContext.from_building(_building())
    ts = datetime(2026, 6, 1, 8, 30, tzinfo=timezone.utc)
    ctx.update(ts)
    assert ctx.last_fired_rules == {}
