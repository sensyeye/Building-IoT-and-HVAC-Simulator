"""Tests for the sensor catalog (used by the Devices tab dropdown)."""

from __future__ import annotations

from simulator.devices import (
    get_sensor_type,
    known_sensor_types,
    list_sensor_types,
)


def test_catalog_is_non_empty_and_unique():
    types = list_sensor_types()
    assert len(types) >= 3
    ids = [t.id for t in types]
    assert len(ids) == len(set(ids)), "sensor type ids must be unique"


def test_known_sensor_types_contains_iaq_and_energy():
    known = known_sensor_types()
    assert "iaq" in known
    assert "energy_meter" in known
    assert "entry_exit_counter" in known


def test_get_sensor_type_returns_none_for_unknown():
    assert get_sensor_type("does-not-exist") is None


def test_default_metadata_covers_all_required_fields():
    for st in list_sensor_types():
        defaults = st.default_metadata()
        for field in st.metadata:
            if field.required:
                assert field.key in defaults, (
                    f"{st.id}.{field.key} is required but missing from defaults"
                )


def test_to_dict_is_json_safe():
    for st in list_sensor_types():
        payload = st.to_dict()
        assert payload["id"] == st.id
        assert isinstance(payload["metadata"], list)
        for field in payload["metadata"]:
            assert "key" in field and "kind" in field
            assert field["kind"] in {
                "number",
                "integer",
                "string",
                "choice",
                "boolean",
            }
