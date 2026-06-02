"""Historical mode runner: generate → CSV export → validation."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..models.config import SimulatorConfig
from ..models.reading import SensorReading
from ..outputs.historical_csv_exporter import ExportResult, HistoricalCsvExporter
from ..validators import ValidationReport, run_validation
from .readings_io import dump_readings_jsonl, load_readings_jsonl
from .simulation_service import SimulationService


@dataclass
class HistoricalRunResult:
    output_dir: Path
    readings_count: int
    export: ExportResult
    report: ValidationReport
    readings_path: Path
    report_path: Path


def _parse_iso(s: str | datetime) -> datetime:
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    # Accept "...Z" or "+00:00".
    s = s.replace("Z", "+00:00") if s.endswith("Z") else s
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class HistoricalRunner:
    """Generate readings for ``[start, end)``, export CSVs, write report."""

    def __init__(
        self,
        cfg: SimulatorConfig,
        *,
        output_dir: str | Path | None = None,
        simulation_id: str | None = None,
        scenarios: list[str] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.cfg = cfg
        self.scenarios = list(scenarios or [])
        self._log = logger or logging.getLogger("sensgreen.historical")

        self.output_dir = Path(output_dir or cfg.outputs.csv.output_dir)
        self.simulation_id = simulation_id or self._default_simulation_id(cfg)

    @staticmethod
    def _default_simulation_id(cfg: SimulatorConfig) -> str:
        return f"{cfg.building.id}-{datetime.utcnow():%Y%m%dT%H%M%S}"

    # -- public API --------------------------------------------------------

    def run(
        self,
        *,
        start: str | datetime | None = None,
        end: str | datetime | None = None,
    ) -> HistoricalRunResult:
        start = _parse_iso(start or self.cfg.simulation.start)  # type: ignore[arg-type]
        end = _parse_iso(end or self.cfg.simulation.end)  # type: ignore[arg-type]
        if end <= start:
            raise ValueError(f"end ({end}) must be after start ({start})")

        self._log.info(
            "historical run: %s → %s, devices=%d, interval=%ss",
            start, end, len(self.cfg.devices), self.cfg.simulation.interval_seconds,
        )

        service = SimulationService(self.cfg, seed=self.cfg.simulation.seed)
        readings: list[SensorReading] = list(service.iter_readings(start=start, end=end))
        self._log.info("generated %d readings", len(readings))

        self.output_dir.mkdir(parents=True, exist_ok=True)

        exporter = HistoricalCsvExporter(
            self.output_dir, simulation_id=self.simulation_id
        )
        export = exporter.export(readings)
        self._log.info(
            "exported: readings_long=%d, uplinks_json=%d, devices=%d",
            export.readings_long_rows,
            export.uplinks_json_rows,
            export.devices_rows,
        )

        # Persist canonical readings for validate-history.
        readings_path = self.output_dir / "readings_internal.jsonl"
        dump_readings_jsonl(readings_path, readings)

        report = run_validation(
            readings,
            simulation_id=self.simulation_id,
            building={"type": "office"},
            scenarios=self.scenarios,
        )
        report_path = self.output_dir / "validation_report.json"
        report_path.write_text(
            json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8"
        )
        self._log.info(
            "validation: overall_score=%.2f, findings=%d → %s",
            report.overall_score, len(report.findings), report_path,
        )

        return HistoricalRunResult(
            output_dir=self.output_dir,
            readings_count=len(readings),
            export=export,
            report=report,
            readings_path=readings_path,
            report_path=report_path,
        )

    def validate_existing(
        self,
        *,
        readings_path: str | Path | None = None,
    ) -> ValidationReport:
        """Re-run validation against a previously generated dataset."""
        path = Path(readings_path or (self.output_dir / "readings_internal.jsonl"))
        readings = load_readings_jsonl(path)
        report = run_validation(
            readings,
            simulation_id=self.simulation_id,
            building={"type": "office"},
            scenarios=self.scenarios,
        )
        return report


__all__ = ["HistoricalRunner", "HistoricalRunResult"]
