from __future__ import annotations

from simulator.validators import run_validation

from .conftest import good_iaq_series


def test_thin_demo_flagged():
    report = run_validation(good_iaq_series(15), simulation_id="thin")
    cats = {f.category for f in report.findings}
    assert "demo_usefulness" in cats


def test_overall_score_in_range():
    report = run_validation(good_iaq_series(30))
    assert 0 <= report.overall_score <= 100
