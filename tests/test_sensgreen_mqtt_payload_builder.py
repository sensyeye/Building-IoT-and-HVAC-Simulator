"""Tests for SensgreenMqttPayloadBuilder."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from simulator.integrations.sensgreen_mqtt_payload_builder import (
    PayloadValidationError,
    SensgreenMqttPayloadBuilder,
)
from simulator.models.reading import SensorReading


# 2026-03-02T15:20:00Z -> 1772464800000 ms
FIXED_TS = datetime(2026, 3, 2, 15, 20, 0, tzinfo=timezone.utc)
FIXED_TS_MS = 1772464800000


def test_valid_iaq_payload():
    reading = SensorReading(
        device_eui="0011223344556677",
        sensor_type="iaq",
        timestamp=FIXED_TS,
        data={"temperature": 23.4, "humidity": 55.2, "co2": 612, "pm25": 7.8},
    )
    payload = SensgreenMqttPayloadBuilder().build(reading)
    assert payload == {
        "deviceEui": "0011223344556677",
        "timestamp": FIXED_TS_MS,
        "data": {
            "temperature": 23.4,
            "humidity": 55.2,
            "co2": 612,
            "pm25": 7.8,
        },
    }


def test_valid_energy_payload():
    reading = SensorReading(
        device_eui="AA:BB:CC:DD",
        sensor_type="energy_meter",
        timestamp=FIXED_TS,
        data={
            "active_power_kw": 1.23,
            "active_energy_kwh": 4567.89,
            "voltage_l1": 230.1,
            "current_l1": 5.4,
        },
    )
    payload = SensgreenMqttPayloadBuilder().build(reading)
    assert payload["deviceEui"] == "AA:BB:CC:DD"
    assert payload["timestamp"] == FIXED_TS_MS
    assert payload["data"] == {
        "active_power": 1.23,
        "active_energy": 4567.89,
        "voltage_1": 230.1,
        "current_1": 5.4,
    }


def test_missing_device_eui_raises():
    reading = SensorReading(
        device_eui="   ",
        sensor_type="iaq",
        timestamp=FIXED_TS,
        data={"temperature": 22.0},
    )
    with pytest.raises(PayloadValidationError, match="deviceEui"):
        SensgreenMqttPayloadBuilder().build(reading)


def test_empty_data_raises():
    reading = SensorReading(
        device_eui="dev-1",
        sensor_type="iaq",
        timestamp=FIXED_TS,
        data={},
    )
    with pytest.raises(PayloadValidationError, match="data"):
        SensgreenMqttPayloadBuilder().build(reading)


def test_data_empty_after_mapping_raises():
    # Non-strict mapper drops unknowns; if everything is unknown -> empty data.
    reading = SensorReading(
        device_eui="dev-1",
        sensor_type="iaq",
        timestamp=FIXED_TS,
        data={"totally_made_up_metric": 1.0},
    )
    with pytest.raises(PayloadValidationError, match="empty after mapping"):
        SensgreenMqttPayloadBuilder().build(reading)


def test_naive_timestamp_treated_as_utc():
    naive = datetime(2026, 3, 2, 15, 20, 0)  # no tzinfo
    reading = SensorReading(
        device_eui="dev-1",
        sensor_type="iaq",
        timestamp=naive,
        data={"temperature": 22.0},
    )
    payload = SensgreenMqttPayloadBuilder().build(reading)
    assert payload["timestamp"] == FIXED_TS_MS


def test_non_utc_timestamp_converted():
    from datetime import timedelta

    tz_plus3 = timezone(timedelta(hours=3))
    local = datetime(2026, 3, 2, 18, 20, 0, tzinfo=tz_plus3)  # == 15:20 UTC
    reading = SensorReading(
        device_eui="dev-1",
        sensor_type="iaq",
        timestamp=local,
        data={"temperature": 22.0},
    )
    payload = SensgreenMqttPayloadBuilder().build(reading)
    assert payload["timestamp"] == FIXED_TS_MS


def test_invalid_input_type_raises():
    with pytest.raises(PayloadValidationError, match="SensorReading"):
        SensgreenMqttPayloadBuilder().build({"deviceEui": "x"})  # type: ignore[arg-type]
