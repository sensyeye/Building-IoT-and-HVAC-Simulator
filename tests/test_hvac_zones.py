"""Tests for Phase 3: HVAC zones + scenario-targeting fan-out."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.routes.bridge as bridge_module
import api.routes.config as config_module
import api.routes.live as live_module
import api.routes.projects as projects_module
import api.routes.scenarios as scenarios_module
import api.routes.web as web_module
import api.services.event_service as event_module
from api.main import app
from api.services.event_service import EventService
from api.services.project_service import ProjectService
from simulator.config_loader import ConfigError, load_config
from simulator.scenarios import (
    active_scenario_assignments_at,
    resolve_assignment_zone_targets,
)


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "cfg.yaml"
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Schema-level: parsing + cross-validation
# ---------------------------------------------------------------------------


_BASE_YAML = """
building:
  id: bld-test
  name: Test
  zones:
    - id: z1
      name: Room 1
      hvac_zone_id: H1
    - id: z2
      name: Room 2
      hvac_zone_id: H2
  hvac_zones:
    - id: H1
      name: AHU North
      system_type: ahu
      setpoint_c: 22.5
      capacity_kw: 30.0
    - id: H2
      name: VRF South
      system_type: vrf
devices:
  - device_eui: AAA
    name: IAQ
    type: iaq
    zone_id: z1
simulation:
  mode: live
  interval_seconds: 30
"""


def test_config_loader_parses_hvac_zones_and_fk(tmp_path: Path):
    cfg = load_config(_write(tmp_path, _BASE_YAML))
    assert len(cfg.building.hvac_zones) == 2
    ids = {h.id for h in cfg.building.hvac_zones}
    assert ids == {"H1", "H2"}
    h1 = next(h for h in cfg.building.hvac_zones if h.id == "H1")
    assert h1.system_type == "ahu"
    assert h1.setpoint_c == 22.5
    assert h1.capacity_kw == 30.0


def test_config_loader_rejects_unknown_hvac_zone_id(tmp_path: Path):
    bad = _BASE_YAML.replace("hvac_zone_id: H2", "hvac_zone_id: NOPE")
    with pytest.raises(ConfigError) as ei:
        load_config(_write(tmp_path, bad))
    assert "NOPE" in str(ei.value) or "hvac_zone_id" in str(ei.value)


# ---------------------------------------------------------------------------
# Resolver: target-zone fan-out
# ---------------------------------------------------------------------------


def test_resolve_targets_none_means_global():
    assert resolve_assignment_zone_targets({"id": "s1", "enabled": True}) is None


def test_resolve_targets_explicit_zone_ids():
    out = resolve_assignment_zone_targets(
        {"target_zone_ids": ["z1", "z3"]}, {"z1": "H1", "z2": "H1", "z3": None}
    )
    assert out == {"z1", "z3"}


def test_resolve_targets_hvac_fan_out():
    out = resolve_assignment_zone_targets(
        {"target_hvac_zone_id": "H1"},
        {"z1": "H1", "z2": "H1", "z3": "H2"},
    )
    assert out == {"z1", "z2"}


def test_resolve_targets_hvac_plus_explicit_union():
    out = resolve_assignment_zone_targets(
        {"target_hvac_zone_id": "H1", "target_zone_ids": ["zX"]},
        {"z1": "H1", "z2": "H2"},
    )
    assert out == {"z1", "zX"}


def test_active_scenario_assignments_carries_targeting():
    assignments = [
        {
            "id": "s1",
            "enabled": True,
            "target_hvac_zone_id": "H1",
        },
        {"id": "s2", "enabled": False},
    ]
    active = active_scenario_assignments_at(
        datetime(2025, 1, 1, tzinfo=timezone.utc), assignments
    )
    assert len(active) == 1
    assert active[0]["id"] == "s1"
    assert active[0]["target_hvac_zone_id"] == "H1"


# ---------------------------------------------------------------------------
# HTTP: CRUD + annotation + scenario persistence
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
        scenarios_module,
    ):
        monkeypatch.setattr(mod, "project_service", proj_svc, raising=False)
        monkeypatch.setattr(mod, "event_service", evt_svc, raising=False)
    monkeypatch.setattr(event_module, "event_service", evt_svc)
    with TestClient(app) as c:
        yield c, proj_svc


def _make_project(c: TestClient) -> str:
    r = c.post("/api/projects", json={"name": "HVAC P", "building_type": "office"})
    assert r.status_code == 201
    return r.json()["id"]


def test_add_and_remove_hvac_zone(client):
    c, _ = client
    pid = _make_project(c)
    r = c.post(
        f"/api/projects/{pid}/hvac-zones",
        json={
            "id": "H1",
            "name": "AHU 1",
            "system_type": "ahu",
            "setpoint_c": 21.0,
            "capacity_kw": 25.0,
        },
    )
    assert r.status_code == 201, r.text
    hz = r.json()["config"]["building"]["hvac_zones"]
    assert len(hz) == 1 and hz[0]["id"] == "H1"

    r2 = c.delete(f"/api/projects/{pid}/hvac-zones/H1")
    assert r2.status_code == 200
    assert r2.json()["config"]["building"]["hvac_zones"] == []


def test_remove_hvac_zone_refuses_when_referenced(client):
    c, _ = client
    pid = _make_project(c)
    c.post(f"/api/projects/{pid}/hvac-zones", json={"id": "H1", "name": "AHU 1"})
    c.post(
        f"/api/projects/{pid}/zones",
        json={"id": "z1", "name": "Room 1", "hvac_zone_id": "H1"},
    )
    r = c.delete(f"/api/projects/{pid}/hvac-zones/H1")
    assert r.status_code == 400
    assert "z1" in r.text or "referenced" in r.text.lower()


def test_remove_unknown_hvac_zone_returns_404(client):
    c, _ = client
    pid = _make_project(c)
    r = c.delete(f"/api/projects/{pid}/hvac-zones/ghost")
    assert r.status_code == 404


def test_config_get_annotates_hvac_summary(client):
    c, _ = client
    pid = _make_project(c)
    c.post(f"/api/projects/{pid}/hvac-zones", json={"id": "H1", "name": "A"})
    c.post(f"/api/projects/{pid}/hvac-zones", json={"id": "H_lonely", "name": "B"})
    c.post(
        f"/api/projects/{pid}/zones",
        json={"id": "z1", "name": "R1", "hvac_zone_id": "H1"},
    )
    c.post(
        f"/api/projects/{pid}/zones",
        json={"id": "z2", "name": "R2", "hvac_zone_id": "H_ghost"},
    )

    r = c.get(f"/api/projects/{pid}/config")
    assert r.status_code == 200
    cfg = r.json()["config"]
    summary = cfg["_annotations"]["hvac_summary"]
    assert summary["served_counts"]["H1"] == 1
    assert summary["served_counts"]["H_lonely"] == 0
    assert "H_lonely" in summary["orphan_hvac_zones"]
    assert "H_ghost" in summary["unknown_hvac_zone_refs"]

    # Per-zone served_room_count attached.
    hz_by_id = {h["id"]: h for h in cfg["building"]["hvac_zones"]}
    assert hz_by_id["H1"]["served_room_count"] == 1
    assert hz_by_id["H_lonely"]["served_room_count"] == 0


def test_scenario_assignment_persists_targeting_fields(client):
    c, proj_svc = client
    pid = _make_project(c)
    r = c.put(
        f"/api/projects/{pid}/scenarios",
        json={
            "scenarios": [
                {
                    "id": "meeting_room_poor_ventilation",
                    "enabled": True,
                    "target_hvac_zone_id": "H1",
                    "target_zone_ids": ["z1", "z2"],
                }
            ]
        },
    )
    assert r.status_code == 200, r.text
    saved = r.json()["scenarios"][0]
    assert saved["target_hvac_zone_id"] == "H1"
    assert saved["target_zone_ids"] == ["z1", "z2"]
    # And the service read-side surfaces them too.
    again = proj_svc.get_scenarios(pid)
    assert again[0]["target_hvac_zone_id"] == "H1"


def test_hvac_annotation_does_not_persist(client):
    c, proj_svc = client
    pid = _make_project(c)
    c.post(f"/api/projects/{pid}/hvac-zones", json={"id": "H1", "name": "A"})
    c.post(
        f"/api/projects/{pid}/zones",
        json={"id": "z1", "name": "R1", "hvac_zone_id": "H1"},
    )
    yaml_before = proj_svc.get_config_yaml(pid)
    c.get(f"/api/projects/{pid}/config")
    c.get(f"/api/projects/{pid}/config")
    yaml_after = proj_svc.get_config_yaml(pid)
    assert yaml_after == yaml_before
    assert "served_room_count" not in yaml_after
    assert "_annotations" not in yaml_after
