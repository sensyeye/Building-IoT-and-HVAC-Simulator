from __future__ import annotations

from simulator.validators import run_validation

from .conftest import good_iaq_series, make_iaq


def test_intended_anomaly_does_not_penalise_score():
    # Build a series with a clear meeting-room CO2 spike.
    readings = good_iaq_series(20)
    for m in range(20, 40):
        readings.append(make_iaq(m, co2=1400, occupancy=8))

    report_no_scenario = run_validation(readings, simulation_id="x")
    report_with_scenario = run_validation(
        readings, simulation_id="x",
        scenarios=["meeting_room_poor_ventilation"],
    )
    # With the scenario active the scenario_consistency category should not
    # be penalised (it should be flagged as intended).
    assert (
        report_with_scenario.scores["scenario_consistency"]
        >= report_no_scenario.scores["scenario_consistency"]
    )
    # Intended-anomaly bucket should be populated.
    assert any(
        f["entity_id"] == "meeting_room_poor_ventilation"
        for f in report_with_scenario.intended_anomalies_detected
    )


def test_missing_intended_fingerprint_flagged():
    # Scenario active but no CO2 spike → should produce a warning finding.
    readings = good_iaq_series(20)
    report = run_validation(
        readings, scenarios=["meeting_room_poor_ventilation"]
    )
    assert any(
        f.entity_id == "meeting_room_poor_ventilation" and f.severity == "warning"
        for f in report.findings
    )
