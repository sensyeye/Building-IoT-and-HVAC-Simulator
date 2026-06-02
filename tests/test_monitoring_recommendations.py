"""Tests for the monitoring intent + richness recommendation engine (P9.4)."""

from __future__ import annotations

import pytest

from simulator.catalogs import (
    DEFAULT_INTENT,
    DEFAULT_RICHNESS,
    MONITORING_INTENTS,
    RICHNESS_LEVELS,
    device_type_summary,
    list_monitoring_intents,
    list_richness_levels,
    normalize_intent,
    normalize_richness,
    recommend_devices,
    recommend_devices_for_room,
)
from simulator.devices.catalog import known_sensor_types


# ---------------------------------------------------------------------------
# Taxonomy + normalisation
# ---------------------------------------------------------------------------


def test_intent_and_richness_lists_have_labels():
    intents = list_monitoring_intents()
    assert {i["id"] for i in intents} == set(MONITORING_INTENTS)
    assert all("label" in i for i in intents)

    richness = list_richness_levels()
    assert {r["id"] for r in richness} == set(RICHNESS_LEVELS)
    assert all("label" in r for r in richness)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("comfort", "comfort"),
        ("  IAQ  ", "iaq"),
        ("not_a_real_intent", DEFAULT_INTENT),
        ("", DEFAULT_INTENT),
        (None, DEFAULT_INTENT),
    ],
)
def test_normalize_intent(raw, expected):
    assert normalize_intent(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("basic", "basic"),
        ("ADVANCED", "advanced"),
        ("wat", DEFAULT_RICHNESS),
        (None, DEFAULT_RICHNESS),
    ],
)
def test_normalize_richness(raw, expected):
    assert normalize_richness(raw) == expected


# ---------------------------------------------------------------------------
# Recommendation table — coverage
# ---------------------------------------------------------------------------


def test_every_intent_richness_cell_returns_at_least_one_device():
    for intent in MONITORING_INTENTS:
        for richness in RICHNESS_LEVELS:
            specs = recommend_devices(room_type=None, intent=intent, richness=richness)
            assert specs, f"empty recommendation for {intent}/{richness}"
            for s in specs:
                assert "type" in s
                assert "role" in s


def test_recommendations_only_use_known_sensor_types():
    known = known_sensor_types()
    for intent in MONITORING_INTENTS:
        for richness in RICHNESS_LEVELS:
            for s in recommend_devices(None, intent, richness):
                assert s["type"] in known, (
                    f"{intent}/{richness} references unknown sensor type {s['type']!r}"
                )


def test_richness_is_monotonic_in_device_count_for_iaq():
    counts = [
        len(recommend_devices(None, "iaq", level)) for level in RICHNESS_LEVELS
    ]
    # basic <= standard <= advanced (strict not required, monotonic is enough)
    assert counts[0] <= counts[1] <= counts[2]


# ---------------------------------------------------------------------------
# Headline cells (the ones the user asked for)
# ---------------------------------------------------------------------------


def test_hotel_guest_room_comfort_advanced_includes_occ_door_hvac():
    specs = recommend_devices("hotel_guest_room", "comfort", "advanced")
    types = device_type_summary(specs)
    assert "iaq" in types
    assert "occupancy_sensor" in types
    assert "door_contact" in types
    assert "hvac" in types


def test_mall_entrance_people_flow_standard_has_people_counter_and_iaq():
    specs = recommend_devices("mall_entrance", "people_flow", "standard")
    types = device_type_summary(specs)
    assert types.get("entry_exit_counter", 0) >= 1
    assert types.get("iaq", 0) >= 1


def test_meeting_room_iaq_advanced_has_iaq_plus_occupancy():
    specs = recommend_devices("meeting_room", "iaq", "advanced")
    types = device_type_summary(specs)
    assert types["iaq"] >= 1
    assert types["occupancy_sensor"] >= 1


def test_server_room_iaq_basic_is_temperature_humidity_only():
    specs = recommend_devices("server_room", "iaq", "basic")
    types = device_type_summary(specs)
    assert list(types.keys()) == ["iaq"]
    # Role hint should indicate the stripped-down profile.
    assert specs[0]["role"] == "temperature_humidity"


def test_server_room_does_not_get_door_or_people_via_overrides():
    for richness in RICHNESS_LEVELS:
        specs = recommend_devices("server_room", "comfort", richness)
        types = device_type_summary(specs)
        assert "door_contact" not in types
        assert "entry_exit_counter" not in types


def test_parking_area_iaq_uses_co_ventilation_role():
    specs = recommend_devices("parking_area", "iaq", "standard")
    assert any(
        s["type"] == "iaq" and s["role"] == "co_ventilation" for s in specs
    )


def test_recommendations_are_independent_copies():
    a = recommend_devices("meeting_room", "iaq", "advanced")
    b = recommend_devices("meeting_room", "iaq", "advanced")
    assert a == b
    a[0]["role"] = "MUTATED"
    a[0].setdefault("metadata", {})["foo"] = "bar"
    # Second call must not see the mutation.
    c = recommend_devices("meeting_room", "iaq", "advanced")
    assert c[0]["role"] != "MUTATED"
    assert "foo" not in c[0].get("metadata", {})


# ---------------------------------------------------------------------------
# Room-level wrapper
# ---------------------------------------------------------------------------


def test_recommend_devices_for_room_reads_intent_and_richness():
    room = {
        "id": "meet-1",
        "room_type": "meeting_room",
        "monitoring_intent": "iaq",
        "monitoring_richness": "advanced",
    }
    specs = recommend_devices_for_room(room)
    types = device_type_summary(specs)
    assert types["iaq"] >= 1
    assert types["occupancy_sensor"] >= 1


def test_recommend_devices_for_room_uses_defaults_when_missing():
    room = {"id": "x", "room_type": "open_office"}
    specs = recommend_devices_for_room(room)
    assert specs  # whatever the defaults map to, must be non-empty
    # Defaults are iaq/standard which yields at least an IAQ sensor.
    assert any(s["type"] == "iaq" for s in specs)
