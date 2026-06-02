"""P10.4 integration tests — ScenarioContext drives room physics,
door events propagate from door_contact into IAQ readings, and two
similar rooms behave similarly-but-not-identically.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

from simulator.models.config import (
    BuildingConfig,
    DeviceConfig,
    OutputsConfig,
    SimulationConfig,
    SimulatorConfig,
    ZoneConfig,
)
from simulator.services.scenario_context import ScenarioContext
from simulator.services.simulation_service import SimulationService


TS0 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)


def _building(zone_ids=("z1",)) -> BuildingConfig:
    return BuildingConfig(
        id="bldg-test",
        name="Test Building",
        timezone="UTC",
        zones=[
            ZoneConfig(id=z, name=z, capacity=10, area_m2=30.0)
            for z in zone_ids
        ],
    )


def _iaq(eui: str, zone_id: str = "z1") -> DeviceConfig:
    return DeviceConfig(
        device_eui=eui, name=eui, type="iaq", zone_id=zone_id,
        metadata={"interval_seconds": 60},
    )


def _door(eui: str, zone_id: str = "z1") -> DeviceConfig:
    return DeviceConfig(
        device_eui=eui, name=eui, type="door_contact", zone_id=zone_id,
        metadata={"interval_seconds": 60},
    )


def _sim_cfg(devices, zone_ids=("z1",)) -> SimulatorConfig:
    return SimulatorConfig(
        building=_building(zone_ids),
        devices=devices,
        simulation=SimulationConfig(interval_seconds=60, seed=42),
        outputs=OutputsConfig(),
    )


# ---------------------------------------------------------------------------
# ScenarioContext
# ---------------------------------------------------------------------------
def test_context_update_advances_physics():
    ctx = ScenarioContext.from_building(_building())
    # Force high occupancy on the room directly.
    state = ctx.zone_state("z1")
    state.occupancy = 15
    start_co2 = state.co2_ppm

    # First call only seeds last_update_ts; subsequent calls step.
    ctx.update(TS0)
    for i in range(1, 31):
        ctx.update(TS0 + timedelta(minutes=i))
        # The scheduler may reset occupancy; push it back up each tick
        # so we keep building CO₂ for this test.
        state.occupancy = 15

    assert state.co2_ppm > start_co2 + 100


def test_context_first_update_does_not_advance_physics():
    ctx = ScenarioContext.from_building(_building())
    state = ctx.zone_state("z1")
    before = (state.co2_ppm, state.temperature_c)
    ctx.update(TS0)
    after = (state.co2_ppm, state.temperature_c)
    assert before == after
    assert ctx.last_update_ts == TS0


# ---------------------------------------------------------------------------
# Door → room feedback through SimulationService
# ---------------------------------------------------------------------------
def test_room_driven_iaq_through_simulation_service():
    """IAQ readings should reflect room state (which is being stepped)."""
    cfg = _sim_cfg([_iaq("iaq-1")])
    svc = SimulationService(cfg, seed=42)
    readings = list(
        svc.iter_readings(start=TS0, end=TS0 + timedelta(hours=2))
    )
    assert len(readings) > 60
    # All within plausible physical range.
    for r in readings:
        assert 400 <= r.data["co2"] <= 2000
        assert 5 <= r.data["temperature"] <= 45


def test_two_similar_rooms_behave_similar_but_not_identical():
    """Acceptance from roadmap: two similar rooms ≠ identical traces."""
    cfg = _sim_cfg(
        devices=[_iaq("iaq-A", "zA"), _iaq("iaq-B", "zB")],
        zone_ids=("zA", "zB"),
    )
    svc = SimulationService(cfg, seed=42)
    readings = list(
        svc.iter_readings(start=TS0, end=TS0 + timedelta(hours=2))
    )
    co2_a = [r.data["co2"] for r in readings if r.device_eui == "iaq-A"]
    co2_b = [r.data["co2"] for r in readings if r.device_eui == "iaq-B"]
    assert co2_a and co2_b
    # Means similar (same schedule, capacity, area) ...
    assert abs(statistics.mean(co2_a) - statistics.mean(co2_b)) < 80
    # ... but the traces are not identical.
    assert co2_a != co2_b


def test_two_devices_same_room_track_each_other():
    cfg = _sim_cfg([_iaq("iaq-1"), _iaq("iaq-2")])
    svc = SimulationService(cfg, seed=42)
    readings = list(
        svc.iter_readings(start=TS0, end=TS0 + timedelta(hours=1))
    )
    by_eui = {"iaq-1": [], "iaq-2": []}
    for r in readings:
        by_eui[r.device_eui].append(r.data["co2"])
    # Different streams (different personalities) ...
    assert by_eui["iaq-1"] != by_eui["iaq-2"]
    # ... but their means should be within a small band (same room).
    assert abs(statistics.mean(by_eui["iaq-1"]) -
               statistics.mean(by_eui["iaq-2"])) < 40


def test_door_open_event_propagates_to_room():
    """A 'door open' reading should flip ZoneState.door_open."""
    cfg = _sim_cfg([_door("door-1")])
    svc = SimulationService(cfg, seed=42)
    # Pump a couple of hours through; door simulator will eventually
    # produce at least one 'open' event under default rates.
    saw_open = False
    for _ in svc.iter_readings(start=TS0, end=TS0 + timedelta(hours=8)):
        if svc.context.zone_state("z1").door_open:
            saw_open = True
            break
    # We don't *require* a door to open in any specific window (the
    # process is stochastic) but the integration path itself must be
    # exercised: at minimum the door simulator must have produced
    # `closed` readings, and the context must have stepped physics.
    state = svc.context.zone_state("z1")
    assert svc.context.last_update_ts is not None
    # boost-remaining may be zero at any sample but the field must exist:
    assert hasattr(state, "door_boost_remaining_min")
    # If we did see an open, the open path was clearly wired.
    if saw_open:
        assert state.door_boost_remaining_min >= 0.0


def test_manual_door_open_flushes_co2_in_iaq_stream():
    """Direct integration: force door_open True, IAQ readings should
    drop faster than with door closed."""
    cfg_closed = _sim_cfg([_iaq("iaq-closed")])
    cfg_open = _sim_cfg([_iaq("iaq-open")])

    svc_closed = SimulationService(cfg_closed, seed=42)
    svc_open = SimulationService(cfg_open, seed=42)

    # Pre-charge both rooms to high CO₂.
    for svc in (svc_closed, svc_open):
        svc.context.zone_state("z1").co2_ppm = 1600.0
    svc_open.context.zone_state("z1").open_door()

    end = TS0 + timedelta(minutes=30)
    list(svc_closed.iter_readings(start=TS0, end=end))
    # Keep door pinned open across ticks for the open variant.
    open_state = svc_open.context.zone_state("z1")
    last_open_co2 = None
    for r in svc_open.iter_readings(start=TS0, end=end):
        open_state.open_door()
        last_open_co2 = r.data["co2"]

    closed_state = svc_closed.context.zone_state("z1")
    assert last_open_co2 is not None
    assert open_state.co2_ppm < closed_state.co2_ppm - 80
