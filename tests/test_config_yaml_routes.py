"""Tests for the raw-YAML editor endpoints.

These guard the path used by the in-app YAML editor: GET returns text
(default skeleton when no file exists yet), PUT validates with the same
``load_config`` pipeline as the structured editor and surfaces parse +
loader errors as HTTP 400.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
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
        json={"name": "YAML P", "building_type": "office"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_get_yaml_returns_default_skeleton_when_no_file(client):
    pid = _make_project(client)
    r = client.get(f"/api/projects/{pid}/config/yaml")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    parsed = yaml.safe_load(r.text)
    assert parsed["building"]["zones"][0]["id"] == "zone-default"
    assert parsed["devices"] == []


def test_put_yaml_roundtrip(client):
    pid = _make_project(client)
    text = client.get(f"/api/projects/{pid}/config/yaml").text
    cfg = yaml.safe_load(text)
    cfg["building"]["name"] = "Edited via YAML"
    cfg["devices"] = [
        {
            "id": "dev-1",
            "name": "Dev 1",
            "type": "iaq",
            "device_eui": "AA" * 8,
            "zone_id": "zone-default",
            "interval_sec": 60,
        }
    ]
    new_text = yaml.safe_dump(cfg, sort_keys=False)
    r = client.put(f"/api/projects/{pid}/config/yaml", json={"yaml": new_text})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["config"]["building"]["name"] == "Edited via YAML"
    assert body["meta"]["valid"] is True
    # Subsequent GET reflects the saved file.
    r2 = client.get(f"/api/projects/{pid}/config/yaml")
    assert "Edited via YAML" in r2.text


def test_put_yaml_rejects_syntax_error(client):
    pid = _make_project(client)
    bad = "building: {name: 'unterminated\ndevices: []\n"
    r = client.put(f"/api/projects/{pid}/config/yaml", json={"yaml": bad})
    assert r.status_code == 400
    assert "YAML" in r.json()["detail"] or "parse" in r.json()["detail"].lower()


def test_put_yaml_rejects_non_mapping(client):
    pid = _make_project(client)
    r = client.put(
        f"/api/projects/{pid}/config/yaml", json={"yaml": "- just\n- a\n- list\n"}
    )
    assert r.status_code == 400
    assert "mapping" in r.json()["detail"].lower()


def test_put_yaml_rejects_loader_violation(client):
    pid = _make_project(client)
    # Parses fine, but load_config will reject (missing building/devices).
    r = client.put(
        f"/api/projects/{pid}/config/yaml", json={"yaml": "foo: bar\n"}
    )
    assert r.status_code == 400


def test_yaml_routes_404_for_unknown_project(client):
    r = client.get("/api/projects/does-not-exist/config/yaml")
    assert r.status_code == 404
    r2 = client.put(
        "/api/projects/does-not-exist/config/yaml", json={"yaml": "a: 1\n"}
    )
    assert r2.status_code == 404
