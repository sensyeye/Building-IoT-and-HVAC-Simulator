"""Tests for the PUT /zones/{id} route and update_zone service path."""

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


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    proj_svc = ProjectService(data_dir=tmp_path)
    evt_svc = EventService(data_dir=tmp_path)
    for mod in (
        projects_module, config_module, live_module,
        bridge_module, web_module,
    ):
        monkeypatch.setattr(mod, "project_service", proj_svc, raising=False)
        monkeypatch.setattr(mod, "event_service", evt_svc, raising=False)
    monkeypatch.setattr(event_module, "event_service", evt_svc)
    with TestClient(app) as c:
        yield c, proj_svc


def _make_project(c: TestClient) -> str:
    r = c.post("/api/projects", json={"name": "Z-edit", "building_type": "office"})
    assert r.status_code == 201
    return r.json()["id"]


def test_update_zone_sets_wiring_fields(client):
    c, _ = client
    pid = _make_project(c)
    c.post(f"/api/projects/{pid}/zones", json={"id": "z1", "name": "Z1"})
    c.post(f"/api/projects/{pid}/hvac-zones", json={"id": "AHU-1", "name": "AHU 1"})

    r = c.put(
        f"/api/projects/{pid}/zones/z1",
        json={
            "id": "z1",
            "name": "Z1 renamed",
            "room_type": "meeting_room",
            "monitoring_profile": "iaq_occupancy",
            "hvac_zone_id": "AHU-1",
            "floor_id": "f2",
            "exposure": "north",
            "ventilation_quality": "good",
        },
    )
    assert r.status_code == 200, r.text
    cfg = r.json()["config"]
    z = next(z for z in cfg["building"]["zones"] if z["id"] == "z1")
    assert z["name"] == "Z1 renamed"
    assert z["room_type"] == "meeting_room"
    assert z["monitoring_profile"] == "iaq_occupancy"
    assert z["hvac_zone_id"] == "AHU-1"
    assert z["floor_id"] == "f2"
    assert z["exposure"] == "north"
    assert z["ventilation_quality"] == "good"


def test_update_zone_path_id_overrides_payload(client):
    c, _ = client
    pid = _make_project(c)
    c.post(f"/api/projects/{pid}/zones", json={"id": "z1", "name": "Z1"})
    r = c.put(
        f"/api/projects/{pid}/zones/z1",
        json={"id": "ATTEMPT-RENAME", "name": "Updated"},
    )
    assert r.status_code == 200
    ids = {z["id"] for z in r.json()["config"]["building"]["zones"]}
    assert "z1" in ids
    assert "ATTEMPT-RENAME" not in ids


def test_update_unknown_zone_returns_404(client):
    c, _ = client
    pid = _make_project(c)
    r = c.put(
        f"/api/projects/{pid}/zones/ghost",
        json={"id": "ghost", "name": "x"},
    )
    assert r.status_code == 404


def test_update_zone_rejects_bad_hvac_ref_via_get_meta(client):
    """The PUT itself doesn't FK-check (validate=False on disk), but the
    /config response's hvac_summary should surface the dangling ref."""
    c, _ = client
    pid = _make_project(c)
    c.post(f"/api/projects/{pid}/zones", json={"id": "z1", "name": "Z1"})
    r = c.put(
        f"/api/projects/{pid}/zones/z1",
        json={"id": "z1", "name": "Z1", "hvac_zone_id": "GHOST"},
    )
    assert r.status_code == 200
    cfg_r = c.get(f"/api/projects/{pid}/config")
    summary = cfg_r.json()["config"]["_annotations"]["hvac_summary"]
    assert "GHOST" in summary["unknown_hvac_zone_refs"]
