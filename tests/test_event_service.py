"""Tests for the per-project event log."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.services.event_service import Event, EventService


@pytest.fixture
def svc(tmp_path: Path) -> EventService:
    return EventService(data_dir=tmp_path)


def test_record_appends_jsonl(svc: EventService, tmp_path: Path) -> None:
    ev = svc.record(
        "proj-1",
        kind="bridge_test",
        status="succeeded",
        summary="all 9 devices published",
        details={"published_count": 9, "failed_count": 0},
    )
    assert isinstance(ev, Event)
    file = tmp_path / "proj-1.events.jsonl"
    assert file.exists()
    data = json.loads(file.read_text().strip())
    assert data["project_id"] == "proj-1"
    assert data["kind"] == "bridge_test"
    assert data["status"] == "succeeded"
    assert data["details"]["published_count"] == 9
    assert "T" in data["ts"]  # ISO-8601


def test_recent_newest_first_with_limit(svc: EventService) -> None:
    for i in range(5):
        svc.record(
            "p",
            kind="bridge_test",
            status="succeeded",
            summary=f"#{i}",
            details={"i": i},
        )
    out = svc.recent("p", limit=3)
    assert len(out) == 3
    assert [e.summary for e in out] == ["#4", "#3", "#2"]


def test_recent_filter_by_kind(svc: EventService) -> None:
    svc.record("p", kind="bridge_test", status="succeeded", summary="bridge a")
    svc.record("p", kind="historical_run", status="succeeded", summary="hist a")
    svc.record("p", kind="bridge_test", status="failed", summary="bridge b")
    out = svc.recent("p", kind="bridge_test")
    assert len(out) == 2
    assert all(e.kind == "bridge_test" for e in out)


def test_recent_empty_when_no_file(svc: EventService) -> None:
    assert svc.recent("missing") == []


def test_invalid_kind_rejected(svc: EventService) -> None:
    with pytest.raises(ValueError):
        svc.record("p", kind="party", status="succeeded", summary="x")


def test_invalid_status_rejected(svc: EventService) -> None:
    with pytest.raises(ValueError):
        svc.record("p", kind="bridge_test", status="ok", summary="x")


def test_corrupt_line_is_skipped(svc: EventService, tmp_path: Path) -> None:
    svc.record("p", kind="bridge_test", status="succeeded", summary="ok")
    # Append a garbage line.
    with (tmp_path / "p.events.jsonl").open("a", encoding="utf-8") as fh:
        fh.write("{not json}\n")
    svc.record("p", kind="bridge_test", status="failed", summary="oops")
    out = svc.recent("p")
    assert [e.summary for e in out] == ["oops", "ok"]


def test_truncate_removes_file(svc: EventService, tmp_path: Path) -> None:
    svc.record("p", kind="bridge_test", status="succeeded", summary="x")
    assert (tmp_path / "p.events.jsonl").exists()
    svc.truncate("p")
    assert not (tmp_path / "p.events.jsonl").exists()
    assert svc.recent("p") == []
