"""JSONL persistence for canonical :class:`SensorReading` objects.

Used by the historical runner so that ``validate-history`` can
re-validate a previously generated dataset without re-running the
simulation.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

from ..models.reading import SensorReading


def _to_dict(r: SensorReading) -> dict:
    return {
        "device_eui": r.device_eui,
        "sensor_type": r.sensor_type,
        "timestamp": r.timestamp.isoformat(),
        "data": r.data,
        "metadata": r.metadata,
    }


def _from_dict(d: dict) -> SensorReading:
    return SensorReading(
        device_eui=d["device_eui"],
        sensor_type=d["sensor_type"],
        timestamp=datetime.fromisoformat(d["timestamp"]),
        data=dict(d.get("data") or {}),
        metadata=dict(d.get("metadata") or {}),
    )


def dump_readings_jsonl(path: str | Path, readings: Iterable[SensorReading]) -> int:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with p.open("w", encoding="utf-8") as f:
        for r in readings:
            f.write(json.dumps(_to_dict(r), ensure_ascii=False))
            f.write("\n")
            count += 1
    return count


def load_readings_jsonl(path: str | Path) -> list[SensorReading]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    out: list[SensorReading] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(_from_dict(json.loads(line)))
    return out


def iter_readings_jsonl(path: str | Path) -> Iterator[SensorReading]:
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield _from_dict(json.loads(line))


__all__ = ["dump_readings_jsonl", "iter_readings_jsonl", "load_readings_jsonl"]
