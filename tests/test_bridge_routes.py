"""Tests for the bridge / integration API routes."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.services.event_service import EventService, event_service as _real_event_svc
from api.services.project_service import ProjectService, project_service as _real_proj_svc
import api.routes.bridge as bridge_module
import api.routes.projects as projects_module
import api.routes.web as web_module
import api.services.event_service as event_module


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A TestClient backed by a temp data dir for projects + events."""
    proj_svc = ProjectService(data_dir=tmp_path)
    evt_svc = EventService(data_dir=tmp_path)

    # Patch every binding the routes reference.
    monkeypatch.setattr(bridge_module, "project_service", proj_svc)
    monkeypatch.setattr(bridge_module, "event_service", evt_svc)
    monkeypatch.setattr(projects_module, "project_service", proj_svc)
    monkeypatch.setattr(projects_module, "event_service", evt_svc)
    monkeypatch.setattr(web_module, "project_service", proj_svc)
    monkeypatch.setattr(web_module, "event_service", evt_svc)
    monkeypatch.setattr(event_module, "event_service", evt_svc)

    with TestClient(app) as c:
        c.svc = proj_svc  # type: ignore[attr-defined]
        c.evt = evt_svc  # type: ignore[attr-defined]
        yield c


def _make_project(client: TestClient) -> str:
    r = client.post(
        "/api/projects",
        json={"name": "Bridge Test Project", "building_type": "office"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# Integration GET/PUT
# ---------------------------------------------------------------------------


def test_get_integration_returns_not_configured_initially(client):
    pid = _make_project(client)
    r = client.get(f"/api/projects/{pid}/integration")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False
    assert body["integration"] is None


def test_put_integration_saves_and_masks_password(client):
    pid = _make_project(client)
    r = client.put(
        f"/api/projects/{pid}/integration",
        json={
            "host": "ankara.sensgreen.com",
            "port": 1881,
            "username": "user-x",
            "password": "secret",
            "topic": "sensor/data/925255",
            "error_topic": "sensor/error/925255",
            "tls": False,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True
    assert body["integration"]["host"] == "ankara.sensgreen.com"
    assert body["integration"]["password"] == "********"

    # And the event log gained an integration entry.
    r2 = client.get(f"/api/projects/{pid}/events")
    events = r2.json()["events"]
    assert any(e["kind"] == "integration" for e in events)


def test_put_integration_rejects_missing_host(client):
    pid = _make_project(client)
    r = client.put(
        f"/api/projects/{pid}/integration",
        json={"host": "", "topic": "sensor/data/1"},
    )
    # pydantic min_length=1 → 422
    assert r.status_code == 422


def test_put_integration_unknown_project_404s(client):
    r = client.put(
        "/api/projects/does-not-exist/integration",
        json={"host": "h", "topic": "t"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Bridge test
# ---------------------------------------------------------------------------


def test_bridge_test_dry_run_against_dubai_config(client):
    pid = _make_project(client)
    client.put(
        f"/api/projects/{pid}/integration",
        json={
            "host": "ankara.sensgreen.com",
            "port": 1881,
            "topic": "sensor/data/925255",
        },
    )
    r = client.post(
        f"/api/projects/{pid}/bridge-test",
        json={
            "config_path": "configs/dubai_office.yaml",
            "dry_run": True,
            "error_listen_seconds": 0,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["published_count"] == len(body["devices"])
    assert body["failed_count"] == 0
    assert body["all_ok"] is True

    # Event recorded.
    events = client.get(f"/api/projects/{pid}/events").json()["events"]
    kinds = [e["kind"] for e in events]
    assert "bridge_test" in kinds


def test_bridge_test_without_integration_and_not_dry_run_400s(client):
    pid = _make_project(client)
    r = client.post(
        f"/api/projects/{pid}/bridge-test",
        json={"config_path": "configs/dubai_office.yaml", "dry_run": False},
    )
    assert r.status_code == 400
    assert "integration" in r.json()["detail"].lower()


def test_bridge_test_bad_config_path_400s(client):
    pid = _make_project(client)
    client.put(
        f"/api/projects/{pid}/integration",
        json={"host": "h", "topic": "t"},
    )
    r = client.post(
        f"/api/projects/{pid}/bridge-test",
        json={"config_path": "configs/does_not_exist.yaml", "dry_run": True},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Events listing
# ---------------------------------------------------------------------------


def test_events_endpoint_filters_by_kind(client):
    pid = _make_project(client)
    client.evt.record(pid, kind="bridge_test", status="succeeded", summary="a")
    client.evt.record(pid, kind="historical_run", status="succeeded", summary="b")
    r = client.get(f"/api/projects/{pid}/events", params={"kind": "bridge_test"})
    assert r.status_code == 200
    events = r.json()["events"]
    assert len(events) == 1
    assert events[0]["kind"] == "bridge_test"


def test_events_endpoint_404s_for_unknown_project(client):
    r = client.get("/api/projects/missing-xyz/events")
    assert r.status_code == 404


def test_project_detail_html_renders_all_four_tabs(client):
    pid = _make_project(client)
    r = client.get(f"/projects/{pid}")
    assert r.status_code == 200
    body = r.text
    for label in ("Overview", "Integration", "Bridge Test", "Events"):
        assert label in body
    assert "integration-form" in body
    assert "bridge-form" in body
    assert "events-table-wrap" in body


def test_events_html_fragment_endpoint(client):
    pid = _make_project(client)
    r = client.get(f"/projects/{pid}/events")
    assert r.status_code == 200
    # Empty state copy.
    assert "No events yet" in r.text

    client.evt.record(pid, kind="bridge_test", status="succeeded", summary="hello")
    r2 = client.get(f"/projects/{pid}/events")
    assert "hello" in r2.text
    assert "bridge_test" in r2.text


def test_events_html_fragment_404s_for_unknown(client):
    r = client.get("/projects/no-such-project/events")
    assert r.status_code == 404
