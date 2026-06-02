"""Historical CSV exporter for the Sensgreen Sensor Simulator.

Exports canonical :class:`SensorReading` objects into three CSV files
that map cleanly onto the Sensgreen historical-import schema:

* ``readings_long.csv`` — one row per (reading, metric) pair, using
  Sensgreen-canonical metric ids.
* ``uplinks_json.csv`` — one row per reading, with the full Sensgreen
  MQTT payload serialised as JSON (built via
  :class:`SensgreenMqttPayloadBuilder`).
* ``devices.csv`` — distinct device inventory derived from the readings.

This module performs no database I/O and no MQTT publishing. Sensor
generation lives elsewhere; readings are passed in from the caller.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from ..integrations.sensgreen_mqtt_payload_builder import SensgreenMqttPayloadBuilder
from ..models.reading import SensorReading


READINGS_LONG_HEADER: Sequence[str] = (
    "simulation_id",
    "device_eui",
    "timestamp_ms",
    "timestamp_utc",
    "metric_name",
    "metric_value",
    "sensor_type",
    "building_id",
    "floor_id",
    "zone_id",
    "room_id",
    "scenario_id",
    "quality",
)

UPLINKS_JSON_HEADER: Sequence[str] = (
    "simulation_id",
    "device_eui",
    "timestamp_ms",
    "timestamp_utc",
    "payload_json",
)

DEVICES_HEADER: Sequence[str] = (
    "device_eui",
    "sensor_type",
    "building_id",
    "floor_id",
    "zone_id",
    "room_id",
    "name",
)


@dataclass(frozen=True)
class ExportPaths:
    """Resolved output file paths."""

    readings_long: Path
    uplinks_json: Path
    devices: Path


@dataclass(frozen=True)
class ExportResult:
    """Summary of an export run."""

    paths: ExportPaths
    readings_long_rows: int
    uplinks_json_rows: int
    devices_rows: int


def _iso_utc(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    # Use trailing 'Z' for clarity in CSV.
    return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"


def _meta(reading: SensorReading, key: str, default: str = "") -> str:
    value = reading.metadata.get(key, default) if reading.metadata else default
    return "" if value is None else str(value)


class HistoricalCsvExporter:
    """Write SensorReadings out as Sensgreen-friendly CSV files.

    Parameters
    ----------
    output_dir:
        Directory where CSV files will be written. Created if missing.
    simulation_id:
        Identifier stamped onto every row to group the export run.
    payload_builder:
        Optional pre-built payload builder. A default non-strict
        :class:`SensgreenMqttPayloadBuilder` is used otherwise.
    """

    def __init__(
        self,
        output_dir: str | Path,
        *,
        simulation_id: str = "sim-default",
        payload_builder: SensgreenMqttPayloadBuilder | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.simulation_id = simulation_id
        self.payload_builder = payload_builder or SensgreenMqttPayloadBuilder()

    # -- public API --------------------------------------------------------

    def paths(self) -> ExportPaths:
        return ExportPaths(
            readings_long=self.output_dir / "readings_long.csv",
            uplinks_json=self.output_dir / "uplinks_json.csv",
            devices=self.output_dir / "devices.csv",
        )

    def export(self, readings: Iterable[SensorReading]) -> ExportResult:
        """Write all three CSV files for the given readings."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        paths = self.paths()

        readings_list = list(readings)

        long_rows = self._write_readings_long(paths.readings_long, readings_list)
        uplink_rows = self._write_uplinks_json(paths.uplinks_json, readings_list)
        device_rows = self._write_devices(paths.devices, readings_list)

        return ExportResult(
            paths=paths,
            readings_long_rows=long_rows,
            uplinks_json_rows=uplink_rows,
            devices_rows=device_rows,
        )

    # -- writers -----------------------------------------------------------

    def _write_readings_long(
        self, path: Path, readings: Sequence[SensorReading]
    ) -> int:
        count = 0
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(READINGS_LONG_HEADER)
            for r in readings:
                ts_ms = r.timestamp_ms()
                ts_iso = _iso_utc(r.timestamp)
                # Map once per reading, then emit one row per metric.
                mapped = self.payload_builder.mapper.map(r.sensor_type, r.data)
                building = _meta(r, "building_id")
                floor = _meta(r, "floor_id")
                zone = _meta(r, "zone_id")
                room = _meta(r, "room_id")
                scenario = _meta(r, "scenario_id")
                quality = _meta(r, "quality", "good")
                for metric_name, metric_value in mapped.items():
                    writer.writerow(
                        [
                            self.simulation_id,
                            r.device_eui,
                            ts_ms,
                            ts_iso,
                            metric_name,
                            metric_value,
                            r.sensor_type,
                            building,
                            floor,
                            zone,
                            room,
                            scenario,
                            quality,
                        ]
                    )
                    count += 1
        return count

    def _write_uplinks_json(
        self, path: Path, readings: Sequence[SensorReading]
    ) -> int:
        count = 0
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(UPLINKS_JSON_HEADER)
            for r in readings:
                payload = self.payload_builder.build(r)
                writer.writerow(
                    [
                        self.simulation_id,
                        r.device_eui,
                        r.timestamp_ms(),
                        _iso_utc(r.timestamp),
                        json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
                    ]
                )
                count += 1
        return count

    def _write_devices(
        self, path: Path, readings: Sequence[SensorReading]
    ) -> int:
        # Distinct devices, preserving first-seen order.
        seen: dict[str, dict[str, str]] = {}
        for r in readings:
            if r.device_eui in seen:
                continue
            seen[r.device_eui] = {
                "device_eui": r.device_eui,
                "sensor_type": r.sensor_type,
                "building_id": _meta(r, "building_id"),
                "floor_id": _meta(r, "floor_id"),
                "zone_id": _meta(r, "zone_id"),
                "room_id": _meta(r, "room_id"),
                "name": _meta(r, "name"),
            }

        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(DEVICES_HEADER)
            for row in seen.values():
                writer.writerow([row[col] for col in DEVICES_HEADER])
        return len(seen)


__all__ = [
    "HistoricalCsvExporter",
    "ExportPaths",
    "ExportResult",
    "READINGS_LONG_HEADER",
    "UPLINKS_JSON_HEADER",
    "DEVICES_HEADER",
]
