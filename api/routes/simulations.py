"""JSON API routes for simulation jobs (skeleton)."""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("")
def list_jobs() -> list[dict]:
    """Placeholder — job orchestration is wired up in a follow-up task."""
    return []
