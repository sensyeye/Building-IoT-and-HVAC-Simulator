"""P4 tests for event filtering (status, query) and the clear endpoint.

These cover the new ``status`` and ``q`` filter parameters on
``EventService.recent`` plus the ``DELETE /api/projects/{id}/events``
HTTP endpoint and the HTMX events fragment filters.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.services.event_service import EventService, event_service
from api.services import project_service as project_service_module
from api.services.project_service import ProjectService


# ---------------------------------------------------------------------------
# EventService.recent — new filter params
# ---------------------------------------------------------------------------


@pytest.fixture
def svc(tmp_path: Path) -> EventService:
    return EventService(data_dir=tmp_path)


def test_recent_filter_by_status(svc: EventService) -> None:
    svc.record("p", kind="bridge_test", status="succeeded", summary="ok 1")
    svc.record("p", kind="bridge_test", status="failed", summary="oops")
    svc.record("p", kind="bridge_test", status="succeeded", summary="ok 2")
    out = svc.recent("p", status="failed")
    assert len(out) == 1
    assert out[0].summary == "oops"


def test_recent_filter_by_kind_and_status(svc: EventService) -> None:
    svc.record("p", kind="bridge_test", status="succeeded", summary="bok")
    svc.record("p", kind="live_run", status="succeeded", summary="lok")
    svc.record("p", kind="live_run", status="failed", summary="lfail")
    out = svc.recent("p", kind="live_run", status="failed")
    assert [e.summary for e in out] == ["lfail"]


def test_recent_query_matches_summary(svc: EventService) -> None:
    svc.record("p", kind="bridge_test", status="succeeded", summary="all 9 devices published")
    svc.record("p", kind="bridge_test", status="failed", summary="broker rejected payload")
    out = svc.recent("p", query="broker")
    assert len(out) == 1
    assert "broker" in out[0].summary


def test_recent_query_matches_details(svc: EventService) -> None:
    svc.record(
        "p",
        kind="bridge_test",
        status="succeeded",
        summary="all good",
        details={"device_eui": "fe00000000000042"},
    )
    svc.record("p", kind="bridge_test", status="succeeded", summary="other")
    out = svc.recent("p", query="00000042")
    assert len(out) == 1
    assert out[0].details["device_eui"].endswith("42")


def test_recent_query_is_case_insensitive(svc: EventService) -> None:
    svc.record("p", kind="bridge_test", status="succeeded", summary="Broker Rejected")
    out = svc.recent("p", query="broker")
    assert len(out) == 1


def test_recent_query_no_match_returns_empty(svc: EventService) -> None:
    svc.record("p", kind="bridge_test", status="succeeded", summary="hello world")
    assert svc.recent("p", query="zzz") == []


# ---------------------------------------------------------------------------
# EventService.clear
# ---------------------------------------------------------------------------


def test_clear_removes_log_and_returns_count(svc: EventService, tmp_path: Path) -> None:
    svc.record("p", kind="bridge_test", status="succeeded", summary="a")
    svc.record("p", kind="bridge_test", status="failed", summary="b")
    svc.record("p", kind="live_run", status="succeeded", summary="c")
    removed = svc.clear("p")
    assert removed == 3
    assert not (tmp_path / "p.events.jsonl").exists()
    assert svc.recent("p") == []


def test_clear_is_safe_when_no_log_exists(svc: EventService) -> None:
    assert svc.clear("never-existed") == 0


# ---------------------------------------------------------------------------
# HTTP API + HTMX fragment
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Test client backed by per-test data dirs (no leakage)."""
    new_svc = ProjectService(data_dir=tmp_path / "projects")
    monkeypatch.setattr(project_service_module, "project_service", new_svc)
    # Routes import the singleton at module import; patch by reference there too.
    from api.routes import projects as projects_routes
    from api.routes import web as web_routes
    monkeypatch.setattr(projects_routes, "project_service", new_svc)
    monkeypatch.setattr(web_routes, "project_service", new_svc)

    new_events = EventService(data_dir=tmp_path / "events")
    monkeypatch.setattr("api.services.event_service.event_service", new_events)
    monkeypatch.setattr(projects_routes, "event_service", new_events)
    monkeypatch.setattr(web_routes, "event_service", new_events)

    with TestClient(app) as c:
        yield c, new_svc, new_events


def _make_project(client_tuple) -> str:
    client, _, _ = client_tuple
    r = client.post("/api/projects", json={"name": "Filter test", "area_m2": 100, "floors": 1})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_api_events_filter_by_status(client):
    c, _, evt = client
    pid = _make_project(client)
    evt.record(pid, kind="bridge_test", status="succeeded", summary="ok")
    evt.record(pid, kind="bridge_test", status="failed", summary="boom")
    r = c.get(f"/api/projects/{pid}/events", params={"status": "failed"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["events"][0]["status"] == "failed"


def test_api_events_filter_by_q(client):
    c, _, evt = client
    pid = _make_project(client)
    evt.record(pid, kind="bridge_test", status="succeeded", summary="published 5 devices")
    evt.record(pid, kind="bridge_test", status="failed", summary="broker rejected")
    r = c.get(f"/api/projects/{pid}/events", params={"q": "broker"})
    assert r.status_code == 200
    assert r.json()["count"] == 1


def test_api_clear_events_removes_all(client):
    c, _, evt = client
    pid = _make_project(client)
    evt.record(pid, kind="bridge_test", status="succeeded", summary="a")
    evt.record(pid, kind="bridge_test", status="failed", summary="b")
    r = c.delete(f"/api/projects/{pid}/events")
    assert r.status_code == 200
    body = r.json()
    assert body["removed"] == 2
    # subsequent list returns 0
    r2 = c.get(f"/api/projects/{pid}/events")
    assert r2.json()["count"] == 0


def test_api_clear_events_404_for_unknown_project(client):
    c, _, _ = client
    r = c.delete("/api/projects/does-not-exist/events")
    assert r.status_code == 404


def test_html_events_fragment_respects_filters(client):
    c, _, evt = client
    pid = _make_project(client)
    evt.record(pid, kind="bridge_test", status="succeeded", summary="ok-row")
    evt.record(pid, kind="bridge_test", status="failed", summary="boom-row")
    r = c.get(
        f"/projects/{pid}/events",
        params={"status": "failed"},
    )
    assert r.status_code == 200
    html = r.text
    assert "boom-row" in html
    assert "ok-row" not in html


def test_html_events_fragment_empty_state_with_filter(client):
    c, _, evt = client
    pid = _make_project(client)
    evt.record(pid, kind="bridge_test", status="succeeded", summary="ok")
    r = c.get(f"/projects/{pid}/events", params={"q": "zzz-no-hit"})
    assert r.status_code == 200
    assert "No events match the current filter" in r.text


def test_html_events_fragment_micro_event_highlight(client):
    c, _, evt = client
    pid = _make_project(client)
    # Aggregated list flavour
    evt.record(
        pid,
        kind="live_run",
        status="info",
        summary="reading w/ micro-events",
        details={
            "micro_events": [
                {"id": "printer_plume", "name": "Printer plume", "zone_id": "zone-1"},
            ]
        },
    )
    # Per-incident flavour
    evt.record(
        pid,
        kind="live_run",
        status="info",
        summary="micro-event started: Sneeze cluster",
        details={"kind": "micro_event_start", "event_id": "sneeze_cluster", "zone_id": "zone-2"},
    )
    r = c.get(f"/projects/{pid}/events")
    assert r.status_code == 200
    html = r.text
    # Both rows should carry the purple highlight class
    assert html.count("bg-purple-50/40") >= 2
    # Aggregated row shows the named badge
    assert "Printer plume" in html
    # Per-incident row shows the event_id
    assert "sneeze_cluster" in html
