"""JSON API routes for CSV exports (skeleton)."""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("")
def list_exports() -> list[dict]:
    """Placeholder — wired up alongside the Exports page."""
    return []
