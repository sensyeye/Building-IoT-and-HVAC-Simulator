"""Tests for the micro-event engine.

These guard the *contract* that the engine offers to the simulation
service and the live session, not the exact RNG sequence — we only
seed for reproducibility and check shape/magnitude bounds.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from simulator.scenarios.micro_events import (
    EVENT_TEMPLATES,
    EventTemplate,
    MicroEventEngine,
    MicroEventInstance,
    get_event_template,
    list_event_templates,
)


UTC = timezone.utc


def _at(minute: int = 0) -> datetime:
    return datetime(2026, 1, 1, 9, 0, tzinfo=UTC) + timedelta(minutes=minute)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def test_catalog_ids_unique_and_lookup_works():
    ids = [t.id for t in EVENT_TEMPLATES]
    assert len(ids) == len(set(ids)), "duplicate template ids"
    assert len(EVENT_TEMPLATES) >= 15, "we want a rich catalog (≥15 events)"
    for tpl in EVENT_TEMPLATES:
        assert get_event_template(tpl.id) is tpl
    assert list_event_templates() is EVENT_TEMPLATES


def test_every_template_has_sane_envelope_and_channels():
    for tpl in EVENT_TEMPLATES:
        assert tpl.rise_min >= 0
        assert tpl.plateau_min >= 0
        assert tpl.decay_min >= 0
        assert tpl.duration_min > 0
        assert tpl.channels, f"{tpl.id} has no channels"
        # Every channel touches a known IAQ data key.
        allowed = {"co2", "temperature", "humidity", "voc", "pm25", "pm10", "pressure"}
        for ch in tpl.channels:
            assert ch in allowed, f"{tpl.id} touches unknown channel {ch}"
        assert 0.0 < tpl.probability_per_min <= 1.0
        assert tpl.cooldown_min >= 0


# ---------------------------------------------------------------------------
# Engine behaviour
# ---------------------------------------------------------------------------


def _force_template(probability: float = 100.0, **overrides) -> EventTemplate:
    """Make a template guaranteed to fire on the very next roll.

    ``probability=100`` gives ``1 - exp(-100) ≈ 1.0`` so any rng draw
    triggers, regardless of seed.
    """
    base = dict(
        id="test_force",
        name="Test event",
        category="fault",
        description="Forced for tests.",
        channels={"co2": 100.0},
        rise_min=0.0,
        plateau_min=5.0,
        decay_min=0.0,
        probability_per_min=probability,
        cooldown_min=0.0,
    )
    base.update(overrides)
    return EventTemplate(**base)


def test_engine_envelope_rises_holds_decays():
    tpl = EventTemplate(
        id="env",
        name="Env",
        category="fault",
        description="",
        channels={"co2": 100.0},
        rise_min=2.0,
        plateau_min=2.0,
        decay_min=2.0,
        probability_per_min=0.001,
    )
    inst = MicroEventInstance(
        template_id=tpl.id,
        zone_id="z",
        started_at=_at(0),
        peaks={"co2": 100.0},
        duration_min=tpl.duration_min,
        rise_min=tpl.rise_min,
        plateau_min=tpl.plateau_min,
        decay_min=tpl.decay_min,
    )
    assert inst.envelope(_at(0)) == 0.0
    assert 0.0 < inst.envelope(_at(1)) < 1.0  # rising
    assert inst.envelope(_at(2)) == pytest.approx(1.0)
    assert inst.envelope(_at(3)) == pytest.approx(1.0)  # plateau
    assert inst.envelope(_at(4)) == pytest.approx(1.0)
    assert 0.0 < inst.envelope(_at(5)) < 1.0  # decaying
    assert inst.envelope(_at(6)) == 0.0


def test_engine_triggers_when_probability_is_one(monkeypatch):
    eng = MicroEventEngine(seed=42)
    # Replace one entry of the catalog so the loop sees a guaranteed-fire
    # template; we monkeypatch the `.templates()` method instead.
    forced = _force_template()
    eng.templates = lambda: (forced,)
    deltas, active = eng.step(_at(0), "zone-a", dt_min=1.0, occupancy=5, schedule_kind="open_office")
    assert active, "expected a guaranteed event to spawn"
    assert active[0].template_id == "test_force"
    # On the very first tick the envelope just started (elapsed ≈ 0) so
    # delta may be 0; on the second tick we should see the plateau.
    deltas2, _ = eng.step(_at(1), "zone-a", dt_min=1.0, occupancy=5, schedule_kind="open_office")
    assert deltas2.get("co2", 0.0) > 0.0


def test_engine_respects_occupied_only(monkeypatch):
    eng = MicroEventEngine(seed=0)
    forced = _force_template(occupied_only=True)
    eng.templates = lambda: (forced,)
    deltas, active = eng.step(_at(0), "z", dt_min=1.0, occupancy=0, schedule_kind="open_office")
    assert not active


def test_engine_respects_zone_kinds(monkeypatch):
    eng = MicroEventEngine(seed=0)
    forced = _force_template(zone_kinds=("meeting_room",))
    eng.templates = lambda: (forced,)
    deltas, active = eng.step(_at(0), "z", dt_min=1.0, occupancy=2, schedule_kind="open_office")
    assert not active
    deltas, active = eng.step(_at(1), "z", dt_min=1.0, occupancy=2, schedule_kind="meeting_room")
    assert active


def test_engine_cooldown_prevents_immediate_re_fire(monkeypatch):
    eng = MicroEventEngine(seed=0)
    # Tiny event (duration ~ 0.1 min) but a 60-min cooldown.
    forced = _force_template(cooldown_min=60.0, plateau_min=0.1, decay_min=0.0)
    eng.templates = lambda: (forced,)
    eng.step(_at(0), "z", dt_min=1.0, occupancy=2, schedule_kind="open_office")
    # Event finished by now, but cooldown holds.
    _, active = eng.step(_at(5), "z", dt_min=1.0, occupancy=2, schedule_kind="open_office")
    assert not active
    _, active = eng.step(_at(120), "z", dt_min=1.0, occupancy=2, schedule_kind="open_office")
    assert active


def test_engine_started_and_ended_queues_drain(monkeypatch):
    eng = MicroEventEngine(seed=0)
    # Long cooldown so the event only fires once across the two ticks.
    forced = _force_template(plateau_min=0.1, decay_min=0.0, cooldown_min=1000.0)
    eng.templates = lambda: (forced,)
    eng.step(_at(0), "z", dt_min=1.0, occupancy=1, schedule_kind="open_office")
    started = eng.pop_started_events()
    assert len(started) == 1
    assert eng.pop_started_events() == []
    # Advance past duration so the event ends.
    eng.step(_at(10), "z", dt_min=1.0, occupancy=1, schedule_kind="open_office")
    ended = eng.pop_ended_events()
    assert len(ended) == 1
    assert ended[0].template_id == "test_force"


def test_engine_deterministic_when_seeded():
    eng_a = MicroEventEngine(seed=7, probability_scale=20.0)
    eng_b = MicroEventEngine(seed=7, probability_scale=20.0)
    for m in range(60):
        eng_a.step(_at(m), "z", dt_min=1.0, occupancy=5, schedule_kind="open_office")
        eng_b.step(_at(m), "z", dt_min=1.0, occupancy=5, schedule_kind="open_office")
    a_ids = [e.template_id for e in eng_a.active_snapshot()]
    b_ids = [e.template_id for e in eng_b.active_snapshot()]
    assert a_ids == b_ids
