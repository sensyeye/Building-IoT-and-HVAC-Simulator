from __future__ import annotations

from simulator.validators.correlation_validator import CorrelationValidator
from simulator.validators.rule_loader import load_rules

from .conftest import good_iaq_series, make_iaq


def _ctx():
    return {"scenarios": set(), "rules": load_rules("global_consistency_rules")}


def test_good_correlation_passes():
    findings = CorrelationValidator(_ctx()).validate(good_iaq_series(40))
    assert not any(f.category == "cross_sensor_correlation" for f in findings)


def test_uncorrelated_co2_flagged():
    # CO2 random / unrelated to occupancy.
    readings = []
    for m in range(40):
        occ = m % 5
        co2 = 500 + (m * 37 % 200)  # noise unrelated to occ
        readings.append(make_iaq(m, co2=co2, occupancy=occ))
    findings = CorrelationValidator(_ctx()).validate(readings)
    assert any(f.category == "cross_sensor_correlation" for f in findings)
