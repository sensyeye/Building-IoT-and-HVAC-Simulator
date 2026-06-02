"""Maps internal simulator metric names to Sensgreen-supported metric IDs.

The simulator uses descriptive, sometimes unit-tagged internal names
(e.g. ``active_power_kw``). The Sensgreen platform uses canonical metric
IDs (e.g. ``active_power``). This module is the single source of truth
for translating between the two.

Output adapters (MQTT, CSV) must run their data dicts through this
mapper before publishing — sensor logic must not embed Sensgreen names
directly.
"""

from __future__ import annotations

from typing import Any


class UnknownMetricError(KeyError):
    """Raised in strict mode when an internal metric has no Sensgreen mapping."""


class UnsupportedSensorTypeError(KeyError):
    """Raised when the requested sensor_type has no mapping table."""


# ---------------------------------------------------------------------------
# Per-sensor-type mappings: internal_name -> Sensgreen metric id.
#
# All right-hand-side values must exist in the Sensgreen metrics catalog.
# Keep this table the single source of truth; do not hardcode Sensgreen
# names elsewhere.
# ---------------------------------------------------------------------------

_IAQ_MAP: dict[str, str] = {
    "temperature": "temperature",
    "humidity": "humidity",
    "pressure": "pressure",
    "co2": "co2",
    "voc": "voc",
    "tvoc": "voc",
    "pm1": "pm1",
    "pm25": "pm25",
    "pm2_5": "pm25",
    "pm4": "pm4",
    "pm10": "pm10",
    "dew_point": "dew_point",
    "iaq": "iaq",
    "aqi": "iaq",
    "hcho": "hcho",
    "formaldehyde": "formaldehyde",
    "no2": "no2",
    "co": "co",
    "o3": "o3",
    "sound_level": "sound_level",
    "luminosity": "luminosity",
    "light_level": "light_level",
}

_ENERGY_METER_MAP: dict[str, str] = {
    # Power
    "active_power_kw": "active_power",
    "active_power": "active_power",
    "reactive_power_kvar": "reactive_power",
    "reactive_power": "reactive_power",
    "apparent_power_kva": "apparent_power",
    "apparent_power": "apparent_power",
    "active_power_total_kw": "active_power_total",
    "active_power_total": "active_power_total",
    "reactive_power_total_kvar": "reactive_power_total",
    "reactive_power_total": "reactive_power_total",
    "apparent_power_total_kva": "apparent_power_total",
    "apparent_power_total": "apparent_power_total",

    # Energy
    "active_energy_kwh": "active_energy",
    "active_energy": "active_energy",
    "reactive_energy_kvarh": "reactive_energy",
    "reactive_energy": "reactive_energy",
    "energy_reading_kwh": "energy_reading",
    "energy_reading": "energy_reading",

    # Per-phase voltages / currents (internal "_l1/2/3" -> Sensgreen "_1/2/3")
    "voltage_l1": "voltage_1",
    "voltage_l2": "voltage_2",
    "voltage_l3": "voltage_3",
    "current_l1": "current_1",
    "current_l2": "current_2",
    "current_l3": "current_3",

    # RMS variants (some meters expose these)
    "voltage_rms_l1": "voltage_rms_1",
    "voltage_rms_l2": "voltage_rms_2",
    "voltage_rms_l3": "voltage_rms_3",
    "current_rms_l1": "current_rms_1",
    "current_rms_l2": "current_rms_2",
    "current_rms_l3": "current_rms_3",

    # Line-to-line
    "voltage_l1_l2": "voltage_ab",
    "voltage_l2_l3": "voltage_bc",
    "voltage_l3_l1": "voltage_ca",

    # Power factors / frequency
    "power_factor": "power_factor",
    "power_factor_l1": "power_factor_1",
    "power_factor_l2": "power_factor_2",
    "power_factor_l3": "power_factor_3",
    "frequency_hz": "frequency",
    "frequency": "frequency",
}

_PEOPLE_COUNTER_MAP: dict[str, str] = {
    "people_count": "people_count",
    "people_max": "people_max",
    "occupancy": "occupancy",
    "periodic_people_count": "periodic_people_count",
}

_ENTRY_EXIT_COUNTER_MAP: dict[str, str] = {
    "periodic_in": "periodic_counter_in",
    "periodic_out": "periodic_counter_out",
    "total_in": "total_counter_in",
    "total_out": "total_counter_out",
    # Pass-throughs in case caller already uses Sensgreen ids
    "periodic_counter_in": "periodic_counter_in",
    "periodic_counter_out": "periodic_counter_out",
    "total_counter_in": "total_counter_in",
    "total_counter_out": "total_counter_out",
    # Computed net (total_in - total_out, clamped at 0). Reuses the
    # ``people_count`` metric id so it lines up with PIR/people-counter
    # dashboards.
    "net_occupancy": "people_count",
}

# Binary PIR-style occupancy sensor. ``occupant_count`` is published
# under Sensgreen's existing ``people_count`` metric id so dashboards
# don't need a new chart.
_OCCUPANCY_SENSOR_MAP: dict[str, str] = {
    "occupancy": "occupancy",
    "occupant_count": "people_count",
}

# Reed-switch style door contact. ``door_state`` rides on the generic
# ``open_status`` metric; counters reuse the people-counter conventions
# so existing analytics pipelines do not need a new metric id.
_DOOR_CONTACT_MAP: dict[str, str] = {
    "door_state": "open_status",
    "open_status": "open_status",
    "periodic_open_events": "periodic_counter_in",
    "total_open_events": "total_counter_in",
}

_HVAC_MAP: dict[str, str] = {
    # Internal metric names emitted by HvacVirtualSimulator (P11.2):
    "mode": "ac_mode",
    "setpoint_c": "setpoint",
    "supply_temp_c": "supply_air_temperature",
    "fan_speed_pct": "fan_speed",
    "valve_open_pct": "valve_position",
    "ventilation_l_s_per_person": "supply_air_flow",
    # Legacy / Sensgreen-side native names (still mapped 1:1):
    "supply_air_temperature": "supply_air_temperature",
    "return_air_temperature": "return_air_temperature",
    "outside_air_temperature": "outside_air_temperature",
    "exhaust_air_temperature": "exhaust_air_temperature",
    "supply_air_humidity": "supply_air_humidity",
    "return_air_humidity": "return_air_humidity",
    "outside_air_humidity": "outside_air_humidity",
    "exhaust_air_humidity": "exhaust_air_humidity",
    "return_air_co2": "return_air_co2",
    "supply_air_pressure": "supply_air_pressure",
    "supply_air_flow": "supply_air_flow",
    "return_air_flow": "return_air_flow",
    "fan_speed": "fan_speed",
    "fan_level": "fan_level",
    "fan_run_status": "fan_run_status",
    "filter_dirty_status": "filter_dirty_status",
    "run_status": "run_status",
    "run_command": "run_command",
    "setpoint": "setpoint",
    "target_temperature": "target_temperature",
    "valve_position": "valve_position",
    "damper_position": "damper_position",
    "cooling_coil_valve_control": "cooling_coil_valve_control",
    "cooling_coil_valve_feedback": "cooling_coil_valve_feedback",
    "ac_mode": "ac_mode",
    "supply_fan_vfd_speed": "supply_fan_vfd_speed",
    "exhaust_fan_vfd_speed": "exhaust_fan_vfd_speed",
}

_SENSOR_MAPS: dict[str, dict[str, str]] = {
    "iaq": _IAQ_MAP,
    "energy_meter": _ENERGY_METER_MAP,
    "people_counter": _PEOPLE_COUNTER_MAP,
    "entry_exit_counter": _ENTRY_EXIT_COUNTER_MAP,
    "occupancy_sensor": _OCCUPANCY_SENSOR_MAP,
    "door_contact": _DOOR_CONTACT_MAP,
    "hvac": _HVAC_MAP,
}


class SensgreenMetricMapper:
    """Map internal metric names to Sensgreen metric IDs.

    Parameters
    ----------
    strict_mode:
        If True, unknown metrics raise :class:`UnknownMetricError`.
        If False (default), unknown metrics are silently dropped.
    """

    def __init__(self, strict_mode: bool = False) -> None:
        self.strict_mode = strict_mode

    # -- introspection -----------------------------------------------------

    @staticmethod
    def supported_sensor_types() -> list[str]:
        return sorted(_SENSOR_MAPS.keys())

    @staticmethod
    def mapping_for(sensor_type: str) -> dict[str, str]:
        try:
            return dict(_SENSOR_MAPS[sensor_type])
        except KeyError as e:
            raise UnsupportedSensorTypeError(
                f"Unsupported sensor_type '{sensor_type}'. "
                f"Known: {SensgreenMetricMapper.supported_sensor_types()}"
            ) from e

    # -- mapping -----------------------------------------------------------

    def map(self, sensor_type: str, data: dict[str, Any]) -> dict[str, Any]:
        """Translate ``data`` keys for ``sensor_type`` to Sensgreen metric ids.

        Values are passed through unchanged. Duplicate target keys (e.g.
        both ``pm25`` and ``pm2_5`` provided) raise ``ValueError``.
        """
        if sensor_type not in _SENSOR_MAPS:
            raise UnsupportedSensorTypeError(
                f"Unsupported sensor_type '{sensor_type}'. "
                f"Known: {self.supported_sensor_types()}"
            )
        if not isinstance(data, dict):
            raise TypeError(f"data must be a dict, got {type(data).__name__}")

        table = _SENSOR_MAPS[sensor_type]
        mapped: dict[str, Any] = {}

        for key, value in data.items():
            target = table.get(key)
            if target is None:
                if self.strict_mode:
                    raise UnknownMetricError(
                        f"Unknown metric '{key}' for sensor_type='{sensor_type}'"
                    )
                # non-strict: drop silently
                continue
            if target in mapped:
                raise ValueError(
                    f"Duplicate Sensgreen metric '{target}' produced while "
                    f"mapping sensor_type='{sensor_type}' (key='{key}')"
                )
            # Coerce booleans to integer 1/0 — Sensgreen's payload format
            # expects numeric values for binary metrics (door open_status,
            # presence/occupancy, etc.) rather than JSON true/false.
            if isinstance(value, bool):
                value = 1 if value else 0
            mapped[target] = value

        return mapped


__all__ = [
    "SensgreenMetricMapper",
    "UnknownMetricError",
    "UnsupportedSensorTypeError",
]
