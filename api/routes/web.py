"""HTML routes (server-rendered Jinja templates).

These routes return HTML, not JSON. They handle the form POST for project
creation by re-using the same ``ProjectService`` that the JSON API uses,
so there is exactly one persistence path.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from api.services.event_service import event_service
from api.services.project_service import project_service
from simulator.scenarios import list_scenarios
from simulator.services.live_session import live_controller

ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES_DIR = ROOT / "web" / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter()


# ---------------------------------------------------------------------------
# Navigation metadata — kept here so layout.html stays declarative.
# Mirrors the order defined in UI_CONTEXT.md §6.
# ---------------------------------------------------------------------------
NAV_ITEMS = [
    {"key": "dashboard", "label": "Dashboard", "href": "/", "scoped": False},
    {"key": "projects", "label": "Projects", "href": "/", "scoped": False},
    {"key": "building", "label": "Building Setup", "href": "#", "scoped": True},
    {"key": "zones", "label": "Zones & Rooms", "href": "#", "scoped": True},
    {"key": "devices", "label": "Devices", "href": "#", "scoped": True},
    {"key": "scenarios", "label": "Scenarios", "href": "#", "scoped": True},
    {"key": "runner", "label": "Simulation Runner", "href": "#", "scoped": True},
    {"key": "validation", "label": "Validation Report", "href": "#", "scoped": True},
    {"key": "mqtt", "label": "Live MQTT Monitor", "href": "#", "scoped": True},
    {"key": "exports", "label": "Exports", "href": "#", "scoped": True},
]


def _base_context(active: str) -> dict:
    return {
        "nav_items": NAV_ITEMS,
        "active_nav": active,
        "app_name": "Sensgreen Sensor Simulator",
    }


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    projects = project_service.list()
    ctx = _base_context(active="dashboard")
    ctx["projects"] = projects
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@router.get("/projects/new", response_class=HTMLResponse)
def project_create_form(request: Request) -> HTMLResponse:
    ctx = _base_context(active="projects")
    ctx["form_errors"] = {}
    ctx["form_values"] = {
        "name": "",
        "building_type": "office",
        "city": "",
        "timezone": "Europe/Istanbul",
        "area_m2": "",
        "floors": 1,
        "demo_depth": "standard",
    }
    return templates.TemplateResponse(request, "project_create.html", ctx)


@router.post("/projects/new", response_model=None)
def project_create_submit(
    request: Request,
    name: str = Form(...),
    building_type: str = Form("office"),
    city: str = Form(""),
    timezone: str = Form("UTC"),
    area_m2: float = Form(0),
    floors: int = Form(1),
    demo_depth: str = Form("standard"),
) -> HTMLResponse | RedirectResponse:
    payload = {
        "name": name,
        "building_type": building_type,
        "city": city,
        "timezone": timezone,
        "area_m2": area_m2,
        "floors": floors,
        "demo_depth": demo_depth,
    }
    try:
        project = project_service.create(payload)
    except ValueError as exc:
        ctx = _base_context(active="projects")
        ctx["form_errors"] = {"name": str(exc)}
        ctx["form_values"] = payload
        return templates.TemplateResponse(
            request, "project_create.html", ctx, status_code=400
        )

    return RedirectResponse(url=f"/projects/{project.id}", status_code=303)


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_detail(request: Request, project_id: str) -> HTMLResponse:
    project = project_service.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    ctx = _base_context(active="projects")
    ctx["project"] = project
    # Derived counts + coverage + last-event snapshot for Overview.
    # Wrapped in try/except so a corrupt YAML cannot 500 the page —
    # the rest of the dashboard (Devices/YAML editor) still helps the
    # user recover.
    try:
        ctx["derived_status"] = project_service.derive_status(project_id)
    except Exception:
        ctx["derived_status"] = None
    ctx["integration"] = _masked_integration(project_id)
    ctx["events"] = [e.to_dict() for e in event_service.recent(project_id, limit=50)]
    # Prefer the dashboard-managed YAML when it exists. Falling back to
    # the legacy ``configs/<slug>.yaml`` guess is almost never right and
    # produces "config_path does not exist" errors at start time.
    if project_service.has_managed_config(project_id):
        ctx["default_config_path"] = ""  # blank → live route uses managed YAML
    else:
        ctx["default_config_path"] = f"configs/{project.id.rsplit('-', 1)[0]}.yaml"

    # Scenario catalog + saved assignments (merged by id).
    from dataclasses import asdict as _asdict
    catalog = [_asdict(s) for s in list_scenarios()]
    saved = {s["id"]: s for s in project_service.get_scenarios(project_id)}
    for entry in catalog:
        saved_entry = saved.get(entry["id"])
        entry["enabled"] = bool(saved_entry["enabled"]) if saved_entry else False
        entry["start"] = saved_entry["start"] if saved_entry else None
        entry["end"] = saved_entry["end"] if saved_entry else None
    ctx["scenario_catalog"] = catalog

    # Live session snapshot.
    live = live_controller.status(project_id)
    ctx["live_status"] = live.to_dict() if live else {"state": "stopped"}

    return templates.TemplateResponse(request, "project_detail.html", ctx)


@router.get("/projects/{project_id}/events", response_class=HTMLResponse)
def project_events_fragment(
    request: Request,
    project_id: str,
    kind: str | None = None,
    status: str | None = None,
    q: str | None = None,
) -> HTMLResponse:
    """HTMX endpoint: returns just the events table body for live refresh.

    Accepts the same filters as the JSON ``/api/projects/{id}/events``
    endpoint so the dashboard's filter dropdowns can drive the table
    directly via HTMX.
    """
    if project_service.get(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    events = [
        e.to_dict()
        for e in event_service.recent(
            project_id, limit=50, kind=kind, status=status, query=q
        )
    ]
    return templates.TemplateResponse(
        request,
        "_events_table.html",
        {
            "events": events,
            "project_id": project_id,
            "filter_kind": kind or "",
            "filter_status": status or "",
            "filter_q": q or "",
        },
    )


def _masked_integration(project_id: str) -> dict | None:
    integ = project_service.get_integration(project_id)
    if integ is None:
        return None
    masked = dict(integ)
    if masked.get("password"):
        masked["password"] = "********"
    return masked
