"""Static catalogs for building archetypes and room types.

These JSON files act as read-only reference data consumed by the layout
generator, monitoring profile system, scenario targeting, and the room-state
engine. They never get mutated at runtime; callers receive deep copies so they
can mutate freely without affecting cached state.
"""
from __future__ import annotations

import copy
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
_ARCHETYPES_PATH = _HERE / "building_archetypes.json"
_ROOM_TYPES_PATH = _HERE / "room_types.json"
_PROFILES_PATH = _HERE / "monitoring_profiles.json"

DEFAULT_ROOM_TYPE_ID = "open_office"


@lru_cache(maxsize=1)
def _load_archetypes_raw() -> list[dict]:
    with _ARCHETYPES_PATH.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return list(data.get("archetypes", []))


@lru_cache(maxsize=1)
def _load_room_types_raw() -> list[dict]:
    with _ROOM_TYPES_PATH.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return list(data.get("room_types", []))


@lru_cache(maxsize=1)
def _load_profiles_raw() -> list[dict]:
    with _PROFILES_PATH.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return list(data.get("monitoring_profiles", []))


@lru_cache(maxsize=1)
def _archetypes_by_id() -> dict[str, dict]:
    return {entry["id"]: entry for entry in _load_archetypes_raw()}


@lru_cache(maxsize=1)
def _room_types_by_id() -> dict[str, dict]:
    return {entry["id"]: entry for entry in _load_room_types_raw()}


@lru_cache(maxsize=1)
def _profiles_by_id() -> dict[str, dict]:
    return {entry["id"]: entry for entry in _load_profiles_raw()}


def list_building_archetypes() -> list[dict]:
    """Return a deep copy of every building archetype."""
    return copy.deepcopy(_load_archetypes_raw())


def get_building_archetype(archetype_id: str) -> Optional[dict]:
    """Return a deep copy of the archetype with ``archetype_id`` or ``None``."""
    entry = _archetypes_by_id().get(archetype_id)
    return copy.deepcopy(entry) if entry is not None else None


def list_room_types() -> list[dict]:
    """Return a deep copy of every room type entry."""
    return copy.deepcopy(_load_room_types_raw())


def get_room_type(room_type_id: str) -> Optional[dict]:
    """Return a deep copy of the room type with ``room_type_id`` or ``None``."""
    entry = _room_types_by_id().get(room_type_id)
    return copy.deepcopy(entry) if entry is not None else None


def _normalize(text: str) -> str:
    text = text.lower()
    # Collapse non-alphanumerics to single space for tolerant keyword matching.
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


@lru_cache(maxsize=1)
def _keyword_index() -> list[tuple[str, str]]:
    """Return [(normalized_keyword, room_type_id)] ordered by descending
    keyword length so multi-word matches win over shorter prefixes.
    """
    pairs: list[tuple[str, str]] = []
    for rt in _load_room_types_raw():
        rt_id = rt["id"]
        for kw in rt.get("infer_keywords", []) or []:
            norm = _normalize(kw)
            if norm:
                pairs.append((norm, rt_id))
    pairs.sort(key=lambda p: (-len(p[0]), p[1]))
    return pairs


def infer_room_type(zone_name: str, zone_id: str = "") -> tuple[str, bool]:
    """Infer a room-type id from a zone's name (and id as fallback).

    Returns ``(room_type_id, was_inferred)``. ``was_inferred`` is always True
    when this function returns a guess; callers should only invoke it when no
    explicit ``room_type`` was set on disk. The default fallback is
    ``open_office`` per the agreed conservative behaviour.
    """
    haystack = " ".join(filter(None, [_normalize(zone_name or ""), _normalize(zone_id or "")]))
    if haystack:
        padded = f" {haystack} "
        for keyword, rt_id in _keyword_index():
            if f" {keyword} " in padded:
                return rt_id, True
    return DEFAULT_ROOM_TYPE_ID, True


# ---------------------------------------------------------------------------
# Monitoring profiles
# ---------------------------------------------------------------------------


def list_monitoring_profiles() -> list[dict]:
    """Return a deep copy of every monitoring profile."""
    return copy.deepcopy(_load_profiles_raw())


def get_monitoring_profile(profile_id: str) -> Optional[dict]:
    """Return a deep copy of the monitoring profile with ``profile_id`` or ``None``."""
    entry = _profiles_by_id().get(profile_id)
    return copy.deepcopy(entry) if entry is not None else None


def default_profile_for_room_type(room_type_id: str) -> Optional[str]:
    """Return the first profile id from the room type's
    ``typical_monitoring_profiles`` list, or ``None`` if the room type or
    list is unknown/empty.
    """
    rt = _room_types_by_id().get(room_type_id)
    if rt is None:
        return None
    profiles = rt.get("typical_monitoring_profiles") or []
    return str(profiles[0]) if profiles else None


def compute_zone_coverage(
    zone: dict, devices: list[dict], *, profile_id: Optional[str] = None
) -> dict:
    """Compute monitoring-profile coverage for a single zone.

    Parameters
    ----------
    zone : the zone dict (must have at minimum ``id``; ``monitoring_profile``
        and ``room_type`` are read if present).
    devices : every device in the project; the function filters by
        ``device.zone_id == zone.id``.
    profile_id : explicit profile id override. When ``None``, uses
        ``zone.monitoring_profile`` and falls back to the first profile
        recommended by the zone's ``room_type``.

    Returns a dict shaped::

        {
            "profile_id": str | None,
            "profile_inferred": bool,
            "required": [sensor_type_id, ...],
            "recommended": [sensor_type_id, ...],
            "present": {sensor_type_id: count, ...},
            "missing_required": [sensor_type_id, ...],
            "missing_recommended": [sensor_type_id, ...],
            "extra": [sensor_type_id, ...],
            "status": "ok" | "partial" | "missing" | "no_profile",
        }

    ``status``:
    * ``no_profile`` — no profile resolved (zone has none and room type has none).
    * ``missing``   — at least one required sensor type is absent.
    * ``partial``   — all required present, at least one recommended absent.
    * ``ok``        — all required + all recommended present.
    """
    zone_id = str(zone.get("id") or "")
    explicit_profile = (zone.get("monitoring_profile") or "").strip() or None
    inferred = False
    if profile_id is None:
        profile_id = explicit_profile
    if profile_id is None:
        profile_id = default_profile_for_room_type(str(zone.get("room_type") or ""))
        inferred = profile_id is not None

    profile = get_monitoring_profile(profile_id) if profile_id else None
    if profile is None:
        return {
            "profile_id": None,
            "profile_inferred": False,
            "required": [],
            "recommended": [],
            "present": {},
            "missing_required": [],
            "missing_recommended": [],
            "extra": [],
            "status": "no_profile",
        }

    required = [str(s) for s in (profile.get("required_sensor_types") or [])]
    recommended = [str(s) for s in (profile.get("recommended_sensor_types") or [])]
    expected = set(required) | set(recommended)

    present_counts: dict[str, int] = {}
    for dev in devices or []:
        if not isinstance(dev, dict):
            continue
        if str(dev.get("zone_id")) != zone_id:
            continue
        stype = str(dev.get("type") or "")
        if not stype:
            continue
        present_counts[stype] = present_counts.get(stype, 0) + 1

    present_types = set(present_counts.keys())
    missing_required = [s for s in required if s not in present_types]
    missing_recommended = [s for s in recommended if s not in present_types]
    extra = sorted(present_types - expected)

    if missing_required:
        status = "missing"
    elif missing_recommended:
        status = "partial"
    else:
        status = "ok"

    return {
        "profile_id": profile_id,
        "profile_inferred": inferred,
        "required": required,
        "recommended": recommended,
        "present": present_counts,
        "missing_required": missing_required,
        "missing_recommended": missing_recommended,
        "extra": extra,
        "status": status,
    }


__all__ = [
    "DEFAULT_ROOM_TYPE_ID",
    "DEFAULT_INTENT",
    "DEFAULT_RICHNESS",
    "MONITORING_INTENTS",
    "RICHNESS_LEVELS",
    "compute_zone_coverage",
    "default_profile_for_room_type",
    "device_type_summary",
    "get_building_archetype",
    "get_monitoring_profile",
    "get_room_type",
    "infer_room_type",
    "list_building_archetypes",
    "list_monitoring_intents",
    "list_monitoring_profiles",
    "list_richness_levels",
    "list_room_types",
    "normalize_intent",
    "normalize_richness",
    "recommend_devices",
    "recommend_devices_for_room",
]

# Re-export the recommendation engine (P9.4).
from .recommendations import (  # noqa: E402  (after __all__ by design)
    DEFAULT_INTENT,
    DEFAULT_RICHNESS,
    MONITORING_INTENTS,
    RICHNESS_LEVELS,
    device_type_summary,
    list_monitoring_intents,
    list_richness_levels,
    normalize_intent,
    normalize_richness,
    recommend_devices,
    recommend_devices_for_room,
)
