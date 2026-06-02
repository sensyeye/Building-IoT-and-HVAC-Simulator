"""Catalog of supported sensor types for the dashboard "Devices" tab.

The simulator's :func:`simulator.config_loader.load_config` is permissive
about device ``metadata`` — it's just a free-form mapping. The real
constraints live inside each sensor simulator (e.g. ``IAQSensor`` reads
``base_temperature_c``, ``EnergyMeter`` reads ``nominal_kw``, etc.). To
build a sensible "add device" form on the dashboard we centralise those
metadata field hints here.

This module deliberately stays **declarative** — no imports of the
runtime sensor classes — so the dashboard can render the form even if a
particular sensor implementation is broken or absent.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Literal


# ---------------------------------------------------------------------------
# Field & type descriptors
# ---------------------------------------------------------------------------


FieldKind = Literal["number", "integer", "string", "choice", "boolean"]


@dataclass(frozen=True)
class MetadataField:
    """A single metadata key the UI should expose for a sensor type."""

    key: str
    label: str
    kind: FieldKind
    default: Any = None
    required: bool = False
    description: str = ""
    choices: tuple[str, ...] | None = None
    min: float | None = None
    max: float | None = None
    step: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Drop unused keys for compactness.
        if self.choices is None:
            d.pop("choices")
        if self.min is None:
            d.pop("min")
        if self.max is None:
            d.pop("max")
        if self.step is None:
            d.pop("step")
        return d


@dataclass(frozen=True)
class SensorType:
    id: str
    name: str
    description: str
    category: str  # "iaq" | "occupancy" | "energy" | "hvac" | "people"
    implemented: bool
    metadata: tuple[MetadataField, ...] = field(default_factory=tuple)
    # Sensible defaults for outputs / UI sliders. Anything not in
    # ``metadata`` will not be touched by the loader.
    default_interval_seconds: int = 60

    def default_metadata(self) -> dict[str, Any]:
        out: dict[str, Any] = {"interval_seconds": self.default_interval_seconds}
        for f in self.metadata:
            if f.default is not None:
                out[f.key] = f.default
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "implemented": self.implemented,
            "default_interval_seconds": self.default_interval_seconds,
            "metadata": [f.to_dict() for f in self.metadata],
        }


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


_INTERVAL = MetadataField(
    key="interval_seconds",
    label="Publish interval (seconds)",
    kind="integer",
    default=60,
    required=True,
    description="How often the device emits a reading.",
    min=1,
    max=3600,
    step=1,
)


_CATALOG: tuple[SensorType, ...] = (
    SensorType(
        id="iaq",
        name="Indoor Air Quality",
        description="Temperature, humidity, CO₂, VOC, PM2.5/PM10, pressure.",
        category="iaq",
        implemented=True,
        default_interval_seconds=60,
        metadata=(
            _INTERVAL,
            MetadataField(
                "base_temperature_c", "Base temperature (°C)", "number",
                default=22.0, min=-10, max=40, step=0.1,
                description="Baseline temperature before zone heat gains.",
            ),
            MetadataField(
                "base_humidity_pct", "Base humidity (%RH)", "number",
                default=50.0, min=0, max=100, step=0.5,
            ),
        ),
    ),
    SensorType(
        id="entry_exit_counter",
        name="People counter (entry/exit)",
        description="Periodic + cumulative entry/exit counts at a door.",
        category="people",
        implemented=True,
        default_interval_seconds=60,
        metadata=(
            _INTERVAL,
            MetadataField(
                "peak_flow_per_min", "Peak flow (people/min)", "number",
                default=1.0, min=0, max=50, step=0.1,
            ),
            MetadataField(
                "morning_peak_hour", "Morning peak hour (0–23)", "number",
                default=9.0, min=0, max=23, step=0.5,
            ),
            MetadataField(
                "evening_peak_hour", "Evening peak hour (0–23)", "number",
                default=18.0, min=0, max=23, step=0.5,
            ),
            MetadataField(
                "report_net_occupancy", "Publish net_occupancy", "boolean",
                default=True,
                description="When True, include net_occupancy (total_in − total_out) in payload.",
            ),
        ),
    ),
    SensorType(
        id="energy_meter",
        name="Energy meter",
        description="Active/apparent power, energy, PF, frequency.",
        category="energy",
        implemented=True,
        default_interval_seconds=300,
        metadata=(
            _INTERVAL,
            MetadataField(
                "submeter", "Sub-meter role", "choice",
                default="main",
                choices=("main", "hvac", "lighting", "plug"),
                required=True,
            ),
            MetadataField(
                "nominal_kw", "Nominal peak power (kW)", "number",
                default=10.0, min=0.1, max=2000, step=0.1, required=True,
            ),
            MetadataField(
                "base_load_factor", "Base load factor (0–1)", "number",
                default=0.05, min=0, max=1, step=0.01,
                description="Power floor as a fraction of nominal_kw.",
            ),
            MetadataField(
                "voltage_nominal", "Nominal voltage (V)", "number",
                default=230.0, min=80, max=480, step=1,
            ),
            MetadataField(
                "frequency_nominal", "Nominal frequency (Hz)", "number",
                default=50.0, min=45, max=65, step=0.1,
            ),
        ),
    ),
    SensorType(
        id="occupancy_sensor",
        name="Occupancy sensor (PIR)",
        description="PIR-style binary occupancy with hold timer; emits occupancy + occupant_count.",
        category="occupancy",
        implemented=True,
        default_interval_seconds=60,
        metadata=(
            _INTERVAL,
            MetadataField(
                "hold_time_seconds", "Hold time (seconds)", "integer",
                default=300, min=0, max=3600, step=10,
                description="How long the sensor latches True after the last detection.",
            ),
            MetadataField(
                "false_negative_rate", "False-negative rate", "number",
                default=0.01, min=0, max=1, step=0.005,
                description="Probability of missing a real occupant on a single tick.",
            ),
            MetadataField(
                "false_positive_rate", "False-positive rate", "number",
                default=0.002, min=0, max=1, step=0.001,
                description="Probability of a spurious detection in an empty room.",
            ),
            MetadataField(
                "report_count", "Report occupant_count", "boolean",
                default=True,
                description="When False the device only publishes the binary occupancy flag.",
            ),
        ),
    ),
    SensorType(
        id="door_contact",
        name="Door contact (reed switch)",
        description="Magnetic door/window contact; emits door_state + open-event counters.",
        category="occupancy",
        implemented=True,
        default_interval_seconds=60,
        metadata=(
            _INTERVAL,
            MetadataField(
                "base_open_rate_per_hour", "Base open rate (per hour)", "number",
                default=0.2, min=0, max=60, step=0.1,
                description="Opens per hour when the room is unoccupied.",
            ),
            MetadataField(
                "occupied_open_rate_per_hour", "Occupied open rate (per occupant per hour)", "number",
                default=1.5, min=0, max=60, step=0.1,
            ),
            MetadataField(
                "open_duration_seconds_mean", "Mean open duration (s)", "number",
                default=6.0, min=1, max=600, step=0.5,
            ),
            MetadataField(
                "open_duration_seconds_std", "Open duration stddev (s)", "number",
                default=3.0, min=0, max=600, step=0.5,
            ),
            MetadataField(
                "activity_peak_hour", "Activity peak hour (0–23)", "number",
                default=13.0, min=0, max=23, step=0.5,
            ),
            MetadataField(
                "activity_width_hours", "Activity envelope width (hours)", "number",
                default=6.0, min=0.5, max=24, step=0.5,
            ),
            MetadataField(
                "weekend_factor", "Weekend factor (0–1)", "number",
                default=0.2, min=0, max=1, step=0.05,
            ),
        ),
    ),
    SensorType(
        id="hvac",
        name="HVAC virtual point",
        description=(
            "Closed-loop HVAC controller: emits mode/setpoint/supply temp "
            "and drives the room's mechanical ventilation + setpoint."
        ),
        category="hvac",
        implemented=True,
        default_interval_seconds=300,
        metadata=(
            _INTERVAL,
            MetadataField(
                "setpoint_c", "Setpoint (°C)", "number",
                default=22.0, min=10, max=30, step=0.1,
            ),
            MetadataField(
                "ventilation_l_s_per_person", "Design ventilation (L/s · person)",
                "number", default=8.0, min=0, max=30, step=0.5,
            ),
            MetadataField(
                "business_hour_start", "Run window start (hour)", "number",
                default=7, min=0, max=23, step=1,
            ),
            MetadataField(
                "business_hour_end", "Run window end (hour)", "number",
                default=19, min=1, max=24, step=1,
            ),
            MetadataField(
                "run_weekends", "Run on weekends", "boolean", default=False,
            ),
            MetadataField(
                "mode_override", "Force mode (optional)", "choice",
                default="", choices=("", "auto", "cool", "heat", "fan_only", "off", "standby"),
            ),
        ),
    ),
)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def list_sensor_types() -> list[SensorType]:
    """All sensor types known to the dashboard."""
    return list(_CATALOG)


def known_sensor_types() -> set[str]:
    return {s.id for s in _CATALOG}


def get_sensor_type(sensor_id: str) -> SensorType | None:
    for s in _CATALOG:
        if s.id == sensor_id:
            return s
    return None


# Compact human-friendly tags used when auto-naming bulk-created devices.
# Kept here next to the catalog so adding a new sensor type only needs
# one entry to roll out everywhere.
_SHORT_NAMES: dict[str, str] = {
    "iaq": "IAQ",
    "entry_exit_counter": "People",
    "energy_meter": "Energy",
    "occupancy_sensor": "Occupancy",
    "door_contact": "Door",
    "hvac": "HVAC",
}


def short_name_for(sensor_id: str) -> str:
    """Return a short tag like ``"IAQ"`` for use in auto-generated names."""
    return _SHORT_NAMES.get(sensor_id, sensor_id.upper())


__all__ = [
    "MetadataField",
    "SensorType",
    "get_sensor_type",
    "known_sensor_types",
    "list_sensor_types",
    "short_name_for",
]
