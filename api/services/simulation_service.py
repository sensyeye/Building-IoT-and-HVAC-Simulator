"""Adapter to the simulator engine for the web UI.

The web UI must never re-implement simulation logic. This service is a
seam where the API layer calls into ``simulator.services`` (engine code)
to start jobs, list jobs, and fetch results.

For the MVP skeleton this is intentionally a stub — actual job
orchestration will be wired up alongside the Simulation Runner page.
"""
from __future__ import annotations

from typing import Any


class SimulationService:
    """Thin facade around the simulator engine's job runners."""

    def list_jobs(self, project_id: str) -> list[dict[str, Any]]:  # pragma: no cover - stub
        return []

    def get_job(self, job_id: str) -> dict[str, Any] | None:  # pragma: no cover - stub
        return None

    def start_historical(
        self, project_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:  # pragma: no cover - stub
        raise NotImplementedError("Historical runs will be wired up in a follow-up task")


simulation_service = SimulationService()
