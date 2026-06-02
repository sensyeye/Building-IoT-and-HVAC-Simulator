"""End-to-end tests for the four CLI subcommands.

We invoke ``simulator.main.main`` directly (no subprocess) so coverage
and tracebacks stay attached.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from simulator import main as cli
from simulator.services.live_runner import LiveRunner

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_CONFIG = REPO_ROOT / "configs" / "demo_office.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_short_history_config(tmp_path: Path) -> Path:
    """Build a 30-minute historical config from the demo config."""
    raw = yaml.safe_load(DEMO_CONFIG.read_text())
    start = datetime(2026, 4, 27, 9, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    raw["simulation"] = {
        "mode": "historical",
        "interval_seconds": 300,  # 5-min steps -> small dataset
        "start": start.isoformat(),
        "end": end.isoformat(),
        "seed": 7,
    }
    raw["outputs"]["csv"]["enabled"] = True
    raw["outputs"]["csv"]["output_dir"] = str(tmp_path / "outputs")
    raw["outputs"]["mqtt"]["enabled"] = False
    p = tmp_path / "history.yaml"
    p.write_text(yaml.safe_dump(raw))
    return p


# ---------------------------------------------------------------------------
# dry-run-config
# ---------------------------------------------------------------------------

def test_dry_run_config_prints_summary(capsys):
    rc = cli.main(["dry-run-config", "--config", str(DEMO_CONFIG)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Effective mode" in out
    assert "Building:" in out


def test_dry_run_config_bad_path_returns_2(capsys):
    rc = cli.main(["dry-run-config", "--config", "/no/such/file.yaml"])
    assert rc == 2


# ---------------------------------------------------------------------------
# generate-history
# ---------------------------------------------------------------------------

def test_generate_history_produces_all_artifacts(tmp_path: Path, capsys):
    cfg_path = _write_short_history_config(tmp_path)
    out_dir = tmp_path / "out"
    rc = cli.main([
        "generate-history",
        "--config", str(cfg_path),
        "--output-dir", str(out_dir),
        "--simulation-id", "test-run",
    ])
    assert rc == 0

    # All four expected files are present.
    assert (out_dir / "readings_long.csv").exists()
    assert (out_dir / "uplinks_json.csv").exists()
    assert (out_dir / "devices.csv").exists()
    assert (out_dir / "validation_report.json").exists()
    assert (out_dir / "readings_internal.jsonl").exists()

    # readings_long has more than just the header.
    long_lines = (out_dir / "readings_long.csv").read_text().splitlines()
    assert len(long_lines) > 1

    # validation_report.json has the canonical shape.
    report = json.loads((out_dir / "validation_report.json").read_text())
    assert report["simulation_id"] == "test-run"
    assert "overall_score" in report
    assert set(report["scores"].keys()) == {
        "physical_validity",
        "temporal_consistency",
        "cross_sensor_correlation",
        "hierarchical_consistency",
        "scenario_consistency",
        "statistical_realism",
        "demo_usefulness",
    }


def test_generate_history_respects_cli_start_end_overrides(tmp_path: Path):
    cfg_path = _write_short_history_config(tmp_path)
    out_dir = tmp_path / "out"
    start = "2026-04-27T10:00:00+00:00"
    end = "2026-04-27T10:15:00+00:00"
    rc = cli.main([
        "generate-history",
        "--config", str(cfg_path),
        "--output-dir", str(out_dir),
        "--start", start,
        "--end", end,
    ])
    assert rc == 0
    # Only ~15 minutes of data → fewer rows than the 30-minute baseline.
    rows = (out_dir / "readings_long.csv").read_text().splitlines()
    assert len(rows) > 1


# ---------------------------------------------------------------------------
# validate-history
# ---------------------------------------------------------------------------

def test_validate_history_replays_existing(tmp_path: Path, capsys):
    cfg_path = _write_short_history_config(tmp_path)
    out_dir = tmp_path / "out"
    assert cli.main([
        "generate-history",
        "--config", str(cfg_path),
        "--output-dir", str(out_dir),
        "--simulation-id", "first-run",
    ]) == 0

    # Overwrite the report and re-run validate-history.
    report_path = out_dir / "validation_report.json"
    original = json.loads(report_path.read_text())
    report_path.write_text("{}")

    rc = cli.main([
        "validate-history",
        "--output-dir", str(out_dir),
        "--simulation-id", "replay",
    ])
    assert rc == 0
    replayed = json.loads(report_path.read_text())
    assert replayed["simulation_id"] == "replay"
    assert replayed["scores"].keys() == original["scores"].keys()


def test_validate_history_missing_jsonl_returns_2(tmp_path: Path, capsys):
    rc = cli.main(["validate-history", "--output-dir", str(tmp_path)])
    assert rc == 2


# ---------------------------------------------------------------------------
# run-live (dry-run)
# ---------------------------------------------------------------------------

def test_run_live_dry_run_publishes_via_stdout(tmp_path: Path, capsys):
    cfg_path = _write_short_history_config(tmp_path)
    rc = cli.main([
        "run-live",
        "--config", str(cfg_path),
        "--dry-run",
        "--duration-seconds", "300",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[dry-run]" in out
    assert "deviceEui" in out  # the JSON body was printed
    assert "published:" in out


def test_run_live_with_injected_publisher(tmp_path: Path):
    """LiveRunner accepts a pre-built publisher (test hook)."""
    from simulator.config_loader import load_config
    from simulator.integrations.sensgreen_mqtt_publisher import SensgreenMqttPublisher

    cfg_path = _write_short_history_config(tmp_path)
    cfg = load_config(cfg_path)
    pub = SensgreenMqttPublisher(host="b", dry_run=True)
    runner = LiveRunner(cfg, publisher=pub)
    start = datetime(2026, 4, 27, 9, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=10)
    result = runner.run(start=start, end=end)
    assert result.dry_run is True
    assert result.published > 0
    assert result.failed == 0
