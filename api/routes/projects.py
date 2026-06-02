"""JSON API routes for projects."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from api.services.event_service import event_service
from api.services.project_service import Project, project_service

router = APIRouter()


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    building_type: str = Field("office", max_length=60)
    city: str = Field("", max_length=120)
    timezone: str = Field("UTC", max_length=60)
    area_m2: float = Field(0, ge=0)
    floors: int = Field(1, ge=1, le=200)
    demo_depth: str = Field("standard", pattern="^(light|standard|deep)$")


def _serialize(p: Project) -> dict[str, Any]:
    """Project payload returned by the API.

    We attach a live ``status`` block derived from the managed config
    so the Overview tab and any external consumers don't read the
    stale ``device_count`` that the Project record stores.
    """
    out = p.to_dict()
    try:
        out["status"] = project_service.derive_status(p.id)
    except Exception:  # pragma: no cover - defensive: never break the API
        out["status"] = None
    return out


@router.get("")
def list_projects() -> list[dict[str, Any]]:
    return [_serialize(p) for p in project_service.list()]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectCreate) -> dict[str, Any]:
    project = project_service.create(payload.model_dump())
    return _serialize(project)


@router.get("/{project_id}")
def get_project(project_id: str) -> dict[str, Any]:
    project = project_service.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return _serialize(project)


@router.get("/{project_id}/events")
def list_events(
    project_id: str,
    limit: int = Query(50, ge=1, le=500),
    kind: str | None = Query(None),
    status: str | None = Query(None),
    q: str | None = Query(None),
) -> dict[str, Any]:
    """Return the most recent events for ``project_id`` (newest first)."""
    project = project_service.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    events = event_service.recent(
        project_id, limit=limit, kind=kind, status=status, query=q
    )
    return {
        "project_id": project_id,
        "count": len(events),
        "events": [e.to_dict() for e in events],
    }


@router.delete("/{project_id}/events")
def clear_events(project_id: str) -> dict[str, Any]:
    """Wipe the event log for ``project_id``. Returns the removed count."""
    project = project_service.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    removed = event_service.clear(project_id)
    return {"project_id": project_id, "removed": removed}
