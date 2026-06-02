"""Layout generation utilities.

Given a building archetype, a total area, and a floor count, the generator
materialises a deterministic list of :class:`~simulator.models.config.ZoneConfig`-shaped
dicts ready to be persisted on a project. The output is fully deterministic for
a given ``(archetype_id, total_area_m2, floors, seed)`` tuple so that previews
match what an ``apply`` call would write to disk.

This module is **pure-Python**: no I/O, no FastAPI, no project_service. The
HTTP layer wraps it in :mod:`api.routes.config` (POST generate-layout).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any, Iterable

from simulator.catalogs import (
    DEFAULT_ROOM_TYPE_ID,
    get_building_archetype,
    get_room_type,
)


# Minimum fraction below which a room type contributes zero rooms even if
# we'd otherwise round it up. Prevents tiny mix entries (e.g. 1%) from
# spawning a single room in small buildings.
_MIN_EFFECTIVE_FRACTION = 0.01


class LayoutGenerationError(ValueError):
    """Raised for invalid generation inputs (unknown archetype, bad area...)."""


@dataclass(frozen=True)
class LayoutSpec:
    archetype_id: str
    total_area_m2: float
    floors: int = 1
    seed: int | None = None

    def validate(self) -> None:
        if not isinstance(self.archetype_id, str) or not self.archetype_id.strip():
            raise LayoutGenerationError("archetype_id is required")
        if not (self.total_area_m2 and self.total_area_m2 > 0):
            raise LayoutGenerationError("total_area_m2 must be > 0")
        if not (isinstance(self.floors, int) and self.floors >= 1):
            raise LayoutGenerationError("floors must be >= 1")
        if self.seed is not None and not isinstance(self.seed, int):
            raise LayoutGenerationError("seed must be an integer or null")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalise_mix(mix: dict[str, float]) -> list[tuple[str, float]]:
    """Return [(room_type_id, fraction)] in deterministic order with
    fractions re-normalised to sum to 1.0. Filters out non-positive entries.
    """
    items = [
        (rt_id, float(frac))
        for rt_id, frac in mix.items()
        if isinstance(frac, (int, float)) and frac > 0
    ]
    total = sum(frac for _, frac in items)
    if total <= 0:
        return []
    # Sort by descending fraction, then by id for stable ordering.
    items.sort(key=lambda p: (-p[1], p[0]))
    return [(rt_id, frac / total) for rt_id, frac in items]


def _room_counts(
    mix: list[tuple[str, float]], total_area_m2: float
) -> list[tuple[str, int, float]]:
    """For each room type, compute (id, instance_count, per_room_area_m2).

    Counts are computed by ``target_area / default_area_m2`` then rounded.
    A room type with an effective fraction below ``_MIN_EFFECTIVE_FRACTION``
    is skipped entirely (no room).
    """
    out: list[tuple[str, int, float]] = []
    for rt_id, frac in mix:
        if frac < _MIN_EFFECTIVE_FRACTION:
            continue
        rt = get_room_type(rt_id)
        if rt is None:
            # Unknown room id in catalog — skip silently rather than crash;
            # this can happen if an archetype references a future room type.
            continue
        default_area = float(rt.get("default_area_m2") or 25.0)
        target_area = total_area_m2 * frac
        count = max(1, int(round(target_area / default_area)))
        out.append((rt_id, count, default_area))
    return out


def _slugify(text: str) -> str:
    cleaned = []
    for ch in text.lower():
        if ch.isalnum():
            cleaned.append(ch)
        elif ch in (" ", "-", "_"):
            cleaned.append("-")
    slug = "".join(cleaned)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-") or "zone"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_layout(spec: LayoutSpec) -> dict[str, Any]:
    """Generate a deterministic layout from a :class:`LayoutSpec`.

    Returns a dict shaped as::

        {
            "archetype_id": str,
            "total_area_m2": float,
            "floors": int,
            "seed": int | None,
            "zones": [ {id, name, area_m2, capacity, room_type, floor_id}, ... ],
            "summary": {
                "zone_count": int,
                "modeled_area_m2": float,
                "by_room_type": {room_type_id: count, ...},
                "by_floor": {floor_id: count, ...},
            },
        }

    The ``zones`` list is the same shape ``ZonePayload`` accepts, so the API
    layer can drop it straight into ``cfg["building"]["zones"]``.
    """
    spec.validate()
    archetype = get_building_archetype(spec.archetype_id)
    if archetype is None:
        raise LayoutGenerationError(
            f"unknown building archetype: {spec.archetype_id!r}"
        )

    mix = _normalise_mix(archetype.get("room_mix") or {})
    if not mix:
        raise LayoutGenerationError(
            f"archetype {spec.archetype_id!r} has no usable room_mix"
        )

    counts = _room_counts(mix, spec.total_area_m2)
    if not counts:
        # Fallback: produce one default room rather than failing on tiny areas.
        default_rt = get_room_type(DEFAULT_ROOM_TYPE_ID) or {}
        counts = [
            (
                DEFAULT_ROOM_TYPE_ID,
                1,
                float(default_rt.get("default_area_m2") or spec.total_area_m2),
            )
        ]

    rng = random.Random(spec.seed if spec.seed is not None else 0)

    # Build a flat list of (room_type_id, instance_index_in_type, per_room_area).
    flat: list[tuple[str, int, float]] = []
    for rt_id, n, per_area in counts:
        for i in range(n):
            flat.append((rt_id, i + 1, per_area))

    # Deterministic floor assignment: round-robin so each floor sees a balanced
    # mix. Shuffle the *order* (still seeded) so floor 1 doesn't always get the
    # first room type, but the order is stable for the same seed.
    rng.shuffle(flat)

    zones: list[dict[str, Any]] = []
    by_floor: dict[str, int] = {f"F{f}": 0 for f in range(1, spec.floors + 1)}
    by_room_type: dict[str, int] = {}
    modeled_area = 0.0

    used_ids: set[str] = set()
    for idx, (rt_id, instance_idx, per_area) in enumerate(flat):
        floor_num = (idx % spec.floors) + 1
        floor_id = f"F{floor_num}"
        rt = get_room_type(rt_id) or {}
        rt_name = str(rt.get("name") or rt_id.replace("_", " ").title())
        default_capacity = int(rt.get("default_capacity") or 0)

        # Per-floor sequence number for this room type.
        per_floor_seq = (
            sum(
                1
                for z in zones
                if z["room_type"] == rt_id and z["floor_id"] == floor_id
            )
            + 1
        )

        base_id = f"{_slugify(rt_id)}-{floor_id.lower()}-{per_floor_seq:02d}"
        zone_id = base_id
        # Defensive: handle pathological collisions (shouldn't happen with our
        # numbering, but cheap insurance).
        bump = 1
        while zone_id in used_ids:
            bump += 1
            zone_id = f"{base_id}-{bump}"
        used_ids.add(zone_id)

        name = f"{rt_name} {floor_id}-{per_floor_seq:02d}"

        zone = {
            "id": zone_id,
            "name": name,
            "area_m2": round(float(per_area), 1),
            "capacity": default_capacity,
            "room_type": rt_id,
            "floor_id": floor_id,
        }
        zones.append(zone)
        modeled_area += float(per_area)
        by_floor[floor_id] = by_floor.get(floor_id, 0) + 1
        by_room_type[rt_id] = by_room_type.get(rt_id, 0) + 1

    # Order the zones deterministically for stable output: floor, then
    # room_type id, then per-floor sequence.
    zones.sort(
        key=lambda z: (
            int(str(z["floor_id"]).lstrip("F") or 0),
            z["room_type"],
            z["id"],
        )
    )

    return {
        "archetype_id": spec.archetype_id,
        "archetype_name": archetype.get("name") or spec.archetype_id,
        "total_area_m2": float(spec.total_area_m2),
        "floors": spec.floors,
        "seed": spec.seed,
        "zones": zones,
        "summary": {
            "zone_count": len(zones),
            "modeled_area_m2": round(modeled_area, 1),
            "by_room_type": by_room_type,
            "by_floor": by_floor,
        },
    }


__all__ = [
    "LayoutGenerationError",
    "LayoutSpec",
    "generate_layout",
]
