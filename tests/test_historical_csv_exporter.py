"""Tests for HistoricalCsvExporter."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from simulator.models.reading import SensorReading
from simulator.outputs.historical_csv_exporter import (
    DEVICES_HEADER,
    HistoricalCsvExporter,
    READINGS_LONG_HEADER,
    UPLINKS_JSON_HEADER,
)


TS = datetime(2026, 3, 2, 15, 20, 0, tzinfo=timezone.utc)
TS_MS = 1772464800000


def _sample_readings() -> list[SensorReading]:
    return [
        SensorReading(
            device_eui="dev-iaq-1",
            sensor_type="iaq",
            timestamp=TS,
            data={"temperature": 23.4, "humidity": 55.2, "co2": 612},
            metadata={
                "building_id": "bld-1",
                "floor_id": "f1",
                "zone_id": "z1",
                "room_id": "r1",
                "scenario_id": "weekday",
                "quality": "good",
                "name": "IAQ Open Space",
            },
        ),
        SensorReading(
            device_eui="dev-energy-1",
            sensor_type="energy_meter",
            timestamp=TS,
            data={"active_power_kw": 1.23, "active_energy_kwh": 4567.89},
            metadata={
                "building_id": "bld-1",
                "zone_id": "z1",
                "name": "Main Energy Meter",
            },
        ),
        # Same device a tick later -> should not duplicate in devices.csv
        SensorReading(
            device_eui="dev-iaq-1",
            sensor_type="iaq",
            timestamp=TS,
            data={"temperature": 23.5, "humidity": 55.0, "co2": 615},
            metadata={"building_id": "bld-1", "zone_id": "z1"},
        ),
    ]


def _read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        rows = list(reader)
    return rows[0], rows[1:]


def test_export_creates_output_dir(tmp_path: Path):
    out = tmp_path / "deep" / "nested" / "outputs"
    assert not out.exists()
    exporter = HistoricalCsvExporter(out, simulation_id="sim-x")
    result = exporter.export(_sample_readings())
    assert out.is_dir()
    assert result.paths.readings_long.exists()
    assert result.paths.uplinks_json.exists()
    assert result.paths.devices.exists()


def test_readings_long_one_row_per_metric(tmp_path: Path):
    exporter = HistoricalCsvExporter(tmp_path, simulation_id="sim-1")
    result = exporter.export(_sample_readings())

    header, rows = _read_csv(result.paths.readings_long)
    assert tuple(header) == tuple(READINGS_LONG_HEADER)
    # 3 (iaq) + 2 (energy) + 3 (iaq) = 8 metric rows
    assert len(rows) == 8
    assert result.readings_long_rows == 8

    # Spot-check first IAQ row
    first = rows[0]
    row = dict(zip(header, first))
    assert row["simulation_id"] == "sim-1"
    assert row["device_eui"] == "dev-iaq-1"
    assert row["timestamp_ms"] == str(TS_MS)
    assert row["timestamp_utc"].startswith("2026-03-02T15:20:00")
    assert row["metric_name"] == "temperature"
    assert row["metric_value"] == "23.4"
    assert row["sensor_type"] == "iaq"
    assert row["building_id"] == "bld-1"
    assert row["zone_id"] == "z1"
    assert row["scenario_id"] == "weekday"
    assert row["quality"] == "good"

    # Energy mapping should produce Sensgreen ids, not internal *_kw / *_kwh
    metric_names = {r[header.index("metric_name")] for r in rows}
    assert "active_power" in metric_names
    assert "active_energy" in metric_names
    assert "active_power_kw" not in metric_names


def test_uplinks_json_one_row_per_reading(tmp_path: Path):
    exporter = HistoricalCsvExporter(tmp_path, simulation_id="sim-2")
    readings = _sample_readings()
    result = exporter.export(readings)

    header, rows = _read_csv(result.paths.uplinks_json)
    assert tuple(header) == tuple(UPLINKS_JSON_HEADER)
    assert len(rows) == len(readings) == 3
    assert result.uplinks_json_rows == 3

    # Each payload_json must be the Sensgreen MQTT envelope.
    for r in rows:
        row = dict(zip(header, r))
        payload = json.loads(row["payload_json"])
        assert set(payload.keys()) == {"deviceEui", "timestamp", "data"}
        assert payload["deviceEui"] == row["device_eui"]
        assert payload["timestamp"] == int(row["timestamp_ms"])
        assert isinstance(payload["data"], dict) and payload["data"]


def test_devices_csv_distinct_devices(tmp_path: Path):
    exporter = HistoricalCsvExporter(tmp_path)
    result = exporter.export(_sample_readings())

    header, rows = _read_csv(result.paths.devices)
    assert tuple(header) == tuple(DEVICES_HEADER)
    assert len(rows) == 2
    assert result.devices_rows == 2

    by_eui = {r[0]: dict(zip(header, r)) for r in rows}
    assert by_eui["dev-iaq-1"]["sensor_type"] == "iaq"
    assert by_eui["dev-iaq-1"]["name"] == "IAQ Open Space"
    assert by_eui["dev-iaq-1"]["building_id"] == "bld-1"
    assert by_eui["dev-energy-1"]["sensor_type"] == "energy_meter"


def test_export_with_no_readings_writes_headers(tmp_path: Path):
    exporter = HistoricalCsvExporter(tmp_path)
    result = exporter.export([])
    assert result.readings_long_rows == 0
    assert result.uplinks_json_rows == 0
    assert result.devices_rows == 0

    for p in (
        result.paths.readings_long,
        result.paths.uplinks_json,
        result.paths.devices,
    ):
        header, rows = _read_csv(p)
        assert header  # header exists
        assert rows == []
