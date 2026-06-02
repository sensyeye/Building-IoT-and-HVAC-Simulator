"""Tests for SensgreenMetricMapper."""

from __future__ import annotations

import pytest

from simulator.integrations.sensgreen_metric_mapper import (
    SensgreenMetricMapper,
    UnknownMetricError,
    UnsupportedSensorTypeError,
)


def test_iaq_mapping_basic():
    m = SensgreenMetricMapper()
    out = m.map(
        "iaq",
        {"temperature": 23.4, "humidity": 55.2, "co2": 612, "pm25": 7.8},
    )
    assert out == {
        "temperature": 23.4,
        "humidity": 55.2,
        "co2": 612,
        "pm25": 7.8,
    }


def test_iaq_aliases():
    m = SensgreenMetricMapper()
    out = m.map("iaq", {"tvoc": 120, "pm2_5": 10.0, "aqi": 42})
    assert out == {"voc": 120, "pm25": 10.0, "iaq": 42}


def test_energy_meter_mapping():
    m = SensgreenMetricMapper()
    out = m.map(
        "energy_meter",
        {
            "active_power_kw": 1.23,
            "active_energy_kwh": 4567.89,
            "voltage_l1": 230.1,
            "current_l1": 5.4,
            "frequency_hz": 50.01,
            "power_factor": 0.97,
        },
    )
    assert out == {
        "active_power": 1.23,
        "active_energy": 4567.89,
        "voltage_1": 230.1,
        "current_1": 5.4,
        "frequency": 50.01,
        "power_factor": 0.97,
    }


def test_people_counter_mapping():
    m = SensgreenMetricMapper()
    out = m.map("people_counter", {"people_count": 12, "occupancy": True})
    assert out == {"people_count": 12, "occupancy": True}


def test_entry_exit_counter_mapping():
    m = SensgreenMetricMapper()
    out = m.map(
        "entry_exit_counter",
        {
            "periodic_in": 3,
            "periodic_out": 2,
            "total_in": 1042,
            "total_out": 1037,
        },
    )
    assert out == {
        "periodic_counter_in": 3,
        "periodic_counter_out": 2,
        "total_counter_in": 1042,
        "total_counter_out": 1037,
    }


def test_hvac_mapping():
    m = SensgreenMetricMapper()
    out = m.map(
        "hvac",
        {
            "supply_air_temperature": 16.5,
            "return_air_temperature": 23.0,
            "fan_speed": 75,
            "valve_position": 40,
        },
    )
    assert out == {
        "supply_air_temperature": 16.5,
        "return_air_temperature": 23.0,
        "fan_speed": 75,
        "valve_position": 40,
    }


def test_unknown_metric_strict_mode_raises():
    m = SensgreenMetricMapper(strict_mode=True)
    with pytest.raises(UnknownMetricError, match="bogus_metric"):
        m.map("iaq", {"temperature": 22.0, "bogus_metric": 1})


def test_unknown_metric_non_strict_mode_drops():
    m = SensgreenMetricMapper(strict_mode=False)
    out = m.map("iaq", {"temperature": 22.0, "bogus_metric": 1})
    assert out == {"temperature": 22.0}


def test_unsupported_sensor_type_raises():
    m = SensgreenMetricMapper()
    with pytest.raises(UnsupportedSensorTypeError):
        m.map("not_a_sensor", {"temperature": 1.0})


def test_duplicate_target_raises():
    m = SensgreenMetricMapper()
    # Both pm25 and pm2_5 map to "pm25" — providing both is ambiguous.
    with pytest.raises(ValueError, match="Duplicate"):
        m.map("iaq", {"pm25": 1.0, "pm2_5": 2.0})


def test_supported_sensor_types_listed():
    types = SensgreenMetricMapper.supported_sensor_types()
    for t in ("iaq", "energy_meter", "people_counter", "entry_exit_counter", "hvac"):
        assert t in types


def test_booleans_coerced_to_int_for_sensgreen_payload():
    """Sensgreen's wire format expects numeric 1/0 for binary metrics,
    not JSON ``true``/``false``. The mapper must coerce booleans at the
    integration boundary so transports never publish ``true``/``false``."""

    m = SensgreenMetricMapper()

    # Door contact: door_state True/False → open_status 1/0
    out_open = m.map("door_contact", {"door_state": True})
    out_closed = m.map("door_contact", {"door_state": False})
    assert out_open == {"open_status": 1}
    assert out_closed == {"open_status": 0}
    assert isinstance(out_open["open_status"], int)
    assert not isinstance(out_open["open_status"], bool)

    # Occupancy / people counter: occupancy True/False → occupancy 1/0
    out_occ = m.map("occupancy_sensor", {"occupancy": True})
    assert out_occ == {"occupancy": 1}
    assert not isinstance(out_occ["occupancy"], bool)

    # Numeric values pass through unchanged
    out_num = m.map("iaq", {"temperature": 22.5, "co2": 600})
    assert out_num == {"temperature": 22.5, "co2": 600}
