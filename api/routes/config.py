"""Project-managed simulator config (YAML) + sensor catalog endpoints.

Routes
------
* ``GET    /api/sensor-types``                         — sensor catalog
* ``GET    /api/projects/{id}/config``                 — full managed config
* ``PUT    /api/projects/{id}/config``                 — replace config
* ``POST   /api/projects/{id}/zones``                  — add a zone
* ``DELETE /api/projects/{id}/zones/{zone_id}``        — remove a zone
* ``POST   /api/projects/{id}/devices``                — add a device
* ``PUT    /api/projects/{id}/devices/{device_eui}``   — update a device
* ``DELETE /api/projects/{id}/devices/{device_eui}``   — remove a device
* ``POST   /api/projects/{id}/devices/generate-eui``   — deterministic EUI

This is the dashboard's *write* path for the simulator config. Live and
Bridge Test routes read the same file when the user leaves the config
path empty.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from api.services.event_service import event_service
from api.services.project_service import project_service
from simulator.catalogs import compute_zone_coverage, infer_room_type
from simulator.config_loader import ConfigError, load_config
from simulator.devices import get_sensor_type, list_sensor_types
from simulator.layout import LayoutGenerationError, LayoutSpec, generate_layout
from simulator.utils.eui import generate_eui, is_lorawan_eui, normalize_eui


router = APIRouter()
_log = logging.getLogger("sensgreen.api.config")


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------


class ZonePayload(BaseModel):
    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    area_m2: float | None = None
    capacity: int | None = None
    # Optional room-level metadata (Phase 1 of the room/HVAC/monitoring
    # architecture). All fields are optional so legacy clients keep working.
    room_type: str | None = None
    floor_id: str | None = None
    exposure: str | None = None
    ventilation_quality: str | None = None
    infiltration_level: str | None = None
    hvac_zone_id: str | None = None
    monitoring_profile: str | None = None
    monitoring_intent: str | None = None
    monitoring_richness: str | None = None
    metadata: dict[str, Any] | None = None


class HVACZonePayload(BaseModel):
    id: str = Field(..., min_length=1)
    name: str | None = None
    system_type: str | None = None
    system_id: str | None = None
    setpoint_c: float | None = None
    capacity_kw: float | None = None
    metadata: dict[str, Any] | None = None


class DevicePayload(BaseModel):
    device_eui: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    type: str = Field(..., min_length=1)
    zone_id: str = Field(..., min_length=1)
    metadata: dict[str, Any] | None = None


class DeviceUpdatePayload(BaseModel):
    name: str | None = None
    type: str | None = None
    zone_id: str | None = None
    metadata: dict[str, Any] | None = None


class GenerateEuiPayload(BaseModel):
    name: str = Field(..., min_length=1)
    oui: str | None = None


class BulkDeviceItem(BaseModel):
    type: str = Field(..., min_length=1)
    count: int = Field(..., ge=1, le=1000)
    zone_id: str | None = None
    metadata: dict[str, Any] | None = None


class BulkDevicesPayload(BaseModel):
    items: list[BulkDeviceItem] = Field(..., min_length=1)
    zone_strategy: str = Field("round_robin")
    name_prefix: str | None = None


class AutoProvisionPayload(BaseModel):
    dry_run: bool = True
    overwrite: bool = False


class ConfigReplacePayload(BaseModel):
    # Free-form: validated by load_config in the service layer.
    config: dict[str, Any]


class ConfigYamlPayload(BaseModel):
    yaml: str = Field(..., min_length=1)


class GenerateLayoutPayload(BaseModel):
    archetype_id: str = Field(..., min_length=1)
    total_area_m2: float = Field(..., gt=0)
    floors: int = Field(1, ge=1, le=50)
    seed: int | None = None
    # When False (default), only preview the proposed layout. When True,
    # replace the project's zones with the generated list (atomic write).
    apply: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_project(project_id: str) -> None:
    if project_service.get(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")


def _record(
    project_id: str, kind: str, status_: str, summary: str, **details: Any
) -> None:
    try:
        event_service.record(
            project_id,
            kind=kind,
            status=status_,
            summary=summary,
            details=details or {},
        )
    except Exception:  # pragma: no cover - defensive
        _log.exception("failed to record event")


def _annotate_zones_inference(cfg: dict[str, Any]) -> dict[str, Any]:
    """Fill in ``room_type`` (and a ``room_type_inferred`` flag) on every zone
    in ``cfg`` that doesn't have one set on disk.

    This mutates and returns the same dict for convenience. Disk state is
    NOT touched — callers run this only on the response payload so legacy
    configs surface a usable room_type without silently rewriting YAML.
    """
    building = cfg.get("building") if isinstance(cfg, dict) else None
    if not isinstance(building, dict):
        return cfg
    zones = building.get("zones")
    if not isinstance(zones, list):
        return cfg
    devices = cfg.get("devices") if isinstance(cfg.get("devices"), list) else []
    coverage_rollup = {"ok": 0, "partial": 0, "missing": 0, "no_profile": 0}
    for zone in zones:
        if not isinstance(zone, dict):
            continue
        existing = zone.get("room_type")
        if isinstance(existing, str) and existing.strip():
            zone["room_type_inferred"] = False
        else:
            inferred_id, _ = infer_room_type(
                str(zone.get("name") or ""), str(zone.get("id") or "")
            )
            zone["room_type"] = inferred_id
            zone["room_type_inferred"] = True
        # Compute monitoring coverage for this zone (uses possibly inferred
        # room_type to fall back when no explicit profile is set).
        coverage = compute_zone_coverage(zone, devices)
        zone["monitoring_coverage"] = coverage
        coverage_rollup[coverage["status"]] = (
            coverage_rollup.get(coverage["status"], 0) + 1
        )
    cfg.setdefault("_annotations", {})["monitoring_coverage_summary"] = coverage_rollup
    # ------------------------------------------------------------------
    # HVAC zone roll-up (Phase 3): served_room_count per declared zone +
    # orphan list (declared but with zero rooms) + unknown_hvac_zone_refs
    # (zones whose hvac_zone_id doesn't match any declared HVAC zone).
    # ------------------------------------------------------------------
    hvac_zones = building.get("hvac_zones")
    declared_ids: set[str] = set()
    served_counts: dict[str, int] = {}
    if isinstance(hvac_zones, list):
        for hz in hvac_zones:
            if not isinstance(hz, dict):
                continue
            hid = str(hz.get("id") or "").strip()
            if not hid:
                continue
            declared_ids.add(hid)
            served_counts.setdefault(hid, 0)
    referenced: dict[str, int] = {}
    for zone in zones:
        if not isinstance(zone, dict):
            continue
        ref = zone.get("hvac_zone_id")
        if not isinstance(ref, str) or not ref.strip():
            continue
        ref = ref.strip()
        referenced[ref] = referenced.get(ref, 0) + 1
        if ref in served_counts:
            served_counts[ref] += 1
    if isinstance(hvac_zones, list):
        for hz in hvac_zones:
            if not isinstance(hz, dict):
                continue
            hid = str(hz.get("id") or "").strip()
            if hid:
                hz["served_room_count"] = served_counts.get(hid, 0)
    orphan = sorted(hid for hid, n in served_counts.items() if n == 0)
    unknown = sorted(rid for rid in referenced if rid not in declared_ids)
    cfg["_annotations"]["hvac_summary"] = {
        "declared": sorted(declared_ids),
        "served_counts": served_counts,
        "orphan_hvac_zones": orphan,
        "unknown_hvac_zone_refs": unknown,
    }
    return cfg


def _config_meta(project_id: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Run the config through ``load_config`` to surface validation issues."""
    path = project_service.managed_config_path(project_id)
    meta: dict[str, Any] = {
        "managed_path": str(path),
        "exists": path.exists(),
        "valid": False,
        "errors": [],
    }
    if not path.exists():
        meta["errors"].append(
            "no managed config yet — add at least one zone and one device."
        )
        return meta
    try:
        loaded = load_config(path)
        meta["valid"] = True
        meta["device_count"] = len(loaded.devices)
        meta["zone_count"] = len(loaded.building.zones)
    except ConfigError as exc:
        meta["errors"].append(str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        meta["errors"].append(f"unexpected: {exc}")
    return meta


# ---------------------------------------------------------------------------
# Sensor catalog
# ---------------------------------------------------------------------------


@router.get("/sensor-types")
def get_sensor_types() -> dict[str, Any]:
    return {"sensor_types": [s.to_dict() for s in list_sensor_types()]}


# ---------------------------------------------------------------------------
# Whole-config read / replace
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/config")
def get_project_config(project_id: str) -> dict[str, Any]:
    _require_project(project_id)
    cfg = project_service.get_config(project_id)
    meta = _config_meta(project_id, cfg)
    _annotate_zones_inference(cfg)
    return {"config": cfg, "meta": meta}


@router.put("/projects/{project_id}/config")
def put_project_config(
    project_id: str, payload: ConfigReplacePayload
) -> dict[str, Any]:
    _require_project(project_id)
    try:
        project_service.set_config(project_id, payload.config, validate=True)
    except (ValueError, ConfigError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    cfg = project_service.get_config(project_id)
    meta = _config_meta(project_id, cfg)
    _annotate_zones_inference(cfg)
    _record(project_id, "config", "info", "managed config replaced")
    return {"config": cfg, "meta": meta}


@router.get("/projects/{project_id}/config/yaml", response_class=PlainTextResponse)
def get_project_config_yaml(project_id: str) -> str:
    """Return the managed config as raw YAML text (for the in-app editor)."""
    _require_project(project_id)
    return project_service.get_config_yaml(project_id)


@router.put("/projects/{project_id}/config/yaml")
def put_project_config_yaml(
    project_id: str, payload: ConfigYamlPayload
) -> dict[str, Any]:
    """Replace the managed config from raw YAML text.

    Validation is mandatory here — the editor should refuse to save a
    config that would later fail at run time. The error response
    surfaces the loader's message so the UI can show *which* field is
    broken.
    """
    _require_project(project_id)
    try:
        project_service.set_config_yaml(project_id, payload.yaml, validate=True)
    except (ValueError, ConfigError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    cfg = project_service.get_config(project_id)
    meta = _config_meta(project_id, cfg)
    _annotate_zones_inference(cfg)
    _record(project_id, "config", "info", "managed config edited via YAML editor")
    return {
        "config": cfg,
        "yaml": project_service.get_config_yaml(project_id),
        "meta": meta,
    }


# ---------------------------------------------------------------------------
# Zone CRUD
# ---------------------------------------------------------------------------


@router.post("/projects/{project_id}/zones", status_code=status.HTTP_201_CREATED)
def add_zone(project_id: str, payload: ZonePayload) -> dict[str, Any]:
    _require_project(project_id)
    try:
        cfg = project_service.add_zone(project_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _record(project_id, "config", "info", f"zone added: {payload.id}")
    return {"config": cfg, "meta": _config_meta(project_id, cfg)}


@router.put("/projects/{project_id}/zones/{zone_id}")
def update_zone(
    project_id: str, zone_id: str, payload: ZonePayload
) -> dict[str, Any]:
    _require_project(project_id)
    try:
        # The path id wins; ZonePayload requires an id but we ignore it.
        data = payload.model_dump()
        data["id"] = zone_id
        cfg = project_service.update_zone(project_id, zone_id, data)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _record(project_id, "config", "info", f"zone updated: {zone_id}")
    return {"config": cfg, "meta": _config_meta(project_id, cfg)}


@router.delete("/projects/{project_id}/zones/{zone_id}")
def remove_zone(project_id: str, zone_id: str) -> dict[str, Any]:
    _require_project(project_id)
    try:
        cfg = project_service.remove_zone(project_id, zone_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _record(project_id, "config", "info", f"zone removed: {zone_id}")
    return {"config": cfg, "meta": _config_meta(project_id, cfg)}


# ---------------------------------------------------------------------------
# HVAC zone CRUD
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{project_id}/hvac-zones", status_code=status.HTTP_201_CREATED
)
def add_hvac_zone(project_id: str, payload: HVACZonePayload) -> dict[str, Any]:
    _require_project(project_id)
    try:
        cfg = project_service.add_hvac_zone(project_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _record(project_id, "config", "info", f"hvac zone added: {payload.id}")
    return {"config": cfg, "meta": _config_meta(project_id, cfg)}


@router.delete("/projects/{project_id}/hvac-zones/{hvac_zone_id}")
def remove_hvac_zone(project_id: str, hvac_zone_id: str) -> dict[str, Any]:
    _require_project(project_id)
    try:
        cfg = project_service.remove_hvac_zone(project_id, hvac_zone_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _record(project_id, "config", "info", f"hvac zone removed: {hvac_zone_id}")
    return {"config": cfg, "meta": _config_meta(project_id, cfg)}


@router.post("/projects/{project_id}/generate-layout")
def generate_project_layout(
    project_id: str, payload: GenerateLayoutPayload
) -> dict[str, Any]:
    """Generate a deterministic zone layout from a building archetype.

    When ``apply`` is False (default), the response only contains the
    proposed ``layout`` — no disk writes. The UI uses this for a preview.
    When ``apply`` is True, the generated zones replace ``building.zones``;
    devices that would be orphaned (zone_id no longer exists) cause a 400
    so the caller can reassign or clear them first.
    """
    _require_project(project_id)
    try:
        spec = LayoutSpec(
            archetype_id=payload.archetype_id,
            total_area_m2=float(payload.total_area_m2),
            floors=int(payload.floors),
            seed=payload.seed,
        )
        layout = generate_layout(spec)
    except LayoutGenerationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    response: dict[str, Any] = {"layout": layout, "applied": False}

    if payload.apply:
        try:
            cfg = project_service.apply_generated_zones(project_id, layout["zones"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        response["applied"] = True
        response["config"] = cfg
        response["meta"] = _config_meta(project_id, cfg)
        _record(
            project_id,
            "config",
            "info",
            f"layout generated from archetype {payload.archetype_id}: "
            f"{layout['summary']['zone_count']} zones",
            archetype_id=payload.archetype_id,
            zone_count=layout["summary"]["zone_count"],
        )

    return response


# ---------------------------------------------------------------------------
# Device CRUD
# ---------------------------------------------------------------------------


def _validate_device_type(type_: str) -> None:
    if get_sensor_type(type_) is None:
        raise HTTPException(
            status_code=400,
            detail=f"unknown sensor type: {type_!r}. "
                   "GET /api/sensor-types for the catalog.",
        )


def _validate_device_eui(eui: str) -> str:
    try:
        return normalize_eui(eui)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/projects/{project_id}/devices", status_code=status.HTTP_201_CREATED
)
def add_device(project_id: str, payload: DevicePayload) -> dict[str, Any]:
    _require_project(project_id)
    _validate_device_type(payload.type)
    eui = _validate_device_eui(payload.device_eui)

    body = payload.model_dump()
    body["device_eui"] = eui
    try:
        cfg = project_service.add_device(project_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=f"config error: {exc}") from exc
    _record(project_id, "config", "info", f"device added: {eui}", type=payload.type)
    return {"config": cfg, "meta": _config_meta(project_id, cfg)}


@router.put("/projects/{project_id}/devices/{device_eui}")
def update_device(
    project_id: str, device_eui: str, payload: DeviceUpdatePayload
) -> dict[str, Any]:
    _require_project(project_id)
    if payload.type is not None:
        _validate_device_type(payload.type)
    eui = _validate_device_eui(device_eui)
    diff = {k: v for k, v in payload.model_dump().items() if v is not None}
    try:
        cfg = project_service.update_device(project_id, eui, diff)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=f"config error: {exc}") from exc
    _record(project_id, "config", "info", f"device updated: {eui}")
    return {"config": cfg, "meta": _config_meta(project_id, cfg)}


@router.delete("/projects/{project_id}/devices/{device_eui}")
def remove_device(project_id: str, device_eui: str) -> dict[str, Any]:
    _require_project(project_id)
    eui = _validate_device_eui(device_eui)
    try:
        cfg = project_service.remove_device(project_id, eui)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _record(project_id, "config", "info", f"device removed: {eui}")
    return {"config": cfg, "meta": _config_meta(project_id, cfg)}


@router.post("/projects/{project_id}/devices/generate-eui")
def generate_eui_for_project(
    project_id: str, payload: GenerateEuiPayload
) -> dict[str, Any]:
    _require_project(project_id)
    try:
        eui = generate_eui(project_id, payload.name, oui=payload.oui)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    assert is_lorawan_eui(eui)
    return {"device_eui": eui}


@router.post(
    "/projects/{project_id}/devices/bulk",
    status_code=status.HTTP_201_CREATED,
)
def bulk_add_devices(
    project_id: str, payload: BulkDevicesPayload
) -> dict[str, Any]:
    """Create many devices in one shot with smart placement and naming.

    The user specifies a per-type count and (optionally) a target zone
    or sensor-metadata override. The service distributes the devices
    across zones according to ``zone_strategy`` and generates names +
    deterministic EUIs.
    """
    _require_project(project_id)
    # Validate types up-front so the user gets a clear 400 instead of a
    # vague config-loader error after partial work.
    for item in payload.items:
        if get_sensor_type(item.type) is None:
            raise HTTPException(
                status_code=400,
                detail=f"unknown sensor type: {item.type!r}",
            )
    try:
        result = project_service.bulk_add_devices(
            project_id,
            [item.model_dump() for item in payload.items],
            zone_strategy=payload.zone_strategy,
            name_prefix=payload.name_prefix,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=f"config error: {exc}") from exc
    created = result["created"]
    cfg = result["config"]
    _record(
        project_id,
        "config",
        "info",
        f"bulk added {len(created)} device(s)",
        zone_strategy=payload.zone_strategy,
        types={i.type: i.count for i in payload.items},
    )
    return {
        "config": cfg,
        "created": created,
        "meta": _config_meta(project_id, cfg),
    }


@router.post("/projects/{project_id}/devices/auto-provision")
def auto_provision_devices(
    project_id: str, payload: AutoProvisionPayload
) -> dict[str, Any]:
    """Stage (or commit) devices for every zone based on its monitoring
    profile's required sensor types.

    With ``dry_run=true`` (default) the response only returns the
    planned ``to_add`` + ``skipped`` lists; nothing is written. Set
    ``dry_run=false`` to persist.
    """
    _require_project(project_id)
    try:
        result = project_service.auto_provision_devices(
            project_id, dry_run=payload.dry_run, overwrite=payload.overwrite
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=f"config error: {exc}") from exc

    response: dict[str, Any] = {
        "to_add": result["to_add"],
        "skipped": result["skipped"],
        "dry_run": result["dry_run"],
    }
    if "config" in result:
        cfg = result["config"]
        response["config"] = cfg
        response["meta"] = _config_meta(project_id, cfg)
        _record(
            project_id,
            "config",
            "info",
            f"auto-provisioned {len(result['to_add'])} device(s)",
            overwrite=payload.overwrite,
        )
    return response
