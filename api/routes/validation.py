"""JSON API routes for validation reports (skeleton)."""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("")
def list_reports() -> list[dict]:
    """Placeholder — wired up alongside the Validation Report page."""
    return []
