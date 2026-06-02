"""Tests for the archetype-driven layout generator + the HTTP endpoint."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.routes.bridge as bridge_module
import api.routes.config as config_module
import api.routes.live as live_module
import api.routes.projects as projects_module
import api.routes.web as web_module
import api.services.event_service as event_module
from api.main import app
from api.services.event_service import EventService
from api.services.project_service import ProjectService
from simulator.layout import (
    LayoutGenerationError,
    LayoutSpec,
    generate_layout,
)


# ---------------------------------------------------------------------------
# Pure-Python generator tests
# ---------------------------------------------------------------------------


def test_generate_layout_office_smoke():
    out = generate_layout(LayoutSpec("office", total_area_m2=1500, floors=3, seed=42))
    assert out["archetype_id"] == "office"
    summary = out["summary"]
    assert summary["zone_count"] >= 30
    assert summary["zone_count"] == len(out["zones"])
    # All three floors get zones.
    assert set(summary["by_floor"].keys()) == {"F1", "F2", "F3"}
    for floor_id, n in summary["by_floor"].items():
        assert n >= 5, f"{floor_id} got {n}"
    # Modeled area should be in the same order of magnitude as requested.
    assert 600 < summary["modeled_area_m2"] < 2200


def test_generate_layout_deterministic_for_same_seed():
    spec = LayoutSpec("hotel", total_area_m2=2000, floors=4, seed=7)
    a = generate_layout(spec)
    b = generate_layout(spec)
    assert a == b


def test_generate_layout_differs_for_different_seed():
    a = generate_layout(LayoutSpec("hotel", 2000, 4, seed=1))
    b = generate_layout(LayoutSpec("hotel", 2000, 4, seed=2))
    # Same totals expected, but the specific floor distribution differs.
    assert a["summary"]["zone_count"] == b["summary"]["zone_count"]
    assert a != b


def test_generated_zones_carry_required_fields():
    out = generate_layout(LayoutSpec("school", 1200, 2, seed=11))
    seen_ids = set()
    for z in out["zones"]:
        assert {"id", "name", "room_type", "floor_id"} <= z.keys()
        assert z["id"] not in seen_ids
        seen_ids.add(z["id"])
        assert z["area_m2"] > 0
        assert z["capacity"] >= 0
        assert z["floor_id"].startswith("F")


def test_generate_layout_small_building_fallback():
    # A 50 m² residential should still produce at least one zone.
    out = generate_layout(LayoutSpec("residential", 50, 1, seed=0))
    assert out["summary"]["zone_count"] >= 1


def test_generate_layout_validates_inputs():
    with pytest.raises(LayoutGenerationError):
        generate_layout(LayoutSpec("", 100, 1))
    with pytest.raises(LayoutGenerationError):
        generate_layout(LayoutSpec("office", 0, 1))
    with pytest.raises(LayoutGenerationError):
        generate_layout(LayoutSpec("office", 100, 0))
    with pytest.raises(LayoutGenerationError):
        generate_layout(LayoutSpec("does_not_exist", 100, 1))


def test_room_mix_respected_for_office():
    out = generate_layout(LayoutSpec("office", 5000, 1, seed=3))
    counts = out["summary"]["by_room_type"]
    # Office archetype must have at least these.
    assert "open_office" in counts
    assert "meeting_room" in counts
    # Open office should not be a tiny minority for an office building.
    total = sum(counts.values())
    assert counts["open_office"] / total >= 0.05


# ---------------------------------------------------------------------------
# HTTP endpoint tests
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    proj_svc = ProjectService(data_dir=tmp_path)
    evt_svc = EventService(data_dir=tmp_path)
    for mod in (
        projects_module,
        config_module,
        live_module,
        bridge_module,
        web_module,
    ):
        monkeypatch.setattr(mod, "project_service", proj_svc, raising=False)
        monkeypatch.setattr(mod, "event_service", evt_svc, raising=False)
    monkeypatch.setattr(event_module, "event_service", evt_svc)
    with TestClient(app) as c:
        yield c, proj_svc


def _make_project(c: TestClient, **extra) -> str:
    payload = {"name": "Layout P", "building_type": "office"}
    payload.update(extra)
    r = c.post("/api/projects", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_generate_layout_preview_does_not_persist(client):
    c, proj_svc = client
    pid = _make_project(c)
    yaml_before = proj_svc.get_config_yaml(pid)

    r = c.post(
        f"/api/projects/{pid}/generate-layout",
        json={
            "archetype_id": "office",
            "total_area_m2": 1500,
            "floors": 2,
            "seed": 1,
            "apply": False,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] is False
    assert "layout" in body
    assert body["layout"]["summary"]["zone_count"] > 0
    assert "config" not in body

    # YAML on disk must be untouched.
    assert proj_svc.get_config_yaml(pid) == yaml_before


def test_generate_layout_apply_replaces_zones(client):
    c, proj_svc = client
    pid = _make_project(c)

    r = c.post(
        f"/api/projects/{pid}/generate-layout",
        json={
            "archetype_id": "office",
            "total_area_m2": 1200,
            "floors": 2,
            "seed": 42,
            "apply": True,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] is True
    cfg = body["config"]
    new_zones = cfg["building"]["zones"]
    assert len(new_zones) > 5
    # The default zone created by project creation must be gone.
    ids = {z["id"] for z in new_zones}
    assert "zone-default" not in ids
    # Every generated zone has room_type + floor_id persisted on disk.
    for z in new_zones:
        assert z.get("room_type")
        assert z.get("floor_id")


def test_generate_layout_apply_refuses_when_devices_would_orphan(client):
    c, _ = client
    pid = _make_project(c)
    # Add a device attached to the default zone.
    r = c.post(
        f"/api/projects/{pid}/devices",
        json={
            "device_eui": "0011223344556677",
            "name": "Sensor A",
            "type": "iaq",
            "zone_id": "zone-default",
        },
    )
    assert r.status_code == 201, r.text

    r = c.post(
        f"/api/projects/{pid}/generate-layout",
        json={
            "archetype_id": "office",
            "total_area_m2": 1200,
            "floors": 1,
            "seed": 1,
            "apply": True,
        },
    )
    assert r.status_code == 400
    assert "orphan" in r.json()["detail"].lower()


def test_generate_layout_validation_errors(client):
    c, _ = client
    pid = _make_project(c)
    # Unknown archetype.
    r = c.post(
        f"/api/projects/{pid}/generate-layout",
        json={"archetype_id": "nope", "total_area_m2": 100, "floors": 1, "apply": False},
    )
    assert r.status_code == 400
    # Bad area (pydantic 422).
    r = c.post(
        f"/api/projects/{pid}/generate-layout",
        json={"archetype_id": "office", "total_area_m2": -5, "floors": 1},
    )
    assert r.status_code == 422


def test_generate_layout_unknown_project_returns_404(client):
    c, _ = client
    r = c.post(
        "/api/projects/does-not-exist/generate-layout",
        json={"archetype_id": "office", "total_area_m2": 100, "floors": 1},
    )
    assert r.status_code == 404


def test_generated_zones_pass_config_validation(client):
    """End-to-end: applied layout must round-trip through load_config()."""
    c, _ = client
    pid = _make_project(c)
    r = c.post(
        f"/api/projects/{pid}/generate-layout",
        json={
            "archetype_id": "school",
            "total_area_m2": 800,
            "floors": 2,
            "seed": 9,
            "apply": True,
        },
    )
    assert r.status_code == 200, r.text
    meta = r.json()["meta"]
    # A project with no devices is allowed by the loader → meta.valid may be
    # False because of "no devices", but at minimum we shouldn't see a zone
    # parsing error.
    err_blob = " ".join(meta.get("errors", []))
    assert "zone" not in err_blob.lower() or "device" in err_blob.lower(), (
        f"Unexpected zone error from generated layout: {err_blob}"
    )
