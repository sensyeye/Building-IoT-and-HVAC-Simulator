"""CLI entry point for the Sensgreen Sensor Simulator.

Subcommands
-----------
- ``dry-run-config``    Load and validate a config; print summary; exit.
- ``generate-history``  Generate readings for a date range, export CSVs,
                        and write ``validation_report.json``.
- ``run-live``          Generate readings live and publish to MQTT.
                        Supports ``--dry-run`` (no network).
- ``validate-history``  Re-run the validators against a previously
                        generated dataset.

Command code is intentionally thin — all business logic lives in
:mod:`simulator.services`.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config_loader import ConfigError, load_config
from .services import (
    BridgeTester,
    HistoricalRunner,
    LiveRunner,
    load_readings_jsonl,
)
from .validators import run_validation


# ---------------------------------------------------------------------------
# argparse plumbing
# ---------------------------------------------------------------------------

def _add_config_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--config",
        default="configs/demo_office.yaml",
        help="Path to the YAML config file (default: configs/demo_office.yaml).",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sensgreen-simulator",
        description="Sensgreen Sensor Simulator CLI.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO).",
    )
    sub = parser.add_subparsers(dest="command", required=False)

    # dry-run-config -------------------------------------------------------
    p_dry = sub.add_parser(
        "dry-run-config", help="Load and validate config, print summary, exit."
    )
    _add_config_arg(p_dry)

    # generate-history -----------------------------------------------------
    p_hist = sub.add_parser(
        "generate-history",
        help="Generate historical readings → CSV files + validation_report.json.",
    )
    _add_config_arg(p_hist)
    p_hist.add_argument("--start", help="ISO-8601 start (overrides config).")
    p_hist.add_argument("--end", help="ISO-8601 end (overrides config).")
    p_hist.add_argument("--output-dir", help="Output directory (overrides config).")
    p_hist.add_argument(
        "--simulation-id", help="Simulation id stamped onto every row."
    )
    p_hist.add_argument(
        "--scenario",
        action="append",
        default=[],
        help="Active scenario id (repeatable). Used by the scenario validator.",
    )

    # run-live -------------------------------------------------------------
    p_live = sub.add_parser(
        "run-live", help="Run live mode: generate and publish to MQTT."
    )
    _add_config_arg(p_live)
    p_live.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not connect to the broker. Print payloads instead.",
    )
    p_live.add_argument(
        "--duration-seconds",
        type=int,
        default=None,
        help="How long to run for (default: one interval).",
    )
    p_live.add_argument(
        "--realtime",
        action="store_true",
        help="Sleep between ticks so publishes happen in real time.",
    )

    # bridge-test ----------------------------------------------------------
    p_bt = sub.add_parser(
        "bridge-test",
        help="Publish one synthetic payload per device to the Sensgreen "
             "broker, then report success/failure.",
    )
    _add_config_arg(p_bt)
    p_bt.add_argument(
        "--project",
        default=None,
        help="Project id whose stored integration to use "
             "(falls back to outputs.mqtt in the YAML).",
    )
    p_bt.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not connect to the broker; print payloads instead.",
    )
    p_bt.add_argument(
        "--error-listen-seconds",
        type=float,
        default=5.0,
        help="How long to wait after publishing before declaring success "
             "(default: 5.0).",
    )
    p_bt.add_argument(
        "--data-dir",
        default=None,
        help="Project data directory (default: data/projects).",
    )

    # validate-history -----------------------------------------------------
    p_val = sub.add_parser(
        "validate-history", help="Re-validate a previously generated dataset."
    )
    p_val.add_argument(
        "--output-dir",
        required=True,
        help="Directory containing readings_internal.jsonl.",
    )
    p_val.add_argument(
        "--simulation-id", default="validate-history",
        help="Simulation id used in the report (default: validate-history).",
    )
    p_val.add_argument(
        "--scenario",
        action="append",
        default=[],
        help="Active scenario id (repeatable).",
    )
    p_val.add_argument(
        "--report",
        default=None,
        help="Path to write the validation report JSON "
             "(default: <output-dir>/validation_report.json).",
    )

    # Back-compat: top-level --dry-run / --mode / --config flags.
    parser.add_argument("--config", help=argparse.SUPPRESS)
    parser.add_argument("--mode", choices=["live", "historical"], help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)

    return parser


# ---------------------------------------------------------------------------
# Subcommand handlers (all thin)
# ---------------------------------------------------------------------------

def _cmd_dry_run_config(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    print("[sensgreen-simulator] dry-run: parsed config summary")
    print("-" * 60)
    print(cfg.summary())
    print("-" * 60)
    print(f"Effective mode: {cfg.simulation.mode}")
    return 0


def _cmd_generate_history(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    runner = HistoricalRunner(
        cfg,
        output_dir=args.output_dir,
        simulation_id=args.simulation_id,
        scenarios=args.scenario,
    )
    result = runner.run(start=args.start, end=args.end)
    print(f"output_dir       : {result.output_dir}")
    print(f"readings         : {result.readings_count}")
    print(f"readings_long    : {result.export.readings_long_rows} rows "
          f"→ {result.export.paths.readings_long}")
    print(f"uplinks_json     : {result.export.uplinks_json_rows} rows "
          f"→ {result.export.paths.uplinks_json}")
    print(f"devices          : {result.export.devices_rows} rows "
          f"→ {result.export.paths.devices}")
    print(f"readings_internal: {result.readings_path}")
    print(f"validation_report: {result.report_path}")
    print(f"overall_score    : {result.report.overall_score}")
    return 0


def _cmd_run_live(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    runner = LiveRunner(cfg, dry_run=args.dry_run, realtime=args.realtime)
    result = runner.run(duration_seconds=args.duration_seconds)
    print(f"published: {result.published}")
    print(f"failed   : {result.failed}")
    print(f"dry_run  : {result.dry_run}")
    return 0 if result.failed == 0 else 1


def _cmd_bridge_test(args: argparse.Namespace) -> int:
    from .integrations.sensgreen_mqtt_publisher import SensgreenMqttPublisher

    cfg = load_config(args.config)

    integration: dict | None = None
    if args.project:
        # Lazy import so the CLI works without the web deps loaded.
        from pathlib import Path as _Path
        from api.services.project_service import ProjectService

        svc = ProjectService(
            data_dir=_Path(args.data_dir) if args.data_dir else None
        )
        integration = svc.get_integration(args.project)
        if integration is None:
            print(
                f"[sensgreen-simulator] no stored integration for project "
                f"'{args.project}'",
                file=sys.stderr,
            )
            return 2

    if integration is not None:
        publisher = SensgreenMqttPublisher.from_integration(
            integration, dry_run=args.dry_run
        )
    else:
        if cfg.outputs.mqtt is None:
            print(
                "[sensgreen-simulator] config has no outputs.mqtt section "
                "and no --project was given",
                file=sys.stderr,
            )
            return 2
        publisher = SensgreenMqttPublisher.from_config(
            cfg.outputs.mqtt, dry_run=args.dry_run
        )

    tester = BridgeTester(
        cfg, publisher, error_listen_seconds=args.error_listen_seconds
    )
    result = tester.run(project_id=args.project)

    print(f"host           : {result.host}:{result.port}")
    print(f"topic          : {result.topic}")
    print(f"error_topic    : {result.error_topic or '-'}")
    print(f"dry_run        : {result.dry_run}")
    print(f"published      : {result.published_count}/{len(result.devices)}")
    print(f"broker_errors  : {len(result.broker_errors)}")
    print("-" * 60)
    for d in result.devices:
        status = "OK " if d.published else "FAIL"
        line = f"  [{status}] {d.device_eui}  {d.sensor_type:<18} {d.device_name}"
        if d.error:
            line += f"  — {d.error}"
        print(line)
    if result.broker_errors:
        print("-" * 60)
        print("broker error topic messages:")
        for err in result.broker_errors:
            print(f"  {err['topic']}: {err['body']}")
    print("-" * 60)
    print(f"all_ok         : {result.all_ok}")
    return 0 if result.all_ok else 1


def _cmd_validate_history(args: argparse.Namespace) -> int:
    out_dir = Path(args.output_dir)
    readings_path = out_dir / "readings_internal.jsonl"
    if not readings_path.exists():
        print(f"error: {readings_path} not found", file=sys.stderr)
        return 2
    readings = load_readings_jsonl(readings_path)
    report = run_validation(
        readings,
        simulation_id=args.simulation_id,
        building={"type": "office"},
        scenarios=args.scenario,
    )
    report_path = Path(args.report) if args.report else out_dir / "validation_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8"
    )
    print(f"readings        : {len(readings)}")
    print(f"overall_score   : {report.overall_score}")
    print(f"findings        : {len(report.findings)}")
    print(f"validation_report: {report_path}")
    return 0


COMMANDS = {
    "dry-run-config": _cmd_dry_run_config,
    "generate-history": _cmd_generate_history,
    "run-live": _cmd_run_live,
    "bridge-test": _cmd_bridge_test,
    "validate-history": _cmd_validate_history,
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Back-compat: `python -m simulator.main --dry-run` (no subcommand).
    if args.command is None:
        if args.dry_run:
            args.config = args.config or "configs/demo_office.yaml"
            return _cmd_dry_run_config(args)
        build_parser().print_help()
        return 0

    handler = COMMANDS[args.command]
    try:
        return handler(args)
    except ConfigError as e:
        print(f"[sensgreen-simulator] config error: {e}", file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"[sensgreen-simulator] file not found: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
