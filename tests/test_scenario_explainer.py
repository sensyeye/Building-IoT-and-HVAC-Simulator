"""Tests for the scenario explainer and active-window resolution."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from simulator.scenarios import (
    CATALOG,
    active_scenarios_at,
    explain_reading,
    get_impact,
    known_impacts,
)


# ---------------------------------------------------------------------------
# Catalog ↔ impact-table sync
# ---------------------------------------------------------------------------


def test_every_catalog_scenario_has_impact_entry():
    """Adding a scenario without updating the explainer should fail."""
    catalog_ids = {s.id for s in CATALOG}
    impact_ids = known_impacts()
    missing = catalog_ids - impact_ids
    assert not missing, (
        f"scenarios missing an explainer entry: {sorted(missing)}"
    )


def test_no_orphan_impact_entries():
    catalog_ids = {s.id for s in CATALOG}
    impact_ids = known_impacts()
    extra = impact_ids - catalog_ids
    assert not extra, f"orphan explainer entries: {sorted(extra)}"


def test_impact_metadata_well_formed():
    for sid in known_impacts():
        impact = get_impact(sid)
        assert impact is not None
        assert impact.why, f"{sid} has empty why"
        assert isinstance(impact.sensor_types, tuple)
        assert impact.sensor_types, f"{sid} touches no sensor types"


# ---------------------------------------------------------------------------
# explain_reading
# ---------------------------------------------------------------------------


def test_explain_reading_filters_by_sensor_type():
    out = explain_reading("iaq", ["meeting_room_poor_ventilation"])
    assert len(out) == 1
    assert out[0]["id"] == "meeting_room_poor_ventilation"
    assert "co2" in out[0]["why"].lower() or "ventilat" in out[0]["why"].lower()

    # Energy meter does not get an IAQ-only scenario annotation.
    out = explain_reading("energy_meter", ["meeting_room_poor_ventilation"])
    assert out == []


def test_explain_reading_multiple_scenarios():
    out = explain_reading(
        "iaq",
        [
            "meeting_room_poor_ventilation",
            "after_hours_energy_waste",  # energy-only, should be dropped
            "outdoor_pm_event",
        ],
    )
    ids = [o["id"] for o in out]
    assert ids == ["meeting_room_poor_ventilation", "outdoor_pm_event"]


def test_explain_reading_unknown_scenario_silently_dropped():
    out = explain_reading("iaq", ["this-id-does-not-exist"])
    assert out == []


# ---------------------------------------------------------------------------
# active_scenarios_at
# ---------------------------------------------------------------------------


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def test_disabled_assignment_never_active():
    ids = active_scenarios_at(
        _utc("2025-01-01T12:00:00+00:00"),
        [{"id": "meeting_room_poor_ventilation", "enabled": False}],
    )
    assert ids == []


def test_assignment_with_no_window_is_always_active():
    ids = active_scenarios_at(
        _utc("2025-01-01T12:00:00+00:00"),
        [{"id": "outdoor_pm_event", "enabled": True}],
    )
    assert ids == ["outdoor_pm_event"]


def test_window_inclusive_bounds():
    a = {
        "id": "cleaning_voc_spike",
        "enabled": True,
        "start": "2025-01-01T08:00:00+00:00",
        "end":   "2025-01-01T10:00:00+00:00",
    }
    assert active_scenarios_at(_utc("2025-01-01T07:59:59+00:00"), [a]) == []
    assert active_scenarios_at(_utc("2025-01-01T08:00:00+00:00"), [a]) == ["cleaning_voc_spike"]
    assert active_scenarios_at(_utc("2025-01-01T09:30:00+00:00"), [a]) == ["cleaning_voc_spike"]
    assert active_scenarios_at(_utc("2025-01-01T10:00:00+00:00"), [a]) == ["cleaning_voc_spike"]
    assert active_scenarios_at(_utc("2025-01-01T10:00:01+00:00"), [a]) == []


def test_open_ended_start_or_end():
    open_end = {
        "id": "outdoor_pm_event",
        "enabled": True,
        "start": "2025-01-01T08:00:00+00:00",
    }
    open_start = {
        "id": "after_hours_energy_waste",
        "enabled": True,
        "end": "2025-01-01T08:00:00+00:00",
    }
    ts = _utc("2025-01-01T12:00:00+00:00")
    assert "outdoor_pm_event" in active_scenarios_at(ts, [open_end])
    assert "after_hours_energy_waste" not in active_scenarios_at(ts, [open_start])

    early = _utc("2024-12-31T12:00:00+00:00")
    assert "after_hours_energy_waste" in active_scenarios_at(early, [open_start])


def test_naive_datetime_treated_as_utc():
    a = {"id": "outdoor_pm_event", "enabled": True}
    naive = datetime(2025, 1, 1, 12, 0, 0)  # no tz
    assert active_scenarios_at(naive, [a]) == ["outdoor_pm_event"]


def test_unparseable_iso_falls_back_to_open_window():
    """Garbage in start/end should not crash; treat as missing."""
    a = {
        "id": "outdoor_pm_event",
        "enabled": True,
        "start": "not-a-date",
    }
    assert active_scenarios_at(_utc("2025-01-01T00:00:00+00:00"), [a]) == ["outdoor_pm_event"]


def test_duplicate_ids_deduped():
    a = {"id": "outdoor_pm_event", "enabled": True}
    out = active_scenarios_at(_utc("2025-01-01T00:00:00+00:00"), [a, a])
    assert out == ["outdoor_pm_event"]
