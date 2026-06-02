"""Read-only API for the room-type and building-archetype catalogs.

These power the project setup UI (archetype picker → room mix preview) and
the room-detail editor (room-type dropdown + defaults). Both endpoints are
pure reads — no auth, no persistence.

Routes
------
* ``GET /api/building-archetypes`` — list of all archetypes (12 entries)
* ``GET /api/room-types``          — list of all room types (~31 entries)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from simulator.catalogs import (
    get_building_archetype,
    get_monitoring_profile,
    get_room_type,
    list_building_archetypes,
    list_monitoring_intents,
    list_monitoring_profiles,
    list_richness_levels,
    list_room_types,
    recommend_devices,
)


router = APIRouter()


@router.get("/building-archetypes")
def get_building_archetypes() -> dict[str, Any]:
    archetypes = list_building_archetypes()
    return {"archetypes": archetypes, "count": len(archetypes)}


@router.get("/building-archetypes/{archetype_id}")
def get_building_archetype_by_id(archetype_id: str) -> dict[str, Any]:
    entry = get_building_archetype(archetype_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown building archetype")
    return {"archetype": entry}


@router.get("/room-types")
def get_room_types() -> dict[str, Any]:
    room_types = list_room_types()
    return {"room_types": room_types, "count": len(room_types)}


@router.get("/room-types/{room_type_id}")
def get_room_type_by_id(room_type_id: str) -> dict[str, Any]:
    entry = get_room_type(room_type_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown room type")
    return {"room_type": entry}


@router.get("/monitoring-profiles")
def get_monitoring_profiles() -> dict[str, Any]:
    profiles = list_monitoring_profiles()
    return {"monitoring_profiles": profiles, "count": len(profiles)}


@router.get("/monitoring-profiles/{profile_id}")
def get_monitoring_profile_by_id(profile_id: str) -> dict[str, Any]:
    entry = get_monitoring_profile(profile_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown monitoring profile")
    return {"monitoring_profile": entry}


# ---------------------------------------------------------------------------
# P9.4 — monitoring intent + richness recommendation engine
# ---------------------------------------------------------------------------


@router.get("/monitoring-intents")
def get_monitoring_intents() -> dict[str, Any]:
    items = list_monitoring_intents()
    return {"monitoring_intents": items, "count": len(items)}


@router.get("/richness-levels")
def get_richness_levels() -> dict[str, Any]:
    items = list_richness_levels()
    return {"richness_levels": items, "count": len(items)}


@router.get("/monitoring-recommendations")
def get_monitoring_recommendation(
    room_type: str | None = None,
    intent: str | None = None,
    richness: str | None = None,
) -> dict[str, Any]:
    """Preview the recommended device list for an (intent, richness)
    combination without persisting anything. Used by the room editor UI
    to render a live "what you'll get" chip.
    """
    specs = recommend_devices(room_type=room_type, intent=intent, richness=richness)
    return {
        "room_type": room_type,
        "intent": intent,
        "richness": richness,
        "devices": specs,
        "count": len(specs),
    }
