"""Adapter to the validator engine for the web UI.

The Validation Report page reads from this service. All scoring,
rule-loading, and report-building logic lives in
``simulator.validators`` and must not leak into routes or templates.
"""
from __future__ import annotations

from typing import Any


class ValidationService:
    """Thin facade around the validator engine."""

    def get_latest_report(self, project_id: str) -> dict[str, Any] | None:  # pragma: no cover - stub
        return None

    def list_reports(self, project_id: str) -> list[dict[str, Any]]:  # pragma: no cover - stub
        return []


validation_service = ValidationService()
