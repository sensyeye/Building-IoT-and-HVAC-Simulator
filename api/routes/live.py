"""Live-mode control + SSE stream routes.

* ``POST /api/projects/{project_id}/live/start``  — start a background session
* ``POST /api/projects/{project_id}/live/stop``   — stop it
* ``GET  /api/projects/{project_id}/live/status`` — current status snapshot
* ``GET  /api/projects/{project_id}/live/stream`` — text/event-stream of readings

The stream emits an initial ``snapshot`` event with the current ring buffer,
then one ``reading`` event per new reading. SSE clients (browsers' built-in
``EventSource``) reconnect automatically; we don't try to persist state
across reconnects beyond the bounded ring buffer.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.services.event_service import event_service
from api.services.project_service import project_service
from simulator.config_loader import ConfigError, load_config
from simulator.integrations.sensgreen_mqtt_publisher import SensgreenMqttPublisher
from simulator.services.live_session import live_controller


router = APIRouter()
_log = logging.getLogger("sensgreen.api.live")


class LiveStartPayload(BaseModel):
    # Optional: when blank, the project's managed YAML
    # (``data/projects/<id>.config.yaml``) is used instead.
    config_path: str | None = Field(None)
    dry_run: bool = Field(False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_project(project_id: str) -> None:
    if project_service.get(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")


def _record_event(project_id: str, status: str, summary: str, details: dict) -> None:
    """Callback handed to :class:`LiveSession` so it can append events."""
    # The session emits its own statuses ("running" | "succeeded" | "failed").
    # Map to the event-service taxonomy.
    if status == "running":
        ev_status = "running"
    elif status == "succeeded":
        ev_status = "succeeded"
    elif status == "failed":
        ev_status = "failed"
    else:  # pragma: no cover - defensive
        ev_status = "info"
    try:
        event_service.record(
            project_id,
            kind="live_run",
            status=ev_status,
            summary=summary,
            details=details,
        )
    except Exception:  # pragma: no cover - defensive
        _log.exception("failed to record live_run event")


# ---------------------------------------------------------------------------
# Start / stop / status
# ---------------------------------------------------------------------------


@router.post("/{project_id}/live/start")
def start_live(project_id: str, payload: LiveStartPayload) -> dict[str, Any]:
    _require_project(project_id)

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
    elif not Path(config_path).exists():
        raise HTTPException(
            status_code=400,
            detail=f"config_path does not exist: {config_path}",
        )
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=f"config error: {exc}") from exc

    integration = project_service.get_integration(project_id)
    if integration is None and not payload.dry_run:
        raise HTTPException(
            status_code=400,
            detail="no integration configured for this project (set it first, "
                   "or run with dry_run=true)",
        )

    if integration is not None:
        publisher = SensgreenMqttPublisher.from_integration(
            integration, dry_run=payload.dry_run
        )
    else:
        if cfg.outputs.mqtt is None:
            raise HTTPException(
                status_code=400,
                detail="config has no outputs.mqtt block and no integration is "
                       "configured",
            )
        publisher = SensgreenMqttPublisher.from_config(
            cfg.outputs.mqtt, dry_run=payload.dry_run
        )

    status = live_controller.start(
        project_id, cfg, publisher, on_event=_record_event,
        scenario_assignments=project_service.get_scenarios(project_id),
    )
    return status.to_dict()


@router.post("/{project_id}/live/stop")
def stop_live(project_id: str) -> dict[str, Any]:
    _require_project(project_id)
    status = live_controller.stop(project_id)
    if status is None:
        return {"project_id": project_id, "state": "stopped"}
    return status.to_dict()


@router.get("/{project_id}/live/status")
def live_status(project_id: str) -> dict[str, Any]:
    _require_project(project_id)
    status = live_controller.status(project_id)
    if status is None:
        return {"project_id": project_id, "state": "stopped"}
    return status.to_dict()


@router.get("/{project_id}/live/active-scenarios")
def live_active_scenarios(project_id: str) -> dict[str, Any]:
    """Return the scenarios currently in effect for this project.

    Used by the dashboard's Live tab to render the "active scenarios"
    badge row above the readings table.
    """
    _require_project(project_id)
    from datetime import datetime, timezone

    from simulator.scenarios import (
        active_scenarios_at,
        get_impact,
        get_scenario,
    )

    assignments = project_service.get_scenarios(project_id)
    now = datetime.now(timezone.utc)
    ids = active_scenarios_at(now, assignments)
    out: list[dict[str, Any]] = []
    for sid in ids:
        meta = get_scenario(sid)
        impact = get_impact(sid)
        out.append({
            "id": sid,
            "name": meta.name if meta else sid,
            "category": meta.category if meta else "",
            "description": meta.description if meta else "",
            "why": impact.why if impact else "",
            "channels": list(impact.channels) if impact else [],
        })
    return {
        "project_id": project_id,
        "now": now.isoformat(timespec="seconds"),
        "scenarios": out,
    }


# ---------------------------------------------------------------------------
# SSE stream
# ---------------------------------------------------------------------------


@router.get("/{project_id}/live/stream")
async def live_stream(project_id: str, request: Request) -> StreamingResponse:
    _require_project(project_id)
    session = live_controller.get(project_id)
    if session is None:
        # Still return a valid event stream so the client doesn't fall
        # into reconnect storms when the session isn't running yet.
        async def empty():
            yield _sse("status", {"state": "stopped", "project_id": project_id})
            # Keep the connection open with a heartbeat until the client
            # leaves, so it can start receiving as soon as a session starts.
            while not await request.is_disconnected():
                await asyncio.sleep(15)
                yield _sse_comment("heartbeat")
        return StreamingResponse(empty(), media_type="text/event-stream")

    buffer = session.buffer
    loop = asyncio.get_running_loop()
    queue = buffer.subscribe(loop)

    async def generator():
        try:
            yield _sse("status", session.status().to_dict())
            yield _sse("snapshot", [it.to_dict() for it in buffer.snapshot()])
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield _sse_comment("heartbeat")
                    continue
                yield _sse("reading", item.to_dict())
        finally:
            buffer.unsubscribe(queue)

    return StreamingResponse(generator(), media_type="text/event-stream")


def _sse(event: str, data: Any) -> str:
    body = json.dumps(data, default=str)
    return f"event: {event}\ndata: {body}\n\n"


def _sse_comment(text: str) -> str:
    return f": {text}\n\n"
