"""Per-project event log.

The event log is an append-only JSONL file at
``data/projects/<id>.events.jsonl``. Each line is a JSON object with at
minimum::

    {
      "ts": "2026-05-19T12:34:56+00:00",   # ISO-8601 UTC
      "project_id": "dubai-office-abc123",
      "kind": "historical_run" | "live_run" | "bridge_test" | "error",
      "status": "succeeded" | "failed" | "running",
      "summary": "human-readable one-liner",
      "details": { ... arbitrary, kind-specific ... }
    }

Runners and the bridge tester call :meth:`EventService.record` to
register events; the dashboard reads them back via :meth:`recent`.

We deliberately keep this dead simple: no rotation, no DB. The MVP
dataset is small (handfuls of events per project per day). If a project
file grows past a few MB we can move it to SQLite later behind the same
service interface.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data" / "projects"


VALID_KINDS = (
    "historical_run",
    "live_run",
    "bridge_test",
    "scenario",
    "integration",
    "config",
    "error",
)
VALID_STATUSES = ("succeeded", "failed", "running", "info")


@dataclass
class Event:
    ts: str
    project_id: str
    kind: str
    status: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        return cls(
            ts=str(data.get("ts", "")),
            project_id=str(data.get("project_id", "")),
            kind=str(data.get("kind", "")),
            status=str(data.get("status", "")),
            summary=str(data.get("summary", "")),
            details=dict(data.get("details") or {}),
        )


class EventService:
    """JSONL-backed event log, one file per project."""

    # One lock per file path. Cheap and correct for the single-process
    # dashboard; if we ever go multi-process we'd switch to fcntl/flock.
    _locks: dict[Path, threading.Lock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = Path(data_dir) if data_dir else DATA_DIR
        self._data_dir.mkdir(parents=True, exist_ok=True)

    # -- helpers -----------------------------------------------------------

    def _path(self, project_id: str) -> Path:
        return self._data_dir / f"{project_id}.events.jsonl"

    def _lock_for(self, path: Path) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(path)
            if lock is None:
                lock = threading.Lock()
                self._locks[path] = lock
            return lock

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    # -- public API --------------------------------------------------------

    def record(
        self,
        project_id: str,
        *,
        kind: str,
        status: str,
        summary: str,
        details: dict[str, Any] | None = None,
        ts: str | None = None,
    ) -> Event:
        """Append one event to ``<project_id>.events.jsonl``.

        ``ts`` is auto-set to "now" when missing. Unknown ``kind`` or
        ``status`` values raise ``ValueError`` so typos surface early.
        """
        if kind not in VALID_KINDS:
            raise ValueError(
                f"unknown event kind '{kind}'; expected one of {VALID_KINDS}"
            )
        if status not in VALID_STATUSES:
            raise ValueError(
                f"unknown event status '{status}'; expected one of {VALID_STATUSES}"
            )

        event = Event(
            ts=ts or self._now(),
            project_id=project_id,
            kind=kind,
            status=status,
            summary=summary,
            details=dict(details or {}),
        )
        path = self._path(project_id)
        line = json.dumps(event.to_dict(), default=str, sort_keys=True) + "\n"
        with self._lock_for(path):
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        return event

    def recent(
        self,
        project_id: str,
        *,
        limit: int = 50,
        kind: str | None = None,
        status: str | None = None,
        query: str | None = None,
    ) -> list[Event]:
        """Return the most recent events for ``project_id``, newest first.

        ``limit`` caps the number returned. ``kind`` and ``status``,
        when given, filter by those fields. ``query`` is a free-text
        case-insensitive substring match against the summary and the
        serialized details payload — handy for the dashboard's search
        box.
        """
        if limit <= 0:
            return []
        path = self._path(project_id)
        if not path.exists():
            return []
        q = (query or "").strip().lower() or None
        # Naive read: file is small. If it grows we'll switch to a tail
        # reader, but the dashboard doesn't need that today.
        events: list[Event] = []
        with self._lock_for(path):
            with path.open("r", encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    ev = Event.from_dict(data)
                    if kind is not None and ev.kind != kind:
                        continue
                    if status is not None and ev.status != status:
                        continue
                    if q is not None:
                        hay = ev.summary.lower()
                        if q not in hay:
                            # Fall back to JSON-serialized details so
                            # users can search e.g. by device EUI or a
                            # scenario name embedded in details.
                            try:
                                hay2 = json.dumps(ev.details, default=str).lower()
                            except (TypeError, ValueError):
                                hay2 = ""
                            if q not in hay2:
                                continue
                    events.append(ev)
        events.reverse()  # newest first
        return events[:limit]

    def clear(self, project_id: str) -> int:
        """Delete the event log file and return the number of events removed.

        Used by the dashboard's "Clear events" button. Safe to call when
        no log exists yet.
        """
        path = self._path(project_id)
        with self._lock_for(path):
            if not path.exists():
                return 0
            # Count lines for the return value (cheap; file is small).
            count = 0
            try:
                with path.open("r", encoding="utf-8") as fh:
                    for raw in fh:
                        if raw.strip():
                            count += 1
            except OSError:
                count = 0
            path.unlink()
            return count

    def truncate(self, project_id: str) -> None:
        """Delete the event log file. Used by tests."""
        path = self._path(project_id)
        with self._lock_for(path):
            if path.exists():
                path.unlink()


# Module-level singleton used by the API + runners.
event_service = EventService()


__all__ = ["Event", "EventService", "event_service", "VALID_KINDS", "VALID_STATUSES"]
