"""Tests for /api/projects/{id}/live/* routes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.services.event_service import EventService
from api.services.project_service import ProjectService
from simulator.services.live_session import LiveRunController
import api.routes.live as live_module
import api.routes.projects as projects_module
import api.routes.web as web_module
import api.services.event_service as event_module


CONFIG_PATH = Path(__file__).resolve().parent.parent / "configs" / "diagnostic_small_office.yaml"


# ---------------------------------------------------------------------------
# Silent fake publisher + factory shim
# ---------------------------------------------------------------------------


@dataclass
class _FakeResult:
    ok: bool = True
    rc: int = 0


class _FakePublisher:
    host = "fake"
    port = 0
    topic_template = "t/fake"
    dry_run = True

    def __init__(self):
        self.connected = False
        self.sent: list[Any] = []

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.connected = False

    def publish(self, payload):
        self.sent.append(payload)
        return _FakeResult(True, 0)


class _FakePublisherClass:
    """Mimics SensgreenMqttPublisher.from_config / from_integration classmethods."""

    @staticmethod
    def from_config(*_args, **_kwargs):
        return _FakePublisher()

    @staticmethod
    def from_integration(*_args, **_kwargs):
        return _FakePublisher()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    proj_svc = ProjectService(data_dir=tmp_path)
    evt_svc = EventService(data_dir=tmp_path)
    ctrl = LiveRunController()

    monkeypatch.setattr(projects_module, "project_service", proj_svc)
    monkeypatch.setattr(projects_module, "event_service", evt_svc)
    monkeypatch.setattr(live_module, "project_service", proj_svc)
    monkeypatch.setattr(live_module, "event_service", evt_svc)
    monkeypatch.setattr(live_module, "live_controller", ctrl)
    # Replace the publisher factory so the worker thread never prints.
    monkeypatch.setattr(live_module, "SensgreenMqttPublisher", _FakePublisherClass)
    monkeypatch.setattr(web_module, "project_service", proj_svc)
    monkeypatch.setattr(web_module, "event_service", evt_svc)
    monkeypatch.setattr(web_module, "live_controller", ctrl)
    monkeypatch.setattr(event_module, "event_service", evt_svc)

    try:
        with TestClient(app) as c:
            c.ctrl = ctrl  # type: ignore[attr-defined]
            yield c
    finally:
        ctrl.stop_all()


def _make_project(client: TestClient) -> str:
    r = client.post(
        "/api/projects",
        json={"name": "Live Test", "building_type": "office"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# status / stop on a never-started session
# ---------------------------------------------------------------------------


def test_status_on_unstarted_returns_stopped(client):
    pid = _make_project(client)
    r = client.get(f"/api/projects/{pid}/live/status")
    assert r.status_code == 200
    assert r.json()["state"] == "stopped"


def test_stop_on_unstarted_returns_stopped(client):
    pid = _make_project(client)
    r = client.post(f"/api/projects/{pid}/live/stop")
    assert r.status_code == 200
    assert r.json()["state"] == "stopped"


# ---------------------------------------------------------------------------
# start validation
# ---------------------------------------------------------------------------


def test_start_requires_integration_unless_dry_run(client):
    pid = _make_project(client)
    r = client.post(
        f"/api/projects/{pid}/live/start",
        json={"config_path": str(CONFIG_PATH), "dry_run": False},
    )
    assert r.status_code == 400
    assert "integration" in r.json()["detail"].lower()


def test_start_with_missing_config_returns_400(client):
    pid = _make_project(client)
    r = client.post(
        f"/api/projects/{pid}/live/start",
        json={"config_path": "/tmp/does-not-exist.yaml", "dry_run": True},
    )
    assert r.status_code == 400


def test_start_unknown_project_404(client):
    r = client.post(
        "/api/projects/nope/live/start",
        json={"config_path": str(CONFIG_PATH), "dry_run": True},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# happy path: dry-run start -> status -> stop
# ---------------------------------------------------------------------------


def test_dry_run_start_status_stop(client):
    pid = _make_project(client)
    r = client.post(
        f"/api/projects/{pid}/live/start",
        json={"config_path": str(CONFIG_PATH), "dry_run": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] in ("starting", "running")
    assert body["dry_run"] is True

    r = client.get(f"/api/projects/{pid}/live/status")
    assert r.status_code == 200
    assert r.json()["state"] in ("starting", "running")

    r = client.post(f"/api/projects/{pid}/live/stop")
    assert r.status_code == 200
    assert r.json()["state"] in ("stopped", "succeeded", "failed")


# ---------------------------------------------------------------------------
# SSE stream
# ---------------------------------------------------------------------------


# Note: we don't open a streaming GET against /live/stream with TestClient.
# Starlette's sync TestClient transport keeps the streaming generator alive
# even after the context manager exits, because the heartbeat coroutine is
# parked on ``await asyncio.sleep(15)``. The route itself is exercised in
# the browser by the dashboard's ``EventSource`` subscriber.


def test_stream_unknown_project_404(client):
    r = client.get("/api/projects/nope/live/stream")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Live tab renders on the project page
# ---------------------------------------------------------------------------


def test_project_page_renders_live_tab(client):
    pid = _make_project(client)
    r = client.get(f"/projects/{pid}")
    assert r.status_code == 200
    assert "data-tab-pane=\"live\"" in r.text
    assert "Live simulation" in r.text
