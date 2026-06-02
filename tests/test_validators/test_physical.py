from __future__ import annotations

from simulator.validators import run_validation
from simulator.validators.physical_validator import PhysicalValidator

from .conftest import good_iaq_series, make_iaq


def test_good_iaq_passes_physical():
    findings = PhysicalValidator({"scenarios": set()}).validate(good_iaq_series())
    assert findings == []


def test_co2_below_floor_flagged():
    readings = [make_iaq(0, co2=100)]  # below physical_min=300
    findings = PhysicalValidator({"scenarios": set()}).validate(readings)
    assert any("co2" in f.entity_id and f.severity == "critical" for f in findings)


def test_pm10_below_pm25_flagged():
    readings = [make_iaq(0, pm25=20, pm10=5)]
    findings = PhysicalValidator({"scenarios": set()}).validate(readings)
    assert any("pm10" in f.entity_id for f in findings)


def test_battery_increase_flagged_without_scenario():
    from simulator.models.reading import SensorReading
    from .conftest import _ts

    # Battery is reported in volts (1.5–4.5).
    a = SensorReading("iaq-x", "iaq", _ts(0), {"battery": 3.0}, {})
    b = SensorReading("iaq-x", "iaq", _ts(5), {"battery": 3.5}, {})
    findings = PhysicalValidator({"scenarios": set()}).validate([a, b])
    assert any("battery" in f.entity_id for f in findings)


def test_battery_increase_allowed_with_scenario():
    from simulator.models.reading import SensorReading
    from .conftest import _ts

    a = SensorReading("iaq-x", "iaq", _ts(0), {"battery": 3.0}, {})
    b = SensorReading("iaq-x", "iaq", _ts(5), {"battery": 4.0}, {})
    findings = PhysicalValidator({"scenarios": {"battery_replacement"}}).validate(
        [a, b]
    )
    assert not any("battery" in f.entity_id for f in findings)


def test_run_validation_smoke_returns_report():
    report = run_validation(
        good_iaq_series(),
        simulation_id="t1",
        building={"type": "office"},
    )
    d = report.to_dict()
    assert d["simulation_id"] == "t1"
    assert set(d["scores"].keys()) == {
        "physical_validity",
        "temporal_consistency",
        "cross_sensor_correlation",
        "hierarchical_consistency",
        "scenario_consistency",
        "statistical_realism",
        "demo_usefulness",
    }
    assert 0 <= d["overall_score"] <= 100
