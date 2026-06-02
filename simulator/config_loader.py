"""YAML configuration loader for the Sensgreen Sensor Simulator.

Parses YAML files into typed `SimulatorConfig` dataclasses and validates
required fields with clear error messages. No simulation logic here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models.config import (
    BuildingConfig,
    CSVOutputConfig,
    DeviceConfig,
    HVACZoneConfig,
    MQTTOutputConfig,
    OutputsConfig,
    SimulationConfig,
    SimulatorConfig,
    ZoneConfig,
)


class ConfigError(ValueError):
    """Raised when a configuration file is missing required fields or invalid."""


# --- helpers ------------------------------------------------------------------

def _require(d: Any, key: str, where: str) -> Any:
    if not isinstance(d, dict):
        raise ConfigError(f"Expected mapping at '{where}', got {type(d).__name__}")
    if key not in d or d[key] is None:
        raise ConfigError(f"Missing required field '{key}' at '{where}'")
    return d[key]


def _optional(d: Any, key: str, default: Any = None) -> Any:
    if not isinstance(d, dict):
        return default
    return d.get(key, default)


# --- section parsers ----------------------------------------------------------

def _parse_zone(raw: dict[str, Any], idx: int) -> ZoneConfig:
    where = f"building.zones[{idx}]"
    def _opt_str(key: str) -> str | None:
        val = _optional(raw, key)
        if val is None or val == "":
            return None
        return str(val)

    return ZoneConfig(
        id=str(_require(raw, "id", where)),
        name=str(_require(raw, "name", where)),
        area_m2=_optional(raw, "area_m2"),
        capacity=_optional(raw, "capacity"),
        room_type=_opt_str("room_type"),
        floor_id=_opt_str("floor_id"),
        exposure=_opt_str("exposure"),
        ventilation_quality=_opt_str("ventilation_quality"),
        infiltration_level=_opt_str("infiltration_level"),
        hvac_zone_id=_opt_str("hvac_zone_id"),
        monitoring_profile=_opt_str("monitoring_profile"),
        metadata=_optional(raw, "metadata", {}) or {},
    )


def _parse_building(raw: dict[str, Any]) -> BuildingConfig:
    where = "building"
    zones_raw = _optional(raw, "zones", []) or []
    if not isinstance(zones_raw, list):
        raise ConfigError("'building.zones' must be a list")
    zones = [_parse_zone(z, i) for i, z in enumerate(zones_raw)]

    hvac_raw = _optional(raw, "hvac_zones", []) or []
    if not isinstance(hvac_raw, list):
        raise ConfigError("'building.hvac_zones' must be a list")
    hvac_zones = [_parse_hvac_zone(h, i) for i, h in enumerate(hvac_raw)]
    # Each room's hvac_zone_id (if set) must reference a defined HVAC zone.
    hvac_ids = {h.id for h in hvac_zones}
    for z in zones:
        if z.hvac_zone_id and z.hvac_zone_id not in hvac_ids:
            raise ConfigError(
                f"building.zones[id={z.id!r}].hvac_zone_id={z.hvac_zone_id!r} "
                f"does not match any building.hvac_zones[].id"
            )

    return BuildingConfig(
        id=str(_require(raw, "id", where)),
        name=str(_require(raw, "name", where)),
        timezone=str(_optional(raw, "timezone", "UTC")),
        zones=zones,
        hvac_zones=hvac_zones,
    )


def _parse_hvac_zone(raw: dict[str, Any], idx: int) -> HVACZoneConfig:
    where = f"building.hvac_zones[{idx}]"

    def _opt_str(key: str) -> str | None:
        val = _optional(raw, key)
        if val is None or val == "":
            return None
        return str(val)

    def _opt_float(key: str) -> float | None:
        val = _optional(raw, key)
        if val is None or val == "":
            return None
        try:
            return float(val)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{where}.{key} must be a number") from exc

    return HVACZoneConfig(
        id=str(_require(raw, "id", where)),
        name=str(_require(raw, "name", where)),
        system_type=_opt_str("system_type"),
        system_id=_opt_str("system_id"),
        setpoint_c=_opt_float("setpoint_c"),
        capacity_kw=_opt_float("capacity_kw"),
        metadata=_optional(raw, "metadata", {}) or {},
    )


def _parse_device(raw: dict[str, Any], idx: int, zone_ids: set[str]) -> DeviceConfig:
    where = f"devices[{idx}]"
    zone_id = str(_require(raw, "zone_id", where))
    if zone_id not in zone_ids:
        raise ConfigError(
            f"Device at '{where}' references unknown zone_id='{zone_id}'. "
            f"Known zones: {sorted(zone_ids) or '[]'}"
        )
    return DeviceConfig(
        device_eui=str(_require(raw, "device_eui", where)),
        name=str(_require(raw, "name", where)),
        type=str(_require(raw, "type", where)),
        zone_id=zone_id,
        metadata=_optional(raw, "metadata", {}) or {},
    )


def _parse_outputs(raw: dict[str, Any] | None) -> OutputsConfig:
    raw = raw or {}
    mqtt_raw = raw.get("mqtt") or {}
    csv_raw = raw.get("csv") or {}

    mqtt = MQTTOutputConfig(
        enabled=bool(mqtt_raw.get("enabled", False)),
        host=str(mqtt_raw.get("host", "")),
        port=int(mqtt_raw.get("port", 8883)),
        username=mqtt_raw.get("username"),
        password=mqtt_raw.get("password"),
        client_id=str(mqtt_raw.get("client_id", "sensgreen-simulator")),
        tls=bool(mqtt_raw.get("tls", True)),
        topic_template=str(mqtt_raw.get("topic_template", "sensgreen/{device_eui}")),
        error_topic=(
            str(mqtt_raw["error_topic"]) if mqtt_raw.get("error_topic") else None
        ),
    )
    if mqtt.enabled and not mqtt.host:
        raise ConfigError("outputs.mqtt.enabled is true but 'host' is missing")

    csv = CSVOutputConfig(
        enabled=bool(csv_raw.get("enabled", False)),
        output_dir=str(csv_raw.get("output_dir", "outputs")),
        filename=str(csv_raw.get("filename", "readings_long.csv")),
    )
    return OutputsConfig(mqtt=mqtt, csv=csv)


def _parse_simulation(raw: dict[str, Any] | None) -> SimulationConfig:
    raw = raw or {}
    mode = str(raw.get("mode", "live"))
    if mode not in ("live", "historical"):
        raise ConfigError(
            f"simulation.mode must be 'live' or 'historical', got '{mode}'"
        )
    interval = int(raw.get("interval_seconds", 60))
    if interval <= 0:
        raise ConfigError("simulation.interval_seconds must be > 0")

    start = raw.get("start")
    end = raw.get("end")
    if mode == "historical":
        if not start or not end:
            raise ConfigError(
                "simulation.start and simulation.end are required when mode='historical'"
            )

    return SimulationConfig(
        mode=mode,  # type: ignore[arg-type]
        interval_seconds=interval,
        start=start,
        end=end,
        seed=raw.get("seed"),
    )


# --- public API ---------------------------------------------------------------

def load_config(path: str | Path) -> SimulatorConfig:
    """Load and validate a YAML config file."""
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Config file not found: {p}")

    try:
        with p.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {p}: {e}") from e

    if not isinstance(raw, dict):
        raise ConfigError(f"Top-level config in {p} must be a mapping")

    building_raw = _require(raw, "building", "<root>")
    building = _parse_building(building_raw)
    zone_ids = {z.id for z in building.zones}

    devices_raw = _require(raw, "devices", "<root>")
    if not isinstance(devices_raw, list) or not devices_raw:
        raise ConfigError("'devices' must be a non-empty list")
    devices = [_parse_device(d, i, zone_ids) for i, d in enumerate(devices_raw)]

    outputs = _parse_outputs(raw.get("outputs"))
    simulation = _parse_simulation(raw.get("simulation"))

    return SimulatorConfig(
        building=building,
        devices=devices,
        outputs=outputs,
        simulation=simulation,
    )
