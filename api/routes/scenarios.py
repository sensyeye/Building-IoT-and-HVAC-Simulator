"""Scenario catalog + per-project scenario assignment routes.

* ``GET  /api/scenarios``                        — full catalog
* ``GET  /api/projects/{project_id}/scenarios``  — assignments for a project
* ``PUT  /api/projects/{project_id}/scenarios``  — overwrite assignments
"""
from __future__ import annotations

from typing import Any

from dataclasses import asdict
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.services.event_service import event_service
from api.services.project_service import project_service
from simulator.scenarios import list_scenarios


router = APIRouter()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ScenarioAssignment(BaseModel):
    id: str = Field(..., min_length=1)
    enabled: bool = Field(False)
    start: str | None = Field(None)
    end: str | None = Field(None)
    # Optional targeting (Phase 3). When both are None/empty the scenario
    # applies to all zones (legacy behaviour). When set, the assignment is
    # restricted to the listed zones / every zone served by the given HVAC
    # zone id.
    target_hvac_zone_id: str | None = Field(None)
    target_zone_ids: list[str] | None = Field(None)


class ScenarioAssignmentList(BaseModel):
    scenarios: list[ScenarioAssignment]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/scenarios")
def get_catalog() -> dict[str, Any]:
    """Return the full scenario catalog."""
    return {
        "scenarios": [asdict(s) for s in list_scenarios()],
    }


@router.get("/projects/{project_id}/scenarios")
def get_project_scenarios(project_id: str) -> dict[str, Any]:
    if project_service.get(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return {
        "project_id": project_id,
        "scenarios": project_service.get_scenarios(project_id),
    }


@router.put("/projects/{project_id}/scenarios")
def put_project_scenarios(
    project_id: str, payload: ScenarioAssignmentList
) -> dict[str, Any]:
    if project_service.get(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        saved = project_service.set_scenarios(
            project_id, [s.model_dump() for s in payload.scenarios]
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    enabled = [s["id"] for s in saved if s["enabled"]]
    event_service.record(
        project_id,
        kind="scenario",
        status="info",
        summary=(
            f"Scenarios updated · {len(enabled)} enabled"
            if enabled else "Scenarios updated · all disabled"
        ),
        details={"enabled": enabled, "count": len(saved)},
    )
    return {"project_id": project_id, "scenarios": saved}
