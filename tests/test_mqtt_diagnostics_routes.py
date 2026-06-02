"""Tests for the P2 MQTT diagnostics + preview + mapping endpoints.

Covers:

* ``POST /api/projects/{id}/preview-payload``
* ``POST /api/projects/{id}/integration/test-connection``
* ``POST /api/projects/{id}/integration/test-credentials``
* ``POST /api/projects/{id}/integration/publish-test``
* ``GET  /api/projects/{id}/mapping``

The central invariant is **no plaintext password ever leaves the
process** — in any response body, in any event details payload, in any
log line we control. The redaction tests assert this directly.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.services.event_service import EventService
from api.services.project_service import ProjectService
import api.routes.bridge as bridge_module
import api.routes.config as config_module
import api.routes.projects as projects_module
import api.routes.web as web_module
import api.services.event_service as event_module
from simulator.services import mqtt_diagnostics


# ---------------------------------------------------------------------------
# Test client + fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    proj_svc = ProjectService(data_dir=tmp_path)
    evt_svc = EventService(data_dir=tmp_path)
    monkeypatch.setattr(bridge_module, "project_service", proj_svc)
    monkeypatch.setattr(bridge_module, "event_service", evt_svc)
    monkeypatch.setattr(config_module, "project_service", proj_svc)
    monkeypatch.setattr(projects_module, "project_service", proj_svc)
    monkeypatch.setattr(projects_module, "event_service", evt_svc)
    monkeypatch.setattr(web_module, "project_service", proj_svc)
    monkeypatch.setattr(web_module, "event_service", evt_svc)
    monkeypatch.setattr(event_module, "event_service", evt_svc)
    with TestClient(app) as c:
        c.svc = proj_svc  # type: ignore[attr-defined]
        c.evt = evt_svc  # type: ignore[attr-defined]
        yield c


def _make_project_with_device(client: TestClient) -> tuple[str, str]:
    r = client.post(
        "/api/projects",
        json={"name": "Diag Project", "building_type": "office"},
    )
    pid = r.json()["id"]
    eui = "aabbccddeeff0011"
    r2 = client.post(
        f"/api/projects/{pid}/devices",
        json={
            "device_eui": eui,
            "name": "Lobby IAQ",
            "type": "iaq",
            "zone_id": "zone-default",
        },
    )
    assert r2.status_code in (200, 201), r2.text
    return pid, eui


def _save_integration(client: TestClient, pid: str, *, password: str = "s3cret") -> None:
    r = client.put(
        f"/api/projects/{pid}/integration",
        json={
            "host": "ankara.sensgreen.com",
            "port": 1881,
            "username": "user-x",
            "password": password,
            "topic": "sensor/data/925255",
            "tls": False,
        },
    )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Redaction unit tests
# ---------------------------------------------------------------------------


def test_redact_integration_masks_password():
    out = mqtt_diagnostics.redact_integration({
        "host": "h", "port": 1881, "username": "u",
        "password": "supersecret", "topic": "t",
    })
    assert out["password"] == "********"
    assert "supersecret" not in str(out)


def test_redact_integration_drops_unknown_fields():
    out = mqtt_diagnostics.redact_integration({
        "host": "h", "topic": "t",
        "password": "x",
        "secret_extra": "must-not-leak",
    })
    assert "secret_extra" not in out
    assert "must-not-leak" not in str(out)


def test_redact_integration_passwordless_is_none():
    out = mqtt_diagnostics.redact_integration({"host": "h", "topic": "t"})
    assert out["password"] is None


# ---------------------------------------------------------------------------
# Payload preview
# ---------------------------------------------------------------------------


def test_preview_payload_returns_sensgreen_envelope(client):
    pid, eui = _make_project_with_device(client)
    r = client.post(f"/api/projects/{pid}/preview-payload", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["device"]["device_eui"] == eui
    assert body["payload"]["deviceEui"] == eui
    assert isinstance(body["payload"]["timestamp"], int)
    assert body["payload"]["data"]  # non-empty mapped dict
    assert body["topic"].startswith("sensgreen/")  # default template — no integration yet
    assert body["used_integration_topic"] is False


def test_preview_payload_uses_saved_topic_when_integration_exists(client):
    pid, eui = _make_project_with_device(client)
    _save_integration(client, pid)
    r = client.post(f"/api/projects/{pid}/preview-payload", json={})
    body = r.json()
    assert body["topic"] == "sensor/data/925255"
    assert body["used_integration_topic"] is True


def test_preview_payload_400s_when_no_devices(client):
    r = client.post(
        "/api/projects",
        json={"name": "Empty", "building_type": "office"},
    )
    pid = r.json()["id"]
    r2 = client.post(f"/api/projects/{pid}/preview-payload", json={})
    assert r2.status_code == 400
    assert "device" in r2.json()["detail"].lower()


def test_preview_payload_404s_for_unknown_device_eui(client):
    pid, _eui = _make_project_with_device(client)
    r = client.post(
        f"/api/projects/{pid}/preview-payload",
        json={"device_eui": "deadbeefdeadbeef"},
    )
    assert r.status_code == 404


def test_preview_payload_never_contains_password_in_body(client):
    pid, _eui = _make_project_with_device(client)
    _save_integration(client, pid, password="topsecretpw123")
    r = client.post(f"/api/projects/{pid}/preview-payload", json={})
    assert "topsecretpw123" not in r.text


# ---------------------------------------------------------------------------
# test-connection
# ---------------------------------------------------------------------------


class _FakeSocket:
    """Stand-in for a socket that records calls and never touches the network."""

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.connected_to: tuple[str, int] | None = None
        self.closed = False

    def settimeout(self, _t: float) -> None: ...
    def connect(self, addr):
        self.connected_to = addr
        if self.fail:
            raise OSError("connection refused (fake)")
    def close(self):
        self.closed = True


def test_test_connection_success_via_fake_socket(monkeypatch):
    sock = _FakeSocket()
    result = mqtt_diagnostics.test_connection(
        {"host": "h", "port": 1881, "tls": False},
        socket_factory=lambda: sock,
    )
    assert result["ok"] is True
    assert sock.connected_to == ("h", 1881)
    assert sock.closed is True
    assert "reachable" in result["message"]


def test_test_connection_failure_via_fake_socket():
    result = mqtt_diagnostics.test_connection(
        {"host": "h", "port": 1881, "tls": False},
        socket_factory=lambda: _FakeSocket(fail=True),
    )
    assert result["ok"] is False
    assert "connection refused" in result["message"].lower()


def test_test_connection_route_records_event_without_password(client, monkeypatch):
    pid, _eui = _make_project_with_device(client)
    _save_integration(client, pid, password="leakme")
    # Patch the socket factory globally inside the diagnostics module.
    monkeypatch.setattr(
        mqtt_diagnostics, "test_connection",
        lambda integration, **_kw: {
            "ok": True, "latency_ms": 12.3,
            "tls": False, "host": integration["host"], "port": integration["port"],
            "message": "reachable in 12 ms (plain TCP)",
        },
    )
    # The route imports the symbol — patch its binding too.
    monkeypatch.setattr(
        bridge_module.mqtt_diagnostics, "test_connection",
        mqtt_diagnostics.test_connection,
    )
    r = client.post(
        f"/api/projects/{pid}/integration/test-connection", json={}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert "leakme" not in r.text

    events = client.get(f"/api/projects/{pid}/events").json()["events"]
    test_events = [e for e in events
                   if e["details"].get("kind") == "test_connection"]
    assert test_events, "test-connection should record an event"
    serialized = str(test_events[0])
    assert "leakme" not in serialized
    assert "********" in serialized


def test_test_connection_400s_when_no_integration(client):
    pid, _eui = _make_project_with_device(client)
    r = client.post(
        f"/api/projects/{pid}/integration/test-connection", json={}
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# test-credentials
# ---------------------------------------------------------------------------


def test_test_credentials_route_records_event_without_password(client, monkeypatch):
    pid, _eui = _make_project_with_device(client)
    _save_integration(client, pid, password="cred-leak")

    monkeypatch.setattr(
        bridge_module.mqtt_diagnostics, "test_credentials",
        lambda integration, **_kw: {
            "ok": True, "rc": 0, "latency_ms": 30.0,
            "username": integration.get("username"),
            "message": "authenticated (rc=0, accepted)",
        },
    )
    r = client.post(
        f"/api/projects/{pid}/integration/test-credentials", json={}
    )
    assert r.status_code == 200, r.text
    assert "cred-leak" not in r.text

    events = client.get(f"/api/projects/{pid}/events").json()["events"]
    cred_evt = [e for e in events
                if e["details"].get("kind") == "test_credentials"]
    assert cred_evt
    assert "cred-leak" not in str(cred_evt[0])


def test_test_credentials_override_payload_uses_provided_password(client, monkeypatch):
    """The 'try before save' path: override.password is honoured but never echoed."""
    pid, _eui = _make_project_with_device(client)
    _save_integration(client, pid, password="saved-pw")

    captured: dict[str, Any] = {}

    def _fake(integration, **_kw):
        captured.update(integration)
        return {
            "ok": True, "rc": 0, "latency_ms": 5.0,
            "username": integration.get("username"),
            "message": "ok",
        }

    monkeypatch.setattr(
        bridge_module.mqtt_diagnostics, "test_credentials", _fake
    )
    r = client.post(
        f"/api/projects/{pid}/integration/test-credentials",
        json={"integration": {"password": "override-pw", "username": "user-y"}},
    )
    assert r.status_code == 200
    assert captured["password"] == "override-pw"
    assert captured["username"] == "user-y"
    # Host/topic should be merged from the saved record.
    assert captured["host"] == "ankara.sensgreen.com"
    # Response must not echo the password.
    assert "override-pw" not in r.text


# ---------------------------------------------------------------------------
# publish-test
# ---------------------------------------------------------------------------


def test_publish_test_route_strips_payload_from_event(client, monkeypatch):
    pid, eui = _make_project_with_device(client)
    _save_integration(client, pid, password="pub-leak")

    def _fake(integration, *, device_eui, sensor_type, **_kw):
        return {
            "ok": True,
            "message": "published 1 payload in 42 ms",
            "topic": "sensor/data/925255",
            "payload": {"deviceEui": device_eui, "timestamp": 0,
                        "data": {"temperature": 22.0}},
            "latency_ms": 42.0,
        }

    monkeypatch.setattr(
        bridge_module.mqtt_diagnostics, "publish_test", _fake
    )
    r = client.post(
        f"/api/projects/{pid}/integration/publish-test", json={}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["topic"] == "sensor/data/925255"
    # API may echo the payload to the caller — that is fine.
    assert "pub-leak" not in r.text

    events = client.get(f"/api/projects/{pid}/events").json()["events"]
    pub_evts = [e for e in events
                if e["details"].get("kind") == "publish_test"]
    assert pub_evts
    details = pub_evts[0]["details"]
    # Event details must NOT include the raw payload body
    # (it can be large; reconstruct from device config instead).
    assert "payload" not in details["result"]
    assert "pub-leak" not in str(pub_evts[0])


# ---------------------------------------------------------------------------
# Sensgreen mapping endpoint
# ---------------------------------------------------------------------------


def test_mapping_endpoint_lists_devices_with_sensgreen_keys(client):
    pid, eui = _make_project_with_device(client)
    r = client.get(f"/api/projects/{pid}/mapping")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["device_count"] == 1
    row = body["rows"][0]
    assert row["device_eui"] == eui
    assert row["sensor_type"] == "iaq"
    assert row["supported"] is True
    assert "co2" in row["sensgreen_metric_keys"]
    assert "temperature" in row["sensgreen_metric_keys"]


def test_mapping_endpoint_empty_for_project_without_devices(client):
    r = client.post(
        "/api/projects",
        json={"name": "Empty", "building_type": "office"},
    )
    pid = r.json()["id"]
    r2 = client.get(f"/api/projects/{pid}/mapping")
    assert r2.status_code == 200
    assert r2.json()["device_count"] == 0
    assert r2.json()["rows"] == []


def test_mapping_endpoint_404s_for_unknown_project(client):
    r = client.get("/api/projects/no-such-thing/mapping")
    assert r.status_code == 404
