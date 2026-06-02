"""Sensgreen MQTT bridge endpoints: integration settings + bridge test.

The dashboard's *Integration* panel posts here to save broker credentials
(stored via :class:`ProjectService.set_integration`), and to trigger the
one-shot :class:`BridgeTester` against the real broker.

Every successful or failed bridge test is recorded in the per-project
event log so the user can audit attempts from the Events tab.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from api.services.event_service import event_service
from api.services.project_service import project_service
from simulator.config_loader import ConfigError, load_config
from simulator.integrations.sensgreen_mqtt_publisher import SensgreenMqttPublisher
from simulator.services.bridge_tester import BridgeTester
from simulator.services import mqtt_diagnostics

router = APIRouter()


class PreviewPayloadRequest(BaseModel):
    # Optional — when omitted, the first device in the managed config is used.
    device_eui: str | None = Field(None, max_length=64)


class MqttTestRequest(BaseModel):
    # Optional override of the saved integration. When ``None`` the saved
    # secrets are used. Useful for "try before save" in the UI.
    integration: dict[str, Any] | None = None
    # publish_test only: which device's EUI / sensor type to simulate.
    device_eui: str | None = Field(None, max_length=64)


def _resolve_integration(
    project_id: str, override: dict[str, Any] | None
) -> dict[str, Any]:
    """Return the integration dict to test, merging any UI override.

    If ``override`` is provided and contains a non-empty ``password``,
    that password is used. Otherwise the saved password (if any) is
    spliced in so "test without retyping" works for already-saved
    integrations.
    """
    saved = project_service.get_integration(project_id) or {}
    if not override:
        if not saved:
            raise HTTPException(
                status_code=400,
                detail="No integration saved for this project — fill the form first.",
            )
        return saved
    merged = dict(saved)
    for key, value in override.items():
        if value in (None, ""):
            continue
        merged[key] = value
    if not merged.get("password") and saved.get("password"):
        merged["password"] = saved["password"]
    if not merged.get("host") or not merged.get("topic"):
        raise HTTPException(
            status_code=400,
            detail="integration.host and integration.topic are required",
        )
    return merged


class IntegrationPayload(BaseModel):
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(1881, ge=1, le=65535)
    username: str | None = Field(None, max_length=255)
    password: str | None = Field(None, max_length=255)
    topic: str = Field(..., min_length=1, max_length=255)
    error_topic: str | None = Field(None, max_length=255)
    tls: bool = False
    client_id: str | None = Field(None, max_length=120)


class BridgeTestRequest(BaseModel):
    # Optional: when blank, the project's managed YAML
    # (``data/projects/<id>.config.yaml``) is used instead.
    config_path: str | None = Field(None, description="Path to the YAML config.")
    dry_run: bool = False
    error_listen_seconds: float = Field(2.0, ge=0.0, le=30.0)


# ---------------------------------------------------------------------------
# Integration CRUD
# ---------------------------------------------------------------------------


@router.get("/{project_id}/integration")
def get_integration(project_id: str) -> dict[str, Any]:
    if project_service.get(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    integration = project_service.get_integration(project_id)
    if integration is None:
        return {"project_id": project_id, "configured": False, "integration": None}
    # Mask the password so the UI never round-trips it.
    masked = dict(integration)
    if masked.get("password"):
        masked["password"] = "********"
    return {"project_id": project_id, "configured": True, "integration": masked}


@router.put("/{project_id}/integration", status_code=status.HTTP_200_OK)
def put_integration(project_id: str, payload: IntegrationPayload) -> dict[str, Any]:
    if project_service.get(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        saved = project_service.set_integration(project_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    event_service.record(
        project_id,
        kind="integration",
        status="info",
        summary=f"Integration saved → {saved['host']}:{saved['port']} {saved['topic']}",
        details={"host": saved["host"], "port": saved["port"], "topic": saved["topic"]},
    )

    masked = dict(saved)
    if masked.get("password"):
        masked["password"] = "********"
    return {"project_id": project_id, "configured": True, "integration": masked}


# ---------------------------------------------------------------------------
# Bridge test
# ---------------------------------------------------------------------------


@router.post("/{project_id}/bridge-test")
def run_bridge_test(project_id: str, payload: BridgeTestRequest) -> dict[str, Any]:
    """Publish one synthetic payload per device to the configured broker.

    Records the outcome as a ``bridge_test`` event regardless of result.
    """
    project = project_service.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    integration = project_service.get_integration(project_id)
    if integration is None and not payload.dry_run:
        raise HTTPException(
            status_code=400,
            detail="No Sensgreen MQTT integration configured for this project.",
        )

    config_path = payload.config_path
    if not config_path:
        managed = project_service.managed_config_path(project_id)
        if not managed.exists():
            raise HTTPException(
                status_code=400,
                detail=(
                    "no config_path supplied and no managed config exists yet — "
                    "add at least one device on the Devices tab first."
                ),
            )
        config_path = str(managed)

    try:
        cfg = load_config(config_path)
    except (ConfigError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=f"config error: {exc}") from exc

    if integration is not None:
        publisher = SensgreenMqttPublisher.from_integration(
            integration, dry_run=payload.dry_run
        )
    else:
        if cfg.outputs.mqtt is None:
            raise HTTPException(
                status_code=400,
                detail="dry_run requested but the YAML has no outputs.mqtt section.",
            )
        publisher = SensgreenMqttPublisher.from_config(
            cfg.outputs.mqtt, dry_run=payload.dry_run
        )

    tester = BridgeTester(
        cfg, publisher, error_listen_seconds=payload.error_listen_seconds
    )
    try:
        result = tester.run(project_id=project_id)
    except Exception as exc:
        event_service.record(
            project_id,
            kind="bridge_test",
            status="failed",
            summary=f"Bridge test raised: {exc}",
            details={"error": str(exc), "config_path": config_path},
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    event_service.record(
        project_id,
        kind="bridge_test",
        status="succeeded" if result.all_ok else "failed",
        summary=(
            f"Bridge test: {result.published_count}/{len(result.devices)} published, "
            f"{len(result.broker_errors)} broker errors"
        ),
        details={
            "host": result.host,
            "port": result.port,
            "topic": result.topic,
            "error_topic": result.error_topic,
            "dry_run": result.dry_run,
            "published_count": result.published_count,
            "failed_count": result.failed_count,
            "broker_error_count": len(result.broker_errors),
            "config_path": config_path,
        },
    )
    return result.to_dict()


# ---------------------------------------------------------------------------
# Payload preview + integration test buttons (P2)
# ---------------------------------------------------------------------------


def _first_device(project_id: str, device_eui: str | None) -> dict[str, Any]:
    """Return the chosen device dict from the managed config, or the first one."""
    cfg = project_service.get_config(project_id)
    devices = list(cfg.get("devices") or [])
    if not devices:
        raise HTTPException(
            status_code=400,
            detail="Project has no devices yet — add at least one on the Devices tab.",
        )
    if device_eui:
        for dev in devices:
            if str(dev.get("device_eui", "")).lower() == device_eui.lower():
                return dev
        raise HTTPException(
            status_code=404,
            detail=f"device_eui '{device_eui}' not found in project config",
        )
    return devices[0]


@router.post("/{project_id}/preview-payload")
def preview_payload(project_id: str, payload: PreviewPayloadRequest) -> dict[str, Any]:
    """Return a sample MQTT payload for one device, without publishing.

    The shape matches what Live mode / Bridge Test would emit, so users
    can verify the JSON contract before any network round-trip.
    """
    if project_service.get(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")

    device = _first_device(project_id, payload.device_eui)
    integration = project_service.get_integration(project_id)
    topic_template = (
        str(integration.get("topic", "")).strip()
        if integration
        else "sensgreen/{device_eui}"
    )

    try:
        sample = mqtt_diagnostics.build_sample_payload(
            device_eui=str(device.get("device_eui", "")),
            sensor_type=str(device.get("type", "")),
            topic_template=topic_template,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "project_id": project_id,
        "device": {
            "device_eui": device.get("device_eui"),
            "name": device.get("name"),
            "type": device.get("type"),
            "zone_id": device.get("zone_id"),
        },
        "used_integration_topic": integration is not None,
        **sample,
    }


@router.post("/{project_id}/integration/test-connection")
def integration_test_connection(
    project_id: str, payload: MqttTestRequest
) -> dict[str, Any]:
    """Open a raw TCP/TLS connection to the broker; no MQTT CONNECT."""
    if project_service.get(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    integration = _resolve_integration(project_id, payload.integration)

    result = mqtt_diagnostics.test_connection(integration)

    event_service.record(
        project_id,
        kind="integration",
        status="info" if result["ok"] else "failed",
        summary=(
            f"test-connection {result['host']}:{result['port']} → "
            + ("ok" if result["ok"] else "failed")
        ),
        # NB: redact_integration drops password / unknown fields before logging.
        details={
            "kind": "test_connection",
            "integration": mqtt_diagnostics.redact_integration(integration),
            "result": result,
        },
    )
    return result


@router.post("/{project_id}/integration/test-credentials")
def integration_test_credentials(
    project_id: str, payload: MqttTestRequest
) -> dict[str, Any]:
    """Full MQTT CONNECT + CONNACK round-trip to validate username/password."""
    if project_service.get(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    integration = _resolve_integration(project_id, payload.integration)

    result = mqtt_diagnostics.test_credentials(integration)

    event_service.record(
        project_id,
        kind="integration",
        status="info" if result["ok"] else "failed",
        summary=(
            f"test-credentials user={integration.get('username') or '∅'} → "
            + ("ok" if result["ok"] else "failed")
        ),
        details={
            "kind": "test_credentials",
            "integration": mqtt_diagnostics.redact_integration(integration),
            "result": result,
        },
    )
    return result


@router.post("/{project_id}/integration/publish-test")
def integration_publish_test(
    project_id: str, payload: MqttTestRequest
) -> dict[str, Any]:
    """Publish exactly one synthetic payload through the full stack."""
    if project_service.get(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    integration = _resolve_integration(project_id, payload.integration)
    device = _first_device(project_id, payload.device_eui)

    result = mqtt_diagnostics.publish_test(
        integration,
        device_eui=str(device.get("device_eui", "")),
        sensor_type=str(device.get("type", "")),
    )

    event_service.record(
        project_id,
        kind="integration",
        status="info" if result["ok"] else "failed",
        summary=(
            f"publish-test {result.get('topic') or '∅'} → "
            + ("ok" if result["ok"] else "failed")
        ),
        details={
            "kind": "publish_test",
            "device_eui": device.get("device_eui"),
            "integration": mqtt_diagnostics.redact_integration(integration),
            # Strip the payload body from the event log — it can be large
            # and is reconstructable from the device config. Keep only
            # the topic + latency + status.
            "result": {
                "ok": result["ok"],
                "message": result["message"],
                "topic": result.get("topic"),
                "latency_ms": result.get("latency_ms"),
            },
        },
    )
    return result


@router.get("/{project_id}/mapping")
def sensgreen_mapping(project_id: str) -> dict[str, Any]:
    """Return the EUI → Sensgreen-metric mapping table for this project.

    Sensgreen's backend only sees the ``deviceEui`` on every payload —
    name and zone are recorded locally for the operator's benefit. This
    endpoint surfaces that distinction in a single table so an operator
    can hand the EUI list off for provisioning.
    """
    if project_service.get(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    cfg = project_service.get_config(project_id)
    # Build minimal device shims the mapping helper can read via getattr.
    shims = [_DeviceShim(d) for d in (cfg.get("devices") or [])]
    return {
        "project_id": project_id,
        "device_count": len(shims),
        "rows": mqtt_diagnostics.mapping_table(shims),
    }


class _DeviceShim:
    """Attribute view over a device config dict for mapping_table()."""

    __slots__ = ("device_eui", "name", "type", "zone_id")

    def __init__(self, raw: dict[str, Any]) -> None:
        self.device_eui = str(raw.get("device_eui", ""))
        self.name = str(raw.get("name", ""))
        self.type = str(raw.get("type", ""))
        self.zone_id = raw.get("zone_id")
