"""Typed configuration dataclasses for the Sensgreen Sensor Simulator.

These mirror the structure of YAML config files (see `configs/demo_office.yaml`).
Only structure + light validation lives here; no simulation logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class DeviceConfig:
    device_eui: str
    name: str
    type: str  # e.g. "iaq", "energy_meter", "occupancy", "people_counter", "hvac", "device_health"
    zone_id: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ZoneConfig:
    id: str
    name: str
    area_m2: float | None = None
    capacity: int | None = None
    # Optional room-level metadata introduced with the room/HVAC/monitoring
    # architecture. All fields are optional so legacy configs keep loading;
    # if ``room_type`` is unset, the API surfaces an inferred fallback and a
    # ``room_type_inferred`` flag so the UI can prompt for review.
    room_type: str | None = None
    floor_id: str | None = None
    exposure: str | None = None  # e.g. "interior", "north", "south_facade"
    ventilation_quality: str | None = None  # "low" | "medium" | "high"
    infiltration_level: str | None = None  # "low" | "medium" | "high"
    hvac_zone_id: str | None = None
    monitoring_profile: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HVACZoneConfig:
    """Defines an HVAC zone served by a single AHU/FCU/VRF/etc.

    A room (``ZoneConfig``) references one of these via ``hvac_zone_id``.
    Multiple rooms typically share one HVAC zone, so a fault on the unit
    affects every room it serves — that is how scenario targeting fans
    out from a single ``hvac_zone_id`` to multiple downstream rooms.
    """

    id: str
    name: str
    system_type: str | None = None  # "ahu" | "fcu" | "vrf" | "split" | "chiller" | "none"
    system_id: str | None = None  # external label grouping multiple zones under one plant
    setpoint_c: float | None = None
    capacity_kw: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BuildingConfig:
    id: str
    name: str
    timezone: str = "UTC"
    zones: list[ZoneConfig] = field(default_factory=list)
    hvac_zones: list[HVACZoneConfig] = field(default_factory=list)


@dataclass(frozen=True)
class MQTTOutputConfig:
    enabled: bool = False
    host: str = ""
    port: int = 8883
    username: str | None = None
    password: str | None = None
    client_id: str = "sensgreen-simulator"
    tls: bool = True
    # Two supported topic styles:
    #   * Building-scoped (Sensgreen native): a literal string shared by
    #     every device of the project, e.g. ``sensor/data/925255``.
    #     The Sensgreen-assigned deviceEui lives inside the JSON body.
    #   * Per-device: a template containing ``{device_eui}``, used for
    #     non-Sensgreen brokers or local testing.
    topic_template: str = "sensgreen/{device_eui}"
    # Optional Sensgreen "error" topic — when set, the publisher
    # subscribes to it on connect and surfaces rejected uplinks.
    error_topic: str | None = None


@dataclass(frozen=True)
class CSVOutputConfig:
    enabled: bool = False
    output_dir: str = "outputs"
    filename: str = "readings_long.csv"


@dataclass(frozen=True)
class OutputsConfig:
    mqtt: MQTTOutputConfig = field(default_factory=MQTTOutputConfig)
    csv: CSVOutputConfig = field(default_factory=CSVOutputConfig)


@dataclass(frozen=True)
class SimulationConfig:
    mode: Literal["live", "historical"] = "live"
    interval_seconds: int = 60
    start: str | None = None  # ISO-8601, used in historical mode
    end: str | None = None    # ISO-8601, used in historical mode
    seed: int | None = None


@dataclass(frozen=True)
class SimulatorConfig:
    building: BuildingConfig
    devices: list[DeviceConfig]
    outputs: OutputsConfig
    simulation: SimulationConfig

    def summary(self) -> str:
        zone_count = len(self.building.zones)
        device_count = len(self.devices)
        outs: list[str] = []
        if self.outputs.mqtt.enabled:
            outs.append(f"mqtt({self.outputs.mqtt.host}:{self.outputs.mqtt.port})")
        if self.outputs.csv.enabled:
            outs.append(f"csv({self.outputs.csv.output_dir}/{self.outputs.csv.filename})")
        outputs_str = ", ".join(outs) if outs else "none"
        return (
            f"Building: {self.building.name} (id={self.building.id}, tz={self.building.timezone})\n"
            f"Zones: {zone_count}\n"
            f"Devices: {device_count}\n"
            f"Mode: {self.simulation.mode} "
            f"(interval={self.simulation.interval_seconds}s, "
            f"start={self.simulation.start}, end={self.simulation.end})\n"
            f"Outputs: {outputs_str}"
        )
