"""Tests for auto-provisioning devices from monitoring profiles."""

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
        json={"name": "Autoprov", "building_type": "office"},
    )
    assert r.status_code == 201
    return r.json()["id"]


# ---------------------------------------------------------------------------
# Service-level
# ---------------------------------------------------------------------------


def test_auto_provision_dry_run_plans_devices_per_zone(client):
    c, svc = client
    pid = _make_project(c)
    # Two zones, no devices yet.
    c.post(
        f"/api/projects/{pid}/zones",
        json={"id": "mtg-1", "name": "Meeting Room 1", "room_type": "meeting_room"},
    )
    c.post(
        f"/api/projects/{pid}/zones",
        json={"id": "lobby-1", "name": "Lobby", "room_type": "lobby"},
    )

    result = svc.auto_provision_devices(pid, dry_run=True)
    assert result["dry_run"] is True
    assert "config" not in result
    by_zone = {}
    for d in result["to_add"]:
        by_zone.setdefault(d["zone_id"], set()).add(d["sensor_type"])
    # Both planned zones must show up.
    assert "mtg-1" in by_zone
    assert "lobby-1" in by_zone
    # Disk should still have no devices (this was a dry run).
    cfg = svc.get_config(pid)
    assert (cfg.get("devices") or []) == [] or all(
        d.get("zone_id") not in {"mtg-1", "lobby-1"}
        for d in cfg.get("devices") or []
    )


def test_auto_provision_apply_persists_devices(client):
    c, svc = client
    pid = _make_project(c)
    c.post(
        f"/api/projects/{pid}/zones",
        json={"id": "mtg-1", "name": "Meeting 1", "room_type": "meeting_room"},
    )
    result = svc.auto_provision_devices(pid, dry_run=False)
    assert "config" in result
    assert result["to_add"], "expected at least one device staged"
    cfg = svc.get_config(pid)
    devs = cfg.get("devices") or []
    euis = {d["device_eui"] for d in devs}
    for entry in result["to_add"]:
        assert entry["device_eui"] in euis
        # Default metadata from sensor catalog should be attached.
        match = next(d for d in devs if d["device_eui"] == entry["device_eui"])
        assert match["metadata"].get("interval_seconds")


def test_auto_provision_skips_existing_sensor_types(client):
    c, svc = client
    pid = _make_project(c)
    c.post(
        f"/api/projects/{pid}/zones",
        json={"id": "mtg-1", "name": "Meeting 1", "room_type": "meeting_room"},
    )
    # First pass adds everything.
    first = svc.auto_provision_devices(pid, dry_run=False)
    n_first = len(first["to_add"])
    assert n_first >= 1

    # Second pass: no overwrite → everything skipped.
    second = svc.auto_provision_devices(pid, dry_run=True)
    assert second["to_add"] == []
    assert all(s["reason"] == "already present" for s in second["skipped"] if s["sensor_type"])


def test_auto_provision_overwrite_re_adds(client):
    c, svc = client
    pid = _make_project(c)
    c.post(
        f"/api/projects/{pid}/zones",
        json={"id": "mtg-1", "name": "Meeting 1", "room_type": "meeting_room"},
    )
    svc.auto_provision_devices(pid, dry_run=False)
    again = svc.auto_provision_devices(pid, dry_run=True, overwrite=True)
    assert again["to_add"], "overwrite should plan new devices"


def test_auto_provision_no_zones_raises(client):
    c, svc = client
    # Make a project but wipe its default zone via raw set.
    pid = _make_project(c)
    cfg = svc.get_config(pid)
    cfg["building"]["zones"] = []
    # Bypass validation since loader rejects empty zones lists.
    svc.set_config(pid, cfg, validate=False)
    with pytest.raises(ValueError, match="no zones"):
        svc.auto_provision_devices(pid, dry_run=True)


# ---------------------------------------------------------------------------
# HTTP-level
# ---------------------------------------------------------------------------


def test_http_auto_provision_dry_run(client):
    c, _ = client
    pid = _make_project(c)
    c.post(
        f"/api/projects/{pid}/zones",
        json={"id": "mtg-1", "name": "Meeting 1", "room_type": "meeting_room"},
    )
    r = c.post(
        f"/api/projects/{pid}/devices/auto-provision",
        json={"dry_run": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert "to_add" in body
    assert "config" not in body


def test_http_auto_provision_apply(client):
    c, _ = client
    pid = _make_project(c)
    c.post(
        f"/api/projects/{pid}/zones",
        json={"id": "mtg-1", "name": "Meeting 1", "room_type": "meeting_room"},
    )
    r = c.post(
        f"/api/projects/{pid}/devices/auto-provision",
        json={"dry_run": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "config" in body
    assert "meta" in body
    # Sanity-check: the new devices are present in the returned cfg.
    devs = body["config"].get("devices") or []
    euis = {d["device_eui"] for d in devs}
    for entry in body["to_add"]:
        assert entry["device_eui"] in euis
