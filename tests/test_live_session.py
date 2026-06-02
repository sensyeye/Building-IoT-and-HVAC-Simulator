"""Unit tests for the live-mode session + ring buffer.

The live-mode worker thread publishes via a publisher instance. The
real :class:`SensgreenMqttPublisher` prints from inside the worker
thread when ``dry_run=True``, which deadlocks pytest's stdout capture.
The tests below use a tiny in-memory fake publisher instead.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from simulator.config_loader import load_config
from simulator.services.live_session import (
    LiveRunController,
    LiveSession,
    StreamBuffer,
    StreamItem,
)


CONFIG_PATH = Path(__file__).resolve().parent.parent / "configs" / "diagnostic_small_office.yaml"


# ---------------------------------------------------------------------------
# Fake publisher (silent, in-memory)
# ---------------------------------------------------------------------------


@dataclass
class _FakeResult:
    ok: bool = True
    rc: int = 0


class FakePublisher:
    """Minimal stand-in for SensgreenMqttPublisher used by the worker thread.

    No prints, no network. Captures published payloads in :attr:`sent`.
    """

    host = "fake"
    port = 0
    topic_template = "t/fake"
    dry_run = True

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def publish(self, payload):  # signature matches the real publisher
        self.sent.append(payload)
        return _FakeResult(ok=True, rc=0)


# ---------------------------------------------------------------------------
# StreamBuffer
# ---------------------------------------------------------------------------


def _make_item(eui: str = "AA01") -> StreamItem:
    return StreamItem(
        ts="2025-01-01T00:00:00+00:00",
        device_eui=eui,
        device_name="dev",
        sensor_type="iaq",
        zone_id="z1",
        data={"co2": 500},
        published=True,
    )


def test_streambuffer_snapshot_respects_maxlen():
    buf = StreamBuffer(maxlen=3)
    for i in range(5):
        buf.push(_make_item(f"AA{i:02d}"))
    snap = buf.snapshot()
    assert [it.device_eui for it in snap] == ["AA02", "AA03", "AA04"]


def test_streambuffer_subscribe_receives_pushes():
    async def run():
        buf = StreamBuffer(maxlen=10)
        loop = asyncio.get_running_loop()
        q = buf.subscribe(loop)
        assert buf.subscriber_count() == 1

        buf.push(_make_item("AA01"))
        buf.push(_make_item("AA02"))

        a = await asyncio.wait_for(q.get(), timeout=1.0)
        b = await asyncio.wait_for(q.get(), timeout=1.0)
        assert a.device_eui == "AA01"
        assert b.device_eui == "AA02"

        buf.unsubscribe(q)
        assert buf.subscriber_count() == 0

    asyncio.run(run())


# ---------------------------------------------------------------------------
# LiveSession lifecycle
# ---------------------------------------------------------------------------


def test_live_session_start_tick_and_stop():
    cfg = load_config(str(CONFIG_PATH))
    pub = FakePublisher()
    events: list[tuple[str, str]] = []

    def on_event(pid, status, summary, details):
        events.append((status, summary))

    session = LiveSession("proj-test", cfg, pub, on_event=on_event)
    try:
        status = session.start()
        assert status.state in ("starting", "running")

        deadline = time.time() + 5.0
        while time.time() < deadline:
            s = session.status()
            if s.ticks >= 1 and s.published >= 1:
                break
            time.sleep(0.05)
        s = session.status()
        assert s.ticks >= 1
        assert s.published >= 1
        assert pub.connected is True
        assert len(pub.sent) >= 1
        assert session.buffer.snapshot(), "buffer should contain at least one item"

        stopped = session.stop(timeout=5.0)
        assert stopped.state in ("stopped", "succeeded")
        assert pub.connected is False
    finally:
        session.stop(timeout=5.0)

    statuses = [e[0] for e in events]
    assert "running" in statuses
    assert any(s in ("succeeded", "failed") for s in statuses)


def test_live_session_annotates_readings_with_scenarios():
    """When a scenario is enabled, matching readings carry annotations."""
    cfg = load_config(str(CONFIG_PATH))
    pub = FakePublisher()
    assignments = [
        {"id": "meeting_room_poor_ventilation", "enabled": True},
        {"id": "outdoor_pm_event", "enabled": True},
        # Disabled scenarios must never leak through.
        {"id": "after_hours_energy_waste", "enabled": False},
    ]
    session = LiveSession(
        "proj-scenarios", cfg, pub, scenario_assignments=assignments,
    )
    try:
        session.start()
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if session.buffer.snapshot():
                break
            time.sleep(0.05)
        items = session.buffer.snapshot()
        assert items, "expected at least one streamed reading"

        iaq_items = [it for it in items if it.sensor_type == "iaq"]
        assert iaq_items, "diagnostic config should produce IAQ readings"
        annotated = [it for it in iaq_items if it.scenarios]
        assert annotated, "IAQ readings should be annotated with scenarios"

        for it in items:
            ids = {s["id"] for s in it.scenarios}
            assert "after_hours_energy_waste" not in ids

        for s in annotated[0].scenarios:
            assert set(s.keys()) >= {"id", "name", "category", "why"}
    finally:
        session.stop(timeout=5.0)


def test_live_run_controller_idempotent_start():
    cfg = load_config(str(CONFIG_PATH))
    pub = FakePublisher()
    ctrl = LiveRunController()
    try:
        s1 = ctrl.start("proj-x", cfg, pub)
        s2 = ctrl.start("proj-x", cfg, pub)  # already running -> no-op
        assert s1.project_id == s2.project_id
        assert ctrl.get("proj-x") is not None

        stopped = ctrl.stop("proj-x")
        assert stopped is not None
        assert stopped.state in ("stopped", "succeeded", "failed")

        assert ctrl.stop("never-started") is None
        assert ctrl.status("never-started") is None
    finally:
        ctrl.stop_all()
