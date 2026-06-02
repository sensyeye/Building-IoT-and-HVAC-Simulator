"""Tests for P10.2 — DevicePersonality."""

from __future__ import annotations

import statistics

import pytest

from simulator.models.config import DeviceConfig
from simulator.sensors.device_personality import (
    KNOWN_PROFILES,
    DevicePersonality,
    normalize_profile,
)


def _device(eui: str = "EUI-001", profile: str | None = None) -> DeviceConfig:
    meta: dict = {}
    if profile:
        meta["personality"] = profile
    return DeviceConfig(
        device_eui=eui,
        name="t",
        type="iaq",
        zone_id="z1",
        metadata=meta,
    )


def test_normalize_profile_falls_back_to_normal():
    assert normalize_profile(None) == "normal"
    assert normalize_profile("") == "normal"
    assert normalize_profile("nonsense") == "normal"
    assert normalize_profile("NEAR_DOOR") == "near_door"


def test_known_profiles_have_all_metric_entries():
    for profile in KNOWN_PROFILES:
        p = DevicePersonality(profile=profile, device_eui="EUI-X")
        for metric in (
            "temperature_c", "humidity_pct", "co2_ppm", "voc_mg_m3",
            "pm25_ug_m3", "pm10_ug_m3", "pressure_pa",
        ):
            assert metric in p.offsets


def test_observe_unknown_metric_returns_truth():
    p = DevicePersonality(profile="normal", device_eui="EUI-1")
    assert p.observe("not_a_metric", 42.0) == 42.0


def test_normal_profile_keeps_readings_close_to_truth():
    p = DevicePersonality(profile="normal", device_eui="EUI-CLOSE")
    samples = [p.observe("temperature_c", 22.0) for _ in range(200)]
    assert abs(statistics.mean(samples) - 22.0) < 0.5


def test_slightly_noisy_has_more_spread_than_normal():
    a = DevicePersonality(profile="normal", device_eui="EUI-A")
    b = DevicePersonality(profile="slightly_noisy", device_eui="EUI-A")
    sa = [a.observe("co2_ppm", 800.0) for _ in range(300)]
    sb = [b.observe("co2_ppm", 800.0) for _ in range(300)]
    assert statistics.pstdev(sb) > statistics.pstdev(sa) + 3.0


def test_offset_profile_yields_consistent_bias_per_device():
    p = DevicePersonality(profile="offset", device_eui="EUI-BIAS")
    bias = p.offsets["temperature_c"]
    # offset shouldn't be exactly zero (with probability ~1)
    assert bias != 0.0
    samples = [p.observe("temperature_c", 22.0) for _ in range(200)]
    # Mean of samples should land near (truth + bias).
    assert abs(statistics.mean(samples) - (22.0 + bias)) < 0.3


def test_near_hvac_supply_reads_cooler_and_drier():
    # The profile has a built-in negative bias on temperature & humidity.
    # Average over many devices should be clearly below truth.
    temps = []
    hums = []
    for i in range(60):
        p = DevicePersonality(profile="near_hvac_supply", device_eui=f"EUI-HV-{i}")
        temps.append(p.observe("temperature_c", 22.0))
        hums.append(p.observe("humidity_pct", 50.0))
    assert statistics.mean(temps) < 22.0 - 0.4
    assert statistics.mean(hums) < 50.0 - 1.5


def test_same_eui_yields_identical_personality():
    p1 = DevicePersonality(profile="offset", device_eui="EUI-SAME")
    p2 = DevicePersonality(profile="offset", device_eui="EUI-SAME")
    assert p1.offsets == p2.offsets


def test_different_eui_yields_different_offsets():
    p1 = DevicePersonality(profile="offset", device_eui="EUI-A1")
    p2 = DevicePersonality(profile="offset", device_eui="EUI-A2")
    # Vanishingly unlikely to be identical with sha256-derived seeds.
    assert p1.offsets["temperature_c"] != p2.offsets["temperature_c"]


def test_slightly_drifty_accumulates_over_time():
    p = DevicePersonality(profile="slightly_drifty", device_eui="EUI-DRIFT")
    drifts = []
    for _ in range(300):
        p.observe("co2_ppm", 800.0, dt_min=1.0)
        drifts.append(p.drift["co2_ppm"])
    # Drift should wander noticeably from zero at some point.
    assert max(abs(d) for d in drifts) > 0.5


def test_from_device_honours_metadata_profile():
    d = _device(profile="near_door")
    p = DevicePersonality.from_device(d)
    assert p.profile == "near_door"


def test_from_device_explicit_overrides_metadata():
    d = _device(profile="near_door")
    p = DevicePersonality.from_device(d, profile="offset")
    assert p.profile == "offset"
