"""Tests for the scenarios catalog + per-project assignment routes."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.services.event_service import EventService
from api.services.project_service import ProjectService
import api.routes.projects as projects_module
import api.routes.scenarios as scenarios_module
import api.routes.web as web_module
import api.services.event_service as event_module


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    proj_svc = ProjectService(data_dir=tmp_path)
    evt_svc = EventService(data_dir=tmp_path)

    monkeypatch.setattr(projects_module, "project_service", proj_svc)
    monkeypatch.setattr(projects_module, "event_service", evt_svc)
    monkeypatch.setattr(scenarios_module, "project_service", proj_svc)
    monkeypatch.setattr(scenarios_module, "event_service", evt_svc)
    monkeypatch.setattr(web_module, "project_service", proj_svc)
    monkeypatch.setattr(web_module, "event_service", evt_svc)
    monkeypatch.setattr(event_module, "event_service", evt_svc)

    with TestClient(app) as c:
        yield c


def _make_project(client: TestClient) -> str:
    r = client.post(
        "/api/projects",
        json={"name": "Scen P", "building_type": "office"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_catalog_endpoint_returns_known_ids(client):
    r = client.get("/api/scenarios")
    assert r.status_code == 200
    body = r.json()
    assert "scenarios" in body
    ids = {s["id"] for s in body["scenarios"]}
    assert "meeting_room_poor_ventilation" in ids
    assert "gateway_outage" in ids
    for s in body["scenarios"]:
        assert {"id", "name", "description", "category"} <= s.keys()


def test_get_scenarios_initially_empty_then_put_roundtrip(client):
    pid = _make_project(client)

    r = client.get(f"/api/projects/{pid}/scenarios")
    assert r.status_code == 200
    assert r.json()["scenarios"] == []

    payload = {
        "scenarios": [
            {
                "id": "meeting_room_poor_ventilation",
                "enabled": True,
                "start": "2025-01-01T08:00",
                "end": "2025-01-01T18:00",
            },
            {"id": "after_hours_energy_waste", "enabled": False},
            {"id": "not-a-real-id", "enabled": True},  # should be dropped
        ]
    }
    r = client.put(f"/api/projects/{pid}/scenarios", json=payload)
    assert r.status_code == 200, r.text
    saved = r.json()["scenarios"]
    ids = [s["id"] for s in saved]
    assert "meeting_room_poor_ventilation" in ids
    assert "after_hours_energy_waste" in ids
    assert "not-a-real-id" not in ids
    enabled = next(s for s in saved if s["id"] == "meeting_room_poor_ventilation")
    assert enabled["enabled"] is True
    assert enabled["start"] == "2025-01-01T08:00"

    # Roundtrip read.
    r = client.get(f"/api/projects/{pid}/scenarios")
    assert r.status_code == 200
    assert {s["id"] for s in r.json()["scenarios"]} == set(ids)


def test_put_scenarios_records_event(client):
    pid = _make_project(client)
    client.put(
        f"/api/projects/{pid}/scenarios",
        json={"scenarios": [{"id": "overcrowding", "enabled": True}]},
    )
    r = client.get(f"/api/projects/{pid}/events")
    # Web HTML route — just confirm the kind appears in the rendered table.
    assert r.status_code == 200
    assert "scenario" in r.text.lower()


def test_unknown_project_returns_404(client):
    r = client.get("/api/projects/does-not-exist/scenarios")
    assert r.status_code == 404
    r = client.put(
        "/api/projects/does-not-exist/scenarios",
        json={"scenarios": []},
    )
    assert r.status_code == 404


def test_project_detail_page_renders_scenario_tab(client):
    pid = _make_project(client)
    r = client.get(f"/projects/{pid}")
    assert r.status_code == 200
    assert "Scenarios" in r.text
    assert "data-tab-pane=\"scenarios\"" in r.text
    # And the catalog should be embedded.
    assert "meeting_room_poor_ventilation" in r.text
