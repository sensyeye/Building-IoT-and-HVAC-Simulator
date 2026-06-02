"""Tests for the YAML config loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from simulator.config_loader import ConfigError, load_config


VALID_YAML = """
building:
  id: bld-test
  name: "Test Building"
  timezone: "UTC"
  zones:
    - id: zone-a
      name: "Zone A"
      area_m2: 50
      capacity: 10

devices:
  - device_eui: "AAA"
    name: "IAQ A"
    type: iaq
    zone_id: zone-a

outputs:
  mqtt:
    enabled: false
  csv:
    enabled: true
    output_dir: outputs
    filename: readings_long.csv

simulation:
  mode: live
  interval_seconds: 30
  seed: 1
"""


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def test_loads_valid_config(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, VALID_YAML))
    assert cfg.building.id == "bld-test"
    assert cfg.building.timezone == "UTC"
    assert len(cfg.building.zones) == 1
    assert cfg.building.zones[0].id == "zone-a"
    assert len(cfg.devices) == 1
    assert cfg.devices[0].device_eui == "AAA"
    assert cfg.devices[0].zone_id == "zone-a"
    assert cfg.outputs.csv.enabled is True
    assert cfg.outputs.mqtt.enabled is False
    assert cfg.simulation.mode == "live"
    assert cfg.simulation.interval_seconds == 30


def test_summary_runs(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, VALID_YAML))
    s = cfg.summary()
    assert "Test Building" in s
    assert "Devices: 1" in s


def test_demo_office_yaml_loads() -> None:
    # Ship-side acceptance: the bundled sample config must parse cleanly.
    repo_root = Path(__file__).resolve().parents[1]
    cfg = load_config(repo_root / "configs" / "demo_office.yaml")
    assert cfg.building.id == "bld-demo-office"
    assert len(cfg.building.zones) == 2
    assert len(cfg.devices) >= 1


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yaml")


def test_missing_building_raises(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "devices:\n  - device_eui: A\n    name: X\n    type: iaq\n    zone_id: z\n",
    )
    with pytest.raises(ConfigError, match="building"):
        load_config(p)


def test_missing_devices_raises(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "building:\n  id: b\n  name: B\n  zones: []\n",
    )
    with pytest.raises(ConfigError, match="devices"):
        load_config(p)


def test_unknown_zone_id_raises(tmp_path: Path) -> None:
    bad = VALID_YAML.replace("zone_id: zone-a", "zone_id: zone-ghost")
    with pytest.raises(ConfigError, match="unknown zone_id"):
        load_config(_write(tmp_path, bad))


def test_invalid_mode_raises(tmp_path: Path) -> None:
    bad = VALID_YAML.replace("mode: live", "mode: bogus")
    with pytest.raises(ConfigError, match="mode"):
        load_config(_write(tmp_path, bad))


def test_historical_requires_range(tmp_path: Path) -> None:
    bad = VALID_YAML.replace("mode: live", "mode: historical")
    with pytest.raises(ConfigError, match="start.*end|historical"):
        load_config(_write(tmp_path, bad))


def test_mqtt_enabled_requires_host(tmp_path: Path) -> None:
    bad = VALID_YAML.replace("mqtt:\n    enabled: false", "mqtt:\n    enabled: true")
    with pytest.raises(ConfigError, match="host"):
        load_config(_write(tmp_path, bad))


def test_dry_run_cli_prints_summary(capsys: pytest.CaptureFixture[str]) -> None:
    from simulator.main import main as cli_main

    repo_root = Path(__file__).resolve().parents[1]
    rc = cli_main(["--config", str(repo_root / "configs" / "demo_office.yaml"), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "parsed config summary" in out
    assert "Sensgreen Demo Office" in out
    assert "Devices:" in out
