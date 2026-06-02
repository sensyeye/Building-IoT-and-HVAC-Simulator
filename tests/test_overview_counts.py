"""Tests for ``ProjectService.derive_status``.

These lock in the contract that the Overview tab depends on:

* device + zone counts come from the **managed YAML**, never the
  stale ``device_count`` field on the Project record;
* coverage ratio is derived from zone area sums vs. the building's
  declared area;
* last validation / last run are pulled from the event log.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from api.services.event_service import EventService
from api.services.project_service import ProjectService


@pytest.fixture
def services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    proj = ProjectService(data_dir=tmp_path)
    evt = EventService(data_dir=tmp_path)
    import api.services.event_service as evt_mod
    monkeypatch.setattr(evt_mod, "event_service", evt)
    return proj, evt


def _new_project(proj: ProjectService, *, area_m2: float = 1500.0) -> str:
    p = proj.create({
        "name": "Coverage Demo",
        "building_type": "office",
        "city": "Istanbul",
        "timezone": "Europe/Istanbul",
        "area_m2": area_m2,
        "floors": 2,
        "demo_depth": "standard",
    })
    return p.id


def test_derive_status_counts_match_managed_config(services):
    proj, _evt = services
    pid = _new_project(proj)
    status = proj.derive_status(pid)
    assert status["zone_count"] == 1
    assert status["device_count"] == 0
    proj.add_zone(pid, {"id": "zone-2", "name": "Floor 2", "area_m2": 200})
    proj.add_device(pid, {
        "device_eui": "aa" * 8,
        "name": "Lobby IAQ",
        "type": "iaq",
        "zone_id": "zone-default",
    })
    proj.add_device(pid, {
        "device_eui": "bb" * 8,
        "name": "Floor 2 IAQ",
        "type": "iaq",
        "zone_id": "zone-2",
    })
    status = proj.derive_status(pid)
    assert status["zone_count"] == 2
    assert status["device_count"] == 2


def test_derive_status_ignores_stale_device_count_field(services):
    proj, _evt = services
    pid = _new_project(proj)
    p = proj.get(pid)
    assert p is not None
    p.device_count = 999
    proj._save(p)  # type: ignore[attr-defined]
    status = proj.derive_status(pid)
    assert status["device_count"] == 0


def test_coverage_low_when_only_default_zone(services):
    proj, _evt = services
    pid = _new_project(proj, area_m2=1500.0)
    status = proj.derive_status(pid)
    cov = status["coverage"]
    assert cov["building_area_m2"] == 1500.0
    assert 0.06 < cov["ratio"] < 0.08
    assert cov["severity"] == "low"
    assert cov["recommendation"] is not None


def test_coverage_ok_when_zones_cover_building(services):
    proj, _evt = services
    pid = _new_project(proj, area_m2=200.0)
    proj.add_zone(pid, {"id": "zone-2", "name": "Annex", "area_m2": 100})
    status = proj.derive_status(pid)
    assert status["coverage"]["severity"] == "ok"
    assert status["coverage"]["recommendation"] is None


def test_coverage_over_when_zones_exceed_building(services):
    proj, _evt = services
    pid = _new_project(proj, area_m2=100.0)
    proj.add_zone(pid, {"id": "zone-2", "name": "Big", "area_m2": 500})
    status = proj.derive_status(pid)
    assert status["coverage"]["severity"] == "over"


def test_coverage_unknown_when_building_area_zero(services):
    proj, _evt = services
    pid = _new_project(proj, area_m2=0.0)
    status = proj.derive_status(pid)
    assert status["coverage"]["severity"] == "unknown"
    assert status["coverage"]["ratio"] is None


def test_last_run_uses_newest_event_across_kinds(services):
    proj, evt = services
    pid = _new_project(proj)
    evt.record(pid, kind="bridge_test", status="succeeded",
               summary="bridge ok", details={})
    evt.record(pid, kind="live_run", status="running",
               summary="live started", details={})
    status = proj.derive_status(pid)
    assert status["last_run"] is not None
    assert status["last_run"]["kind"] == "live_run"
    assert status["last_run"]["status"] == "running"


def test_last_run_is_none_when_no_events(services):
    proj, _evt = services
    pid = _new_project(proj)
    status = proj.derive_status(pid)
    assert status["last_run"] is None
    assert status["last_validation"] is None


def test_unknown_project_raises(services):
    proj, _evt = services
    with pytest.raises(ValueError):
        proj.derive_status("no-such-project")
