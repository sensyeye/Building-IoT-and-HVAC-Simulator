"""Catalog basics + parity with the global consistency rules file."""

from __future__ import annotations

from pathlib import Path

import yaml

from simulator.scenarios import filter_known, get_scenario, known_ids, list_scenarios


def test_catalog_is_non_empty_and_unique():
    items = list_scenarios()
    assert len(items) >= 10
    ids = [s.id for s in items]
    assert len(set(ids)) == len(ids), "scenario ids must be unique"
    for s in items:
        assert s.id and s.name and s.description
        assert s.category in {"occupancy", "environment", "energy", "hvac", "network"}


def test_known_ids_match_rules_file():
    """Catalog ids must mirror simulator/rules/global_consistency_rules.yaml."""
    rules_path = (
        Path(__file__).resolve().parent.parent
        / "simulator" / "rules" / "global_consistency_rules.yaml"
    )
    data = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
    rules_ids = set(data["intended_anomaly_scenarios"])
    assert known_ids() == rules_ids


def test_get_scenario_lookup_and_filter():
    s = get_scenario("meeting_room_poor_ventilation")
    assert s is not None and s.category == "environment"
    assert get_scenario("not-a-real-id") is None

    kept = filter_known(["overcrowding", "not-a-real-id", "gateway_outage"])
    assert kept == ["overcrowding", "gateway_outage"]
