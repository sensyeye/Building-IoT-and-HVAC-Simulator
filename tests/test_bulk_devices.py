"""Tests for the bulk device creation endpoint."""

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
        yield c


def _make_project(client: TestClient) -> str:
    r = client.post(
        "/api/projects",
        json={"name": "Bulk P", "building_type": "office"},
    )
    assert r.status_code == 201
    return r.json()["id"]


def _add_zone(client: TestClient, pid: str, zid: str, *, name: str | None = None,
              area: float | None = None, capacity: int | None = None) -> None:
    body = {"id": zid, "name": name or zid}
    if area is not None:
        body["area_m2"] = area
    if capacity is not None:
        body["capacity"] = capacity
    r = client.post(f"/api/projects/{pid}/zones", json=body)
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_bulk_round_robin_distributes_evenly(client):
    pid = _make_project(client)
    _add_zone(client, pid, "zone-a", name="Lobby")
    _add_zone(client, pid, "zone-b", name="Office")
    _add_zone(client, pid, "zone-c", name="Cafe")

    r = client.post(
        f"/api/projects/{pid}/devices/bulk",
        json={"items": [{"type": "iaq", "count": 6}], "zone_strategy": "round_robin"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    created = body["created"]
    assert len(created) == 6
    by_zone = {}
    for d in created:
        by_zone[d["zone_id"]] = by_zone.get(d["zone_id"], 0) + 1
    # zone-default + 3 added zones, but zone-default is included in
    # the round-robin too, so 6 split across 4 zones = 2,2,1,1.
    assert sorted(by_zone.values())[-2:] == [2, 2]


def test_bulk_fill_packs_into_first_zone(client):
    pid = _make_project(client)
    _add_zone(client, pid, "zone-a", name="Lobby")
    r = client.post(
        f"/api/projects/{pid}/devices/bulk",
        json={"items": [{"type": "iaq", "count": 4}], "zone_strategy": "fill"},
    )
    assert r.status_code == 201
    created = r.json()["created"]
    assert all(d["zone_id"] == "zone-default" for d in created)
    assert {d["name"] for d in created} == {
        "Default Zone IAQ 01",
        "Default Zone IAQ 02",
        "Default Zone IAQ 03",
        "Default Zone IAQ 04",
    }


def test_bulk_by_capacity_weights_by_capacity(client):
    pid = _make_project(client)
    # Remove the default zone for a clean weighting test.
    _add_zone(client, pid, "big", name="Big", capacity=80)
    _add_zone(client, pid, "small", name="Small", capacity=20)
    # Default zone has capacity 20 from the skeleton; total capacity = 120.
    r = client.post(
        f"/api/projects/{pid}/devices/bulk",
        json={
            "items": [{"type": "iaq", "count": 12}],
            "zone_strategy": "by_capacity",
        },
    )
    assert r.status_code == 201
    created = r.json()["created"]
    by_zone = {}
    for d in created:
        by_zone[d["zone_id"]] = by_zone.get(d["zone_id"], 0) + 1
    # 80/120 * 12 = 8, 20/120 * 12 = 2, 20/120 * 12 = 2
    assert by_zone.get("big") == 8
    assert by_zone.get("small") == 2
    assert by_zone.get("zone-default") == 2


def test_bulk_explicit_zone_id_overrides_strategy(client):
    pid = _make_project(client)
    _add_zone(client, pid, "mech", name="Mechanical")
    r = client.post(
        f"/api/projects/{pid}/devices/bulk",
        json={
            "items": [
                {"type": "energy_meter", "count": 3, "zone_id": "mech",
                 "metadata": {"submeter": "hvac", "nominal_kw": 50}},
            ],
            "zone_strategy": "round_robin",
        },
    )
    assert r.status_code == 201, r.text
    created = r.json()["created"]
    assert {d["zone_id"] for d in created} == {"mech"}
    assert all(d["metadata"]["submeter"] == "hvac" for d in created)
    assert all(d["metadata"]["nominal_kw"] == 50 for d in created)


def test_bulk_names_are_unique_and_continue_numbering(client):
    pid = _make_project(client)
    # First call: 2 IAQ in zone-default.
    r1 = client.post(
        f"/api/projects/{pid}/devices/bulk",
        json={"items": [{"type": "iaq", "count": 2, "zone_id": "zone-default"}]},
    )
    assert r1.status_code == 201
    # Second call: should continue numbering from 03, not restart at 01.
    r2 = client.post(
        f"/api/projects/{pid}/devices/bulk",
        json={"items": [{"type": "iaq", "count": 2, "zone_id": "zone-default"}]},
    )
    assert r2.status_code == 201
    cfg = r2.json()["config"]
    names = sorted(d["name"] for d in cfg["devices"])
    assert names == [
        "Default Zone IAQ 01",
        "Default Zone IAQ 02",
        "Default Zone IAQ 03",
        "Default Zone IAQ 04",
    ]
    euis = [d["device_eui"] for d in cfg["devices"]]
    assert len(set(euis)) == len(euis), "EUIs must be unique"


def test_bulk_mixed_types_in_one_request(client):
    pid = _make_project(client)
    r = client.post(
        f"/api/projects/{pid}/devices/bulk",
        json={
            "items": [
                {"type": "iaq", "count": 2},
                {"type": "energy_meter", "count": 1,
                 "metadata": {"submeter": "main", "nominal_kw": 25}},
                {"type": "entry_exit_counter", "count": 1},
            ],
        },
    )
    assert r.status_code == 201
    created = r.json()["created"]
    types = sorted(d["type"] for d in created)
    assert types == ["energy_meter", "entry_exit_counter", "iaq", "iaq"]


def test_bulk_name_prefix(client):
    pid = _make_project(client)
    r = client.post(
        f"/api/projects/{pid}/devices/bulk",
        json={
            "items": [{"type": "iaq", "count": 1, "zone_id": "zone-default"}],
            "name_prefix": "B1",
        },
    )
    assert r.status_code == 201
    assert r.json()["created"][0]["name"].startswith("B1 ")


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_bulk_unknown_sensor_type_400(client):
    pid = _make_project(client)
    r = client.post(
        f"/api/projects/{pid}/devices/bulk",
        json={"items": [{"type": "nope", "count": 2}]},
    )
    assert r.status_code == 400
    assert "unknown sensor type" in r.json()["detail"]


def test_bulk_unknown_zone_400(client):
    pid = _make_project(client)
    r = client.post(
        f"/api/projects/{pid}/devices/bulk",
        json={"items": [{"type": "iaq", "count": 1, "zone_id": "ghost"}]},
    )
    assert r.status_code == 400


def test_bulk_no_zones_400(client):
    """When a project somehow has zero zones, bulk-add returns 400."""
    pid = _make_project(client)
    # Drop all zones by writing the config directly through the API.
    cfg_resp = client.get(f"/api/projects/{pid}/config").json()
    cfg = cfg_resp["config"]
    cfg["building"]["zones"] = []
    # We can't PUT this through the validated endpoint (loader requires
    # zones), so we hit the service layer that backs the fixture.
    from api.routes import config as config_module
    config_module.project_service.set_config(pid, cfg, validate=False)

    r = client.post(
        f"/api/projects/{pid}/devices/bulk",
        json={"items": [{"type": "iaq", "count": 1}]},
    )
    assert r.status_code == 400
    assert "no zones" in r.json()["detail"]


def test_bulk_bad_strategy_400(client):
    pid = _make_project(client)
    r = client.post(
        f"/api/projects/{pid}/devices/bulk",
        json={"items": [{"type": "iaq", "count": 1}], "zone_strategy": "weird"},
    )
    assert r.status_code == 400


def test_bulk_count_zero_rejected_by_schema(client):
    pid = _make_project(client)
    r = client.post(
        f"/api/projects/{pid}/devices/bulk",
        json={"items": [{"type": "iaq", "count": 0}]},
    )
    # Pydantic rejects count < 1.
    assert r.status_code == 422
