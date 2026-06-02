"""Monitoring intent + richness → recommended device list.

This module is the "brain" of P9.5 (the auto-recommendation engine).

It encodes a small, **declarative** table that answers:

    Given a room type, a monitoring intent, and a richness level,
    which devices should be installed in that room?

Intents and richness levels are deliberately coarse — they're the only
two choices a non-developer should have to make on the UI. Defaults are
chosen so that *any* combination produces *something* sensible; the
table has room-type-specific overrides where they matter (hotel guest
rooms, mall entrances, server rooms, etc.).

All sensor type ids on the right-hand side must match
:mod:`simulator.devices.catalog` ids — this module never names a sensor
that the simulator cannot actually build.

The output of :func:`recommend_devices_for_room` is a list of dicts of
the form::

    [
        {"type": "iaq",                 "role": "iaq"},
        {"type": "occupancy_sensor",    "role": "occupancy"},
        {"type": "door_contact",        "role": "door"},
    ]

The auto-provisioner combines this list with the room's `area_m2` and
the device catalog's `default_metadata()` to instantiate concrete
``DeviceConfig`` entries with deterministic EUIs.
"""

from __future__ import annotations

from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------

#: All monitoring intents the user can choose from on the UI.
MONITORING_INTENTS: tuple[str, ...] = (
    "comfort",                   # temperature/humidity comfort tracking
    "iaq",                       # full IAQ (CO2, PM, TVOC, T/RH)
    "occupancy",                 # presence / occupancy
    "energy",                    # electrical energy submetering
    "hvac_ops",                  # HVAC operational state (planned device)
    "people_flow",               # entry/exit counters for footfall
    "guest_room_automation",     # bundle for hotel/serviced apartments
    "retail_footfall",           # bundle for mall/retail entrances
    "compliance_reporting",      # IAQ + occupancy combined for reporting
)

#: Coarse richness levels. Higher levels are supersets of lower ones.
RICHNESS_LEVELS: tuple[str, ...] = ("basic", "standard", "advanced")

#: Default fallback when a room has no intent declared.
DEFAULT_INTENT: str = "iaq"
DEFAULT_RICHNESS: str = "standard"


# ---------------------------------------------------------------------------
# Intent × Richness → device list (with optional room-type overrides)
# ---------------------------------------------------------------------------
#
# Each cell of the table is a *list of device specs*. A device spec is a
# dict with at least ``type``; ``role`` is a short human-friendly tag
# used by the auto-provisioner to build readable device names.
#
# Lists are written in priority order — if a downstream caller decides
# to cap the number of devices per room, it should drop from the tail.

DeviceSpec = dict[str, Any]


def _iaq(role: str = "iaq") -> DeviceSpec:
    return {"type": "iaq", "role": role}


def _occ(role: str = "occupancy") -> DeviceSpec:
    return {"type": "occupancy_sensor", "role": role}


def _door(role: str = "door") -> DeviceSpec:
    return {"type": "door_contact", "role": role}


def _people(role: str = "people") -> DeviceSpec:
    return {"type": "entry_exit_counter", "role": role}


def _energy(role: str = "main", submeter: str = "main") -> DeviceSpec:
    return {"type": "energy_meter", "role": role, "metadata": {"submeter": submeter}}


def _hvac(role: str = "hvac") -> DeviceSpec:
    # HVAC virtual point is catalog-only today (no simulator). Included
    # so the recommendation engine can already plan for it; the
    # auto-provisioner will skip non-implemented types until P11.
    return {"type": "hvac", "role": role}


# Base intent × richness table — applies unless a room type overrides.
_BASE: dict[tuple[str, str], list[DeviceSpec]] = {
    # --- comfort ---------------------------------------------------------
    ("comfort", "basic"):    [_iaq("temperature_humidity")],
    ("comfort", "standard"): [_iaq("temperature_humidity")],
    ("comfort", "advanced"): [_iaq("temperature_humidity"), _occ()],

    # --- iaq -------------------------------------------------------------
    ("iaq", "basic"):    [_iaq()],
    ("iaq", "standard"): [_iaq()],
    ("iaq", "advanced"): [_iaq(), _occ()],

    # --- occupancy -------------------------------------------------------
    ("occupancy", "basic"):    [_occ()],
    ("occupancy", "standard"): [_occ(), _door()],
    ("occupancy", "advanced"): [_occ(), _door(), _people()],

    # --- energy ----------------------------------------------------------
    ("energy", "basic"):    [_energy("main", "main")],
    ("energy", "standard"): [_energy("main", "main"), _energy("hvac", "hvac")],
    ("energy", "advanced"): [
        _energy("main", "main"),
        _energy("hvac", "hvac"),
        _energy("lighting", "lighting"),
        _energy("plug", "plug"),
    ],

    # --- hvac_ops --------------------------------------------------------
    ("hvac_ops", "basic"):    [_hvac()],
    ("hvac_ops", "standard"): [_hvac(), _iaq()],
    ("hvac_ops", "advanced"): [_hvac(), _iaq(), _energy("hvac", "hvac")],

    # --- people_flow -----------------------------------------------------
    ("people_flow", "basic"):    [_people()],
    ("people_flow", "standard"): [_people(), _iaq()],
    ("people_flow", "advanced"): [_people(), _iaq(), _occ()],

    # --- guest_room_automation ------------------------------------------
    ("guest_room_automation", "basic"):    [_iaq(), _occ()],
    ("guest_room_automation", "standard"): [_iaq(), _occ(), _door()],
    ("guest_room_automation", "advanced"): [_iaq(), _occ(), _door(), _hvac()],

    # --- retail_footfall -------------------------------------------------
    ("retail_footfall", "basic"):    [_people()],
    ("retail_footfall", "standard"): [_people(), _iaq()],
    ("retail_footfall", "advanced"): [_people(), _iaq(), _occ()],

    # --- compliance_reporting -------------------------------------------
    ("compliance_reporting", "basic"):    [_iaq()],
    ("compliance_reporting", "standard"): [_iaq(), _occ()],
    ("compliance_reporting", "advanced"): [_iaq(), _occ(), _people()],
}

# Room-type-specific overrides. Keyed by ``room_type`` then
# ``(intent, richness)``. A None value means "fall back to base".
# Only override when the room type genuinely changes what should ship
# (e.g. server rooms shouldn't get occupancy + door contacts).
_OVERRIDES: dict[str, dict[tuple[str, str], list[DeviceSpec]]] = {
    "server_room": {
        ("iaq", "basic"):    [_iaq("temperature_humidity")],
        ("iaq", "standard"): [_iaq("temperature_humidity")],
        ("iaq", "advanced"): [_iaq("temperature_humidity"), _energy("rack", "plug")],
        ("comfort", "basic"):    [_iaq("temperature_humidity")],
        ("comfort", "standard"): [_iaq("temperature_humidity")],
        ("comfort", "advanced"): [_iaq("temperature_humidity")],
        ("energy", "advanced"): [
            _energy("main", "main"),
            _energy("rack", "plug"),
            _energy("crac", "hvac"),
        ],
    },
    "datacenter_hall": {
        ("iaq", "advanced"): [_iaq("temperature_humidity"), _energy("rack", "plug")],
        ("energy", "advanced"): [
            _energy("main", "main"),
            _energy("rack", "plug"),
            _energy("crac", "hvac"),
        ],
    },
    "mall_entrance": {
        # Footfall is the headline metric here; IAQ secondary.
        ("iaq", "standard"): [_people(), _iaq()],
        ("people_flow", "basic"):    [_people()],
        ("people_flow", "standard"): [_people(), _iaq()],
        ("people_flow", "advanced"): [_people(), _iaq(), _occ()],
    },
    "hotel_guest_room": {
        # Even at "basic" intent=comfort, hotels typically want occupancy
        # for HVAC setbacks. Promote occupancy by default.
        ("comfort", "standard"): [_iaq(), _occ()],
        ("comfort", "advanced"): [_iaq(), _occ(), _door(), _hvac()],
        ("iaq", "standard"):     [_iaq(), _occ()],
        ("iaq", "advanced"):     [_iaq(), _occ(), _door()],
    },
    "warehouse_zone": {
        ("comfort", "standard"): [_iaq("temperature_humidity")],
        ("iaq", "standard"):     [_iaq("temperature_humidity")],
    },
    "parking_area": {
        ("iaq", "basic"):    [_iaq("co_ventilation")],
        ("iaq", "standard"): [_iaq("co_ventilation")],
        ("iaq", "advanced"): [_iaq("co_ventilation"), _people()],
    },
    "restaurant_kitchen": {
        ("iaq", "standard"): [_iaq(), _hvac()],
        ("iaq", "advanced"): [_iaq(), _hvac(), _energy("kitchen", "plug")],
    },
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_intent(intent: str | None) -> str:
    """Snap ``intent`` to the canonical taxonomy. Unknown → DEFAULT_INTENT."""
    if not intent:
        return DEFAULT_INTENT
    intent = str(intent).strip().lower()
    return intent if intent in MONITORING_INTENTS else DEFAULT_INTENT


def normalize_richness(richness: str | None) -> str:
    """Snap ``richness`` to the canonical taxonomy. Unknown → DEFAULT_RICHNESS."""
    if not richness:
        return DEFAULT_RICHNESS
    richness = str(richness).strip().lower()
    return richness if richness in RICHNESS_LEVELS else DEFAULT_RICHNESS


def list_monitoring_intents() -> list[dict[str, str]]:
    """Return [{id, label}] for UI dropdowns."""
    labels = {
        "comfort": "Comfort (temperature/humidity)",
        "iaq": "Indoor Air Quality",
        "occupancy": "Occupancy",
        "energy": "Energy",
        "hvac_ops": "HVAC operation",
        "people_flow": "People flow",
        "guest_room_automation": "Guest room automation",
        "retail_footfall": "Retail footfall",
        "compliance_reporting": "Compliance reporting",
    }
    return [{"id": i, "label": labels[i]} for i in MONITORING_INTENTS]


def list_richness_levels() -> list[dict[str, str]]:
    """Return [{id, label}] for UI dropdowns."""
    labels = {
        "basic": "Basic",
        "standard": "Standard",
        "advanced": "Advanced",
    }
    return [{"id": r, "label": labels[r]} for r in RICHNESS_LEVELS]


def recommend_devices(
    room_type: str | None,
    intent: str | None,
    richness: str | None,
) -> list[DeviceSpec]:
    """Return the recommended device specs for the given combination.

    The result is a *fresh list of fresh dicts* — callers may mutate it
    freely without affecting future calls.
    """
    intent_n = normalize_intent(intent)
    richness_n = normalize_richness(richness)
    rt = (room_type or "").strip().lower() or None

    cell: list[DeviceSpec] | None = None
    if rt and rt in _OVERRIDES:
        cell = _OVERRIDES[rt].get((intent_n, richness_n))
    if cell is None:
        cell = _BASE.get((intent_n, richness_n))
    if cell is None:
        # Should be unreachable because _BASE covers every (intent, richness)
        # cell, but fall back gracefully.
        cell = _BASE[(DEFAULT_INTENT, DEFAULT_RICHNESS)]

    # Deep-copy each spec so callers can mutate.
    return [dict(spec, metadata=dict(spec.get("metadata", {}))) for spec in cell]


def recommend_devices_for_room(room: dict) -> list[DeviceSpec]:
    """Convenience wrapper that reads intent + richness off a room dict.

    The room dict is expected to (optionally) carry
    ``monitoring_intent`` and ``monitoring_richness`` keys. ``room_type``
    is read for override lookups.
    """
    return recommend_devices(
        room_type=room.get("room_type"),
        intent=room.get("monitoring_intent"),
        richness=room.get("monitoring_richness"),
    )


def device_type_summary(specs: Iterable[DeviceSpec]) -> dict[str, int]:
    """Reduce a list of specs to ``{device_type: count}`` for display."""
    out: dict[str, int] = {}
    for s in specs:
        t = str(s.get("type") or "")
        if not t:
            continue
        out[t] = out.get(t, 0) + 1
    return out


__all__ = [
    "DEFAULT_INTENT",
    "DEFAULT_RICHNESS",
    "MONITORING_INTENTS",
    "RICHNESS_LEVELS",
    "device_type_summary",
    "list_monitoring_intents",
    "list_richness_levels",
    "normalize_intent",
    "normalize_richness",
    "recommend_devices",
    "recommend_devices_for_room",
]
