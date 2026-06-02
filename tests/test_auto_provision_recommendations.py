"""Tests for the recommendation-driven auto-provisioner + catalog routes (P9.5)."""

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


def _make_project(c: TestClient) -> str:
    r = c.post(
        "/api/projects",
        json={"name": "RecAutoprov", "building_type": "office"},
    )
    assert r.status_code == 201
    return r.json()["id"]


# ---------------------------------------------------------------------------
# Catalog routes
# ---------------------------------------------------------------------------


def test_monitoring_intents_route(client):
    c, _ = client
    r = c.get("/api/monitoring-intents")
    assert r.status_code == 200
    body = r.json()
    ids = {i["id"] for i in body["monitoring_intents"]}
    assert {"comfort", "iaq", "people_flow", "guest_room_automation"} <= ids
    assert body["count"] == len(body["monitoring_intents"])


def test_richness_levels_route(client):
    c, _ = client
    r = c.get("/api/richness-levels")
    assert r.status_code == 200
    body = r.json()
    ids = {i["id"] for i in body["richness_levels"]}
    assert ids == {"basic", "standard", "advanced"}


def test_monitoring_recommendations_route(client):
    c, _ = client
    r = c.get(
        "/api/monitoring-recommendations",
        params={"room_type": "hotel_guest_room",
                "intent": "guest_room_automation",
                "richness": "advanced"},
    )
    assert r.status_code == 200
    body = r.json()
    types = {d["type"] for d in body["devices"]}
    assert "iaq" in types
    assert "occupancy_sensor" in types
    assert "door_contact" in types
    # HVAC virtual point is included in the plan even though it's not
    # yet emitting (the provisioner will skip it).
    assert "hvac" in types
    assert body["count"] == len(body["devices"])


# ---------------------------------------------------------------------------
# Auto-provisioner using the recommendation engine
# ---------------------------------------------------------------------------


def test_recommendation_path_used_when_intent_set(client):
    c, svc = client
    pid = _make_project(c)
    c.post(
        f"/api/projects/{pid}/zones",
        json={
            "id": "guest-1",
            "name": "Guest Room 1",
            "room_type": "hotel_guest_room",
            "monitoring_intent": "guest_room_automation",
            "monitoring_richness": "advanced",
        },
    )

    result = svc.auto_provision_devices(pid, dry_run=True)
    guest_added = [e for e in result["to_add"] if e["zone_id"] == "guest-1"]
    assert guest_added
    assert {e["source"] for e in guest_added} == {"recommendation"}

    types_added = {e["sensor_type"] for e in guest_added}
    assert {"iaq", "occupancy_sensor", "door_contact", "hvac"} <= types_added

    # HVAC is now implemented (P11.2) so it should be added, not skipped.
    hvac_skipped = [
        e for e in result["skipped"]
        if e.get("sensor_type") == "hvac"
        and e.get("zone_id") == "guest-1"
    ]
    assert not hvac_skipped, "HVAC is implemented; it shouldn't be in skipped"


def test_recommendation_apply_persists_devices_with_roles(client):
    c, svc = client
    pid = _make_project(c)
    c.post(
        f"/api/projects/{pid}/zones",
        json={
            "id": "office-1",
            "name": "Open Office",
            "room_type": "open_office",
            "monitoring_intent": "energy",
            "monitoring_richness": "advanced",
        },
    )
    result = svc.auto_provision_devices(pid, dry_run=False)
    cfg = result["config"]
    devs = [d for d in cfg["devices"] if d["zone_id"] == "office-1"]
    submeters = sorted(
        d["metadata"].get("submeter") for d in devs if d["type"] == "energy_meter"
    )
    # Advanced energy bundle yields four distinct submeter roles.
    assert submeters == ["hvac", "lighting", "main", "plug"]
    # And names carry the role for human readability.
    names = [d["name"] for d in devs if d["type"] == "energy_meter"]
    assert any("lighting" in n or "plug" in n for n in names)


def test_recommendation_path_is_idempotent(client):
    c, svc = client
    pid = _make_project(c)
    c.post(
        f"/api/projects/{pid}/zones",
        json={
            "id": "mtg-1",
            "name": "Meeting 1",
            "room_type": "meeting_room",
            "monitoring_intent": "iaq",
            "monitoring_richness": "advanced",
        },
    )
    first = svc.auto_provision_devices(pid, dry_run=False)
    assert first["to_add"]

    second = svc.auto_provision_devices(pid, dry_run=True)
    # Nothing new should be planned; every implemented device skipped as
    # "already present".
    implemented_skips = [
        s for s in second["skipped"]
        if s.get("sensor_type") in {"iaq", "occupancy_sensor"}
    ]
    assert implemented_skips
    assert all(s["reason"] == "already present" for s in implemented_skips)
    assert second["to_add"] == []


def test_legacy_profile_path_unchanged_when_no_intent(client):
    """Zones without intent/richness still flow through the profile path."""
    c, svc = client
    pid = _make_project(c)
    c.post(
        f"/api/projects/{pid}/zones",
        json={
            "id": "lobby-1",
            "name": "Lobby",
            "room_type": "lobby",
            # no monitoring_intent / no monitoring_richness
        },
    )
    result = svc.auto_provision_devices(pid, dry_run=True)
    lobby_added = [e for e in result["to_add"] if e["zone_id"] == "lobby-1"]
    assert lobby_added
    assert {e["source"] for e in lobby_added} == {"profile"}
    for entry in lobby_added:
        assert entry.get("profile_id"), "profile path must carry profile_id"


def test_energy_submeter_dedup_across_runs(client):
    """Re-running auto-provision after a manual main meter must skip
    only that submeter, not all energy meters in the zone."""
    c, svc = client
    pid = _make_project(c)
    c.post(
        f"/api/projects/{pid}/zones",
        json={
            "id": "office-1",
            "name": "Office",
            "room_type": "open_office",
            "monitoring_intent": "energy",
            "monitoring_richness": "advanced",
        },
    )
    # Apply once, then run again — second pass should skip all four
    # submeters as already present and add nothing.
    svc.auto_provision_devices(pid, dry_run=False)
    second = svc.auto_provision_devices(pid, dry_run=True)
    energy_to_add = [e for e in second["to_add"] if e["sensor_type"] == "energy_meter"]
    energy_skipped = [
        s for s in second["skipped"] if s["sensor_type"] == "energy_meter"
    ]
    assert energy_to_add == []
    assert len(energy_skipped) == 4
