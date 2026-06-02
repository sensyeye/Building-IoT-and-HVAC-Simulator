from __future__ import annotations

from simulator.validators.hierarchy_validator import HierarchyValidator

from .conftest import make_energy


def _ctx(hierarchy):
    return {"scenarios": set(), "hierarchy": hierarchy}


def test_balanced_hierarchy_passes():
    readings = []
    for m in range(20):
        readings.append(make_energy(m, device_eui="main", active_power=10.0))
        readings.append(make_energy(m, device_eui="hvac", active_power=6.0, submeter="hvac"))
        readings.append(make_energy(m, device_eui="light", active_power=3.5, submeter="lighting"))
    findings = HierarchyValidator(_ctx({"main": ["hvac", "light"]})).validate(readings)
    assert findings == []


def test_children_exceed_parent_flagged():
    readings = []
    for m in range(20):
        readings.append(make_energy(m, device_eui="main", active_power=5.0))
        readings.append(make_energy(m, device_eui="hvac", active_power=8.0, submeter="hvac"))
    findings = HierarchyValidator(_ctx({"main": ["hvac"]})).validate(readings)
    assert any(f.severity == "critical" for f in findings)


def test_huge_residual_flagged():
    readings = []
    for m in range(20):
        readings.append(make_energy(m, device_eui="main", active_power=10.0))
        readings.append(make_energy(m, device_eui="hvac", active_power=1.0, submeter="hvac"))
    findings = HierarchyValidator(_ctx({"main": ["hvac"]})).validate(readings)
    assert any(f.severity == "warning" for f in findings)
