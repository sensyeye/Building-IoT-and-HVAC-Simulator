"""FastAPI application entry point.

Run locally with::

    uvicorn api.main:app --reload

The app is intentionally thin: it mounts static assets, wires up routers,
and exposes a ``/health`` probe. All domain logic lives in the
``simulator`` package; everything under ``api/services`` only adapts that
package for HTTP / template consumption.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api.routes import (
    bridge,
    catalogs,
    config,
    exports,
    live,
    mqtt,
    projects,
    scenarios,
    simulations,
    validation,
    web,
)

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "web" / "static"

app = FastAPI(
    title="Sensgreen Sensor Simulator",
    description="Internal admin UI for configuring and running sensor simulations.",
    version="0.1.0",
)

# Static assets (Tailwind/HTMX come from CDN; this is for our own CSS/JS).
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    """Liveness probe used by uptime checks and the top-bar status pill."""
    return {"status": "ok"}


# Web (HTML) routes
app.include_router(web.router)

# JSON API routes
app.include_router(projects.router, prefix="/api/projects", tags=["projects"])
app.include_router(bridge.router, prefix="/api/projects", tags=["bridge"])
app.include_router(live.router, prefix="/api/projects", tags=["live"])
app.include_router(scenarios.router, prefix="/api", tags=["scenarios"])
app.include_router(simulations.router, prefix="/api/simulations", tags=["simulations"])
app.include_router(validation.router, prefix="/api/validation", tags=["validation"])
app.include_router(mqtt.router, prefix="/api/mqtt", tags=["mqtt"])
app.include_router(exports.router, prefix="/api/exports", tags=["exports"])
app.include_router(config.router, prefix="/api", tags=["config"])
app.include_router(catalogs.router, prefix="/api", tags=["catalogs"])


@app.on_event("shutdown")
def _stop_live_sessions() -> None:
    """Cleanly stop every running live session at process shutdown."""
    from simulator.services.live_session import live_controller
    live_controller.stop_all()
