"""Tests for the monitoring-profile catalog, coverage math, and API."""

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
from simulator.catalogs import (
    compute_zone_coverage,
    default_profile_for_room_type,
    get_monitoring_profile,
    list_monitoring_profiles,
)


# ---------------------------------------------------------------------------
# Catalog basics
# ---------------------------------------------------------------------------


def test_profiles_present_and_well_formed():
    profiles = list_monitoring_profiles()
    assert len(profiles) >= 9
    ids = {p["id"] for p in profiles}
    # Every id referenced by room_types.json must exist as a profile.
    expected = {
        "basic_environmental",
        "energy_advanced",
        "energy_basic",
        "guest_room_advanced",
        "hvac_operational",
        "iaq_occupancy",
        "iaq_standard",
        "mall_people_flow",
        "research_advanced",
    }
    assert expected <= ids
    for p in profiles:
        assert {"id", "name", "required_sensor_types"} <= p.keys()


def test_profile_lookup_roundtrip_and_isolation():
    p = get_monitoring_profile("iaq_occupancy")
    assert p is not None
    p["required_sensor_types"].append("MUTATION")
    fresh = get_monitoring_profile("iaq_occupancy")
    assert "MUTATION" not in fresh["required_sensor_types"]
    assert get_monitoring_profile("does_not_exist") is None


def test_default_profile_for_room_type():
    # Spec: meeting_room.typical_monitoring_profiles[0] == "iaq_standard"
    assert default_profile_for_room_type("meeting_room") == "iaq_standard"
    assert default_profile_for_room_type("hotel_guest_room") == "guest_room_advanced"
    assert default_profile_for_room_type("does_not_exist") is None


# ---------------------------------------------------------------------------
# Coverage math
# ---------------------------------------------------------------------------


def _zone(id_="z1", **extra):
    return {"id": id_, "room_type": "meeting_room", **extra}


def test_coverage_status_ok():
    out = compute_zone_coverage(
        _zone(monitoring_profile="iaq_occupancy"),
        [
            {"zone_id": "z1", "type": "iaq"},
            {"zone_id": "z1", "type": "entry_exit_counter"},
            {"zone_id": "z1", "type": "occupancy_sensor"},
        ],
    )
    assert out["status"] == "ok"
    assert out["missing_required"] == []
    assert out["missing_recommended"] == []


def test_coverage_status_partial_missing_recommended():
    out = compute_zone_coverage(
        _zone(monitoring_profile="iaq_occupancy"),
        [
            {"zone_id": "z1", "type": "iaq"},
            {"zone_id": "z1", "type": "entry_exit_counter"},
        ],
    )
    assert out["status"] == "partial"
    assert out["missing_required"] == []
    assert out["missing_recommended"] == ["occupancy_sensor"]


def test_coverage_status_missing_required():
    out = compute_zone_coverage(
        _zone(monitoring_profile="iaq_occupancy"),
        [{"zone_id": "z1", "type": "iaq"}],
    )
    assert out["status"] == "missing"
    assert "entry_exit_counter" in out["missing_required"]


def test_coverage_inferred_from_room_type_when_no_explicit_profile():
    out = compute_zone_coverage(_zone(), [])
    assert out["profile_id"] == "iaq_standard"
    assert out["profile_inferred"] is True
    assert out["status"] == "missing"


def test_coverage_no_profile_when_room_type_unknown_and_no_explicit():
    out = compute_zone_coverage({"id": "z1", "room_type": "does_not_exist"}, [])
    assert out["status"] == "no_profile"
    assert out["profile_id"] is None


def test_coverage_ignores_devices_in_other_zones():
    out = compute_zone_coverage(
        _zone(monitoring_profile="iaq_standard"),
        [
            {"zone_id": "other", "type": "iaq"},
            {"zone_id": "z1", "type": "iaq"},
        ],
    )
    assert out["status"] == "ok"
    assert out["present"] == {"iaq": 1}


def test_coverage_extra_devices_listed():
    out = compute_zone_coverage(
        _zone(monitoring_profile="iaq_standard"),
        [
            {"zone_id": "z1", "type": "iaq"},
            {"zone_id": "z1", "type": "energy_meter"},
        ],
    )
    assert out["status"] == "ok"
    assert "energy_meter" in out["extra"]


# ---------------------------------------------------------------------------
# HTTP: catalog endpoints + GET /config coverage annotation
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


def test_monitoring_profiles_endpoint(client):
    c, _ = client
    r = c.get("/api/monitoring-profiles")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 9
    assert any(p["id"] == "iaq_occupancy" for p in body["monitoring_profiles"])


def test_monitoring_profile_by_id_endpoint(client):
    c, _ = client
    r = c.get("/api/monitoring-profiles/iaq_occupancy")
    assert r.status_code == 200
    p = r.json()["monitoring_profile"]
    assert p["id"] == "iaq_occupancy"
    assert "iaq" in p["required_sensor_types"]
    r2 = c.get("/api/monitoring-profiles/does_not_exist")
    assert r2.status_code == 404


def _make_project(c: TestClient) -> str:
    r = c.post("/api/projects", json={"name": "Cov P", "building_type": "office"})
    assert r.status_code == 201
    return r.json()["id"]


def test_config_get_annotates_monitoring_coverage(client):
    c, _ = client
    pid = _make_project(c)
    # Two zones: one with an explicit profile + one device, one without.
    c.post(
        f"/api/projects/{pid}/zones",
        json={
            "id": "mtg-1",
            "name": "Meeting Room 1",
            "monitoring_profile": "iaq_occupancy",
        },
    )
    c.post(
        f"/api/projects/{pid}/zones",
        json={"id": "lobby-1", "name": "Lobby"},
    )
    c.post(
        f"/api/projects/{pid}/devices",
        json={
            "device_eui": "aa11bb22cc33dd44",
            "name": "IAQ Meeting",
            "type": "iaq",
            "zone_id": "mtg-1",
        },
    )

    r = c.get(f"/api/projects/{pid}/config")
    assert r.status_code == 200
    cfg = r.json()["config"]
    zones = {z["id"]: z for z in cfg["building"]["zones"]}

    mtg = zones["mtg-1"]
    assert mtg["monitoring_coverage"]["profile_id"] == "iaq_occupancy"
    assert mtg["monitoring_coverage"]["profile_inferred"] is False
    assert mtg["monitoring_coverage"]["status"] == "missing"
    assert "entry_exit_counter" in mtg["monitoring_coverage"]["missing_required"]

    lobby = zones["lobby-1"]
    # Lobby's first typical profile is iaq_standard per room_types.json.
    assert lobby["monitoring_coverage"]["profile_id"] == "iaq_standard"
    assert lobby["monitoring_coverage"]["profile_inferred"] is True

    rollup = cfg["_annotations"]["monitoring_coverage_summary"]
    assert isinstance(rollup, dict)
    assert sum(rollup.values()) >= 2


def test_coverage_annotation_does_not_persist(client):
    c, proj_svc = client
    pid = _make_project(c)
    c.post(
        f"/api/projects/{pid}/zones",
        json={"id": "z1", "name": "Meeting Room 1"},
    )
    yaml_before = proj_svc.get_config_yaml(pid)
    # Hit GET twice — annotation must not mutate disk.
    c.get(f"/api/projects/{pid}/config")
    c.get(f"/api/projects/{pid}/config")
    yaml_after = proj_svc.get_config_yaml(pid)
    assert yaml_after == yaml_before
    assert "monitoring_coverage" not in yaml_after
    assert "_annotations" not in yaml_after
