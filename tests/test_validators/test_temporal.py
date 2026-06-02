from __future__ import annotations

from simulator.models.reading import SensorReading
from simulator.validators.temporal_validator import TemporalValidator

from .conftest import _ts, good_iaq_series


def test_smooth_series_passes():
    findings = TemporalValidator({"scenarios": set()}).validate(good_iaq_series())
    assert findings == []


def test_giant_co2_jump_flagged():
    a = SensorReading("iaq-1", "iaq", _ts(0), {"co2": 500}, {})
    b = SensorReading("iaq-1", "iaq", _ts(1), {"co2": 1500}, {})
    findings = TemporalValidator({"scenarios": set()}).validate([a, b])
    assert any("co2" in f.entity_id for f in findings)
