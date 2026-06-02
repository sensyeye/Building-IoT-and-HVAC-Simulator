"""Tests for the room-type / building-archetype catalogs + read-time inference."""

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
    DEFAULT_ROOM_TYPE_ID,
    get_building_archetype,
    get_room_type,
    infer_room_type,
    list_building_archetypes,
    list_room_types,
)


# ---------------------------------------------------------------------------
# Pure-Python catalog tests
# ---------------------------------------------------------------------------


def test_archetypes_present_and_well_formed():
    archetypes = list_building_archetypes()
    assert len(archetypes) >= 12
    ids = {a["id"] for a in archetypes}
    # Spec §5 — these 12 must all exist.
    assert {
        "office",
        "hotel",
        "shopping_mall",
        "school",
        "hospital",
        "residential",
        "retail",
        "warehouse",
        "datacenter",
        "restaurant",
        "gym",
        "laboratory",
    } <= ids
    for a in archetypes:
        assert {"id", "name", "default_business_hours", "room_mix"} <= a.keys()
        assert isinstance(a["room_mix"], dict) and a["room_mix"]


def test_room_types_present_and_well_formed():
    room_types = list_room_types()
    assert len(room_types) >= 30
    ids = {rt["id"] for rt in room_types}
    # Spot-check a few mandatory entries from spec §8.
    assert {
        "open_office",
        "meeting_room",
        "phone_booth",
        "server_room",
        "classroom",
        "patient_room",
        "restaurant_kitchen",
        "hotel_guest_room",
    } <= ids
    for rt in room_types:
        assert {"id", "name", "default_area_m2", "default_capacity"} <= rt.keys()


def test_get_building_archetype_roundtrip_and_unknown():
    office = get_building_archetype("office")
    assert office is not None
    assert office["name"]
    # Returned copies must be independent.
    office["name"] = "MUTATED"
    assert get_building_archetype("office")["name"] != "MUTATED"
    assert get_building_archetype("does_not_exist") is None


def test_get_room_type_roundtrip_and_unknown():
    mr = get_room_type("meeting_room")
    assert mr is not None
    assert mr["default_capacity"] > 0
    mr["default_capacity"] = -999
    assert get_room_type("meeting_room")["default_capacity"] != -999
    assert get_room_type("does_not_exist") is None


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Meeting Room 1", "meeting_room"),
        ("Boardroom 5", "meeting_room"),
        ("Open Office", "open_office"),
        ("Workspace 2F", "open_office"),
        ("Server Room A", "server_room"),
        ("Comms Room", "server_room"),
        ("Lobby East", "lobby"),
        ("Atrium", "lobby"),
        ("Toilet 2F", "restroom"),
        ("WC", "restroom"),
        ("Lab 3", "lab"),
        ("Phone Booth #2", "phone_booth"),
        ("Bedroom", "apartment_bedroom"),
        ("Café Central", "restaurant_dining"),
        ("Galley", "restaurant_kitchen"),
        ("Cold Room", "cold_room"),
        ("Parking Level B1", "parking_area"),
        ("Classroom 12", "classroom"),
        ("Lecture Hall A", "lecture_hall"),
        ("Hotel Guest Room 305", "hotel_guest_room"),
    ],
)
def test_infer_room_type_known_keywords(name, expected):
    rt_id, was_inferred = infer_room_type(name)
    assert rt_id == expected
    assert was_inferred is True


def test_infer_room_type_default_fallback():
    rt_id, was_inferred = infer_room_type("Mystery Place 9000")
    assert rt_id == DEFAULT_ROOM_TYPE_ID == "open_office"
    assert was_inferred is True


def test_infer_room_type_uses_zone_id_when_name_is_blank():
    rt_id, was_inferred = infer_room_type("", "kitchen-2f")
    assert rt_id == "restaurant_kitchen"
    assert was_inferred is True


# ---------------------------------------------------------------------------
# API: catalog endpoints + read-time inference annotation
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


def test_building_archetypes_endpoint(client):
    c, _ = client
    r = c.get("/api/building-archetypes")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 12
    assert any(a["id"] == "office" for a in body["archetypes"])


def test_building_archetype_by_id_endpoint(client):
    c, _ = client
    r = c.get("/api/building-archetypes/office")
    assert r.status_code == 200
    assert r.json()["archetype"]["id"] == "office"
    r2 = c.get("/api/building-archetypes/does_not_exist")
    assert r2.status_code == 404


def test_room_types_endpoint(client):
    c, _ = client
    r = c.get("/api/room-types")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 30
    assert any(rt["id"] == "meeting_room" for rt in body["room_types"])


def test_room_type_by_id_endpoint(client):
    c, _ = client
    r = c.get("/api/room-types/meeting_room")
    assert r.status_code == 200
    assert r.json()["room_type"]["id"] == "meeting_room"
    r2 = c.get("/api/room-types/does_not_exist")
    assert r2.status_code == 404


def _make_project(c: TestClient) -> str:
    r = c.post("/api/projects", json={"name": "Room P", "building_type": "office"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_legacy_zones_get_annotated_with_inferred_room_type(client):
    c, _ = client
    pid = _make_project(c)
    # Add two zones with no room_type → should be inferred.
    assert c.post(
        f"/api/projects/{pid}/zones",
        json={"id": "z1", "name": "Meeting Room 1"},
    ).status_code == 201
    assert c.post(
        f"/api/projects/{pid}/zones",
        json={"id": "z2", "name": "Server Room A"},
    ).status_code == 201

    r = c.get(f"/api/projects/{pid}/config")
    assert r.status_code == 200
    zones = {z["id"]: z for z in r.json()["config"]["building"]["zones"]}
    assert zones["z1"]["room_type"] == "meeting_room"
    assert zones["z1"]["room_type_inferred"] is True
    assert zones["z2"]["room_type"] == "server_room"
    assert zones["z2"]["room_type_inferred"] is True


def test_explicit_room_type_is_not_flagged_as_inferred(client):
    c, _ = client
    pid = _make_project(c)
    assert c.post(
        f"/api/projects/{pid}/zones",
        json={
            "id": "z1",
            "name": "Some Random Name",
            "room_type": "phone_booth",
            "floor_id": "F1",
            "exposure": "interior",
            "ventilation_quality": "high",
        },
    ).status_code == 201

    r = c.get(f"/api/projects/{pid}/config")
    assert r.status_code == 200
    zones = {z["id"]: z for z in r.json()["config"]["building"]["zones"]}
    zone = zones["z1"]
    assert zone["room_type"] == "phone_booth"
    assert zone["room_type_inferred"] is False
    # Optional metadata round-trips.
    assert zone["floor_id"] == "F1"
    assert zone["exposure"] == "interior"
    assert zone["ventilation_quality"] == "high"


def test_legacy_yaml_on_disk_is_not_rewritten_by_get(client, tmp_path: Path):
    c, proj_svc = client
    pid = _make_project(c)
    assert c.post(
        f"/api/projects/{pid}/zones",
        json={"id": "z1", "name": "Open Office"},
    ).status_code == 201

    yaml_before = proj_svc.get_config_yaml(pid)
    assert "room_type" not in yaml_before  # never persisted

    # Hit GET twice — annotation must not mutate disk.
    c.get(f"/api/projects/{pid}/config")
    c.get(f"/api/projects/{pid}/config")
    yaml_after = proj_svc.get_config_yaml(pid)
    assert yaml_after == yaml_before
    assert "room_type" not in yaml_after
