"""End-to-end tests for the dashboard-driven simulator config endpoints."""

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
        json={"name": "Cfg P", "building_type": "office"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# Sensor catalog
# ---------------------------------------------------------------------------


def test_sensor_types_endpoint(client):
    r = client.get("/api/sensor-types")
    assert r.status_code == 200
    body = r.json()
    ids = {s["id"] for s in body["sensor_types"]}
    assert {"iaq", "energy_meter", "entry_exit_counter"} <= ids


# ---------------------------------------------------------------------------
# Config skeleton + roundtrip
# ---------------------------------------------------------------------------


def test_get_config_returns_default_skeleton(client):
    pid = _make_project(client)
    r = client.get(f"/api/projects/{pid}/config")
    assert r.status_code == 200
    body = r.json()
    cfg = body["config"]
    assert cfg["building"]["zones"][0]["id"] == "zone-default"
    assert cfg["devices"] == []
    # No managed file exists yet, so meta says it's not valid.
    assert body["meta"]["exists"] is False
    assert body["meta"]["valid"] is False


def test_get_config_unknown_project_404(client):
    r = client.get("/api/projects/does-not-exist/config")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Zone CRUD
# ---------------------------------------------------------------------------


def test_add_and_remove_zone(client):
    pid = _make_project(client)
    r = client.post(
        f"/api/projects/{pid}/zones",
        json={"id": "zone-2f", "name": "2nd floor", "area_m2": 120, "capacity": 30},
    )
    assert r.status_code == 201, r.text
    cfg = r.json()["config"]
    assert {z["id"] for z in cfg["building"]["zones"]} == {"zone-default", "zone-2f"}

    # Duplicate id -> 400
    r = client.post(
        f"/api/projects/{pid}/zones",
        json={"id": "zone-2f", "name": "dup"},
    )
    assert r.status_code == 400

    # Remove succeeds
    r = client.delete(f"/api/projects/{pid}/zones/zone-2f")
    assert r.status_code == 200
    cfg = r.json()["config"]
    assert {z["id"] for z in cfg["building"]["zones"]} == {"zone-default"}

    # Removing unknown zone -> 404
    r = client.delete(f"/api/projects/{pid}/zones/nope")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Device CRUD
# ---------------------------------------------------------------------------


def _add_iaq(client: TestClient, pid: str, eui: str = "fe00000000000001") -> dict:
    r = client.post(
        f"/api/projects/{pid}/devices",
        json={
            "device_eui": eui,
            "name": "Lobby IAQ",
            "type": "iaq",
            "zone_id": "zone-default",
            "metadata": {
                "base_temperature_c": 22.0,
                "base_humidity_pct": 45.0,
                "interval_seconds": 60,
            },
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_add_device_validates_and_persists(client, tmp_path):
    pid = _make_project(client)
    body = _add_iaq(client, pid)
    cfg = body["config"]
    assert len(cfg["devices"]) == 1
    assert cfg["devices"][0]["device_eui"] == "fe00000000000001"
    # Managed config file now exists on disk
    assert body["meta"]["exists"] is True
    assert body["meta"]["valid"] is True
    assert body["meta"]["device_count"] == 1


def test_add_device_rejects_unknown_type(client):
    pid = _make_project(client)
    r = client.post(
        f"/api/projects/{pid}/devices",
        json={
            "device_eui": "fe0000000000abcd",
            "name": "Bad",
            "type": "not-a-real-sensor",
            "zone_id": "zone-default",
        },
    )
    assert r.status_code == 400
    assert "unknown sensor type" in r.json()["detail"]


def test_add_device_rejects_duplicate_eui(client):
    pid = _make_project(client)
    _add_iaq(client, pid)
    r = client.post(
        f"/api/projects/{pid}/devices",
        json={
            "device_eui": "FE00:0000:0000:0001",  # same EUI, different case/format
            "name": "Dup",
            "type": "iaq",
            "zone_id": "zone-default",
            "metadata": {"base_temperature_c": 22, "base_humidity_pct": 45},
        },
    )
    assert r.status_code == 400
    assert "already exists" in r.json()["detail"]


def test_add_device_rejects_bad_eui(client):
    pid = _make_project(client)
    r = client.post(
        f"/api/projects/{pid}/devices",
        json={
            "device_eui": "not-hex",
            "name": "X",
            "type": "iaq",
            "zone_id": "zone-default",
            "metadata": {"base_temperature_c": 22, "base_humidity_pct": 45},
        },
    )
    assert r.status_code == 400


def test_add_device_rejects_unknown_zone(client):
    pid = _make_project(client)
    r = client.post(
        f"/api/projects/{pid}/devices",
        json={
            "device_eui": "fe0000000000feed",
            "name": "Orphan",
            "type": "iaq",
            "zone_id": "no-such-zone",
            "metadata": {"base_temperature_c": 22, "base_humidity_pct": 45},
        },
    )
    # load_config validation should catch this and return 400.
    assert r.status_code == 400


def test_update_device(client):
    pid = _make_project(client)
    _add_iaq(client, pid)
    r = client.put(
        f"/api/projects/{pid}/devices/fe00000000000001",
        json={"name": "Lobby IAQ (renamed)"},
    )
    assert r.status_code == 200, r.text
    cfg = r.json()["config"]
    assert cfg["devices"][0]["name"] == "Lobby IAQ (renamed)"


def test_update_device_404(client):
    pid = _make_project(client)
    r = client.put(
        f"/api/projects/{pid}/devices/fe000000000000aa",
        json={"name": "Ghost"},
    )
    assert r.status_code == 404


def test_remove_device_allows_empty_list(client):
    pid = _make_project(client)
    _add_iaq(client, pid)
    r = client.delete(f"/api/projects/{pid}/devices/fe00000000000001")
    assert r.status_code == 200
    cfg = r.json()["config"]
    assert cfg["devices"] == []


def test_cannot_remove_zone_with_devices(client):
    pid = _make_project(client)
    _add_iaq(client, pid)
    r = client.delete(f"/api/projects/{pid}/zones/zone-default")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# EUI generator
# ---------------------------------------------------------------------------


def test_generate_eui_endpoint(client):
    pid = _make_project(client)
    r = client.post(
        f"/api/projects/{pid}/devices/generate-eui",
        json={"name": "Lobby IAQ"},
    )
    assert r.status_code == 200
    eui = r.json()["device_eui"]
    assert len(eui) == 16
    assert all(c in "0123456789abcdef" for c in eui)
    # Deterministic: same input -> same EUI
    r2 = client.post(
        f"/api/projects/{pid}/devices/generate-eui",
        json={"name": "Lobby IAQ"},
    )
    assert r2.json()["device_eui"] == eui


# ---------------------------------------------------------------------------
# Live / Bridge fallback to managed config
# ---------------------------------------------------------------------------


def test_live_start_falls_back_to_managed_config(client):
    pid = _make_project(client)
    _add_iaq(client, pid)
    # Set a fake integration so live/start doesn't 400 for that reason.
    r = client.put(
        f"/api/projects/{pid}/integration",
        json={
            "host": "broker.example.com",
            "port": 8883,
            "username": "u",
            "password": "p",
            "topic": "t/topic",
            "tls": True,
        },
    )
    assert r.status_code == 200, r.text

    # No config_path: should resolve to the managed YAML and succeed
    # (dry_run is fine; live session is async).
    r = client.post(
        f"/api/projects/{pid}/live/start",
        json={"dry_run": True},
    )
    # 200 OK or 409 if a session is already running, but here it's the
    # first call so 200 is expected.
    assert r.status_code in (200, 409), r.text


def test_live_start_400_when_no_managed_config_and_no_path(client):
    pid = _make_project(client)
    r = client.post(
        f"/api/projects/{pid}/live/start",
        json={"dry_run": True},
    )
    assert r.status_code == 400
    assert "managed config" in r.json()["detail"]
