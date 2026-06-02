"""Background live-mode controller for the dashboard.

This module exists because the existing :class:`LiveRunner` is a one-shot
"walk this time range and publish" thing. The dashboard needs:

* a background thread per project that ticks "now → now+interval" forever,
* a way for the SSE endpoint to subscribe to each reading as it happens,
* clean start / stop with status, without blocking the HTTP worker.

Design choices
--------------
* One :class:`LiveSession` per project. Sessions live in a process-wide
  :class:`LiveRunController` registry keyed by ``project_id``.
* A session owns a worker thread. The thread loops: build readings for
  one interval, publish them, push each to the in-memory ring buffer
  and fan-out to async subscribers, sleep until next tick.
* The publisher is built via the existing factory helpers
  (``SensgreenMqttPublisher.from_integration`` / ``from_config``) so the
  network behaviour is identical to a CLI ``run-live``.
* Events are recorded into the per-project event log so the Events tab
  stays the single source of truth.

The session never raises out to the controller — any exception during a
tick is logged, recorded as a ``live_run`` event with ``status="failed"``,
and stops the session.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from ..config_loader import load_config
from ..integrations.sensgreen_mqtt_payload_builder import SensgreenMqttPayloadBuilder
from ..integrations.sensgreen_mqtt_publisher import SensgreenMqttPublisher
from ..models.config import SimulatorConfig
from ..scenarios.micro_events import (
    MicroEventEngine,
    get_event_template,
)
from .simulation_service import SimulationService


# ---------------------------------------------------------------------------
# Ring buffer + subscriber fan-out
# ---------------------------------------------------------------------------


@dataclass
class StreamItem:
    """One reading-shaped payload pushed to the stream."""

    ts: str
    device_eui: str
    device_name: str
    sensor_type: str
    zone_id: str | None
    data: dict[str, Any]
    published: bool
    error: str | None = None
    # Scenario annotations that explain *why* this reading looks the
    # way it does. Each entry is ``{"id", "name", "category", "why"}``.
    # Empty when no scenarios are active or the sensor type is outside
    # the scenario's curated impact list.
    scenarios: list[dict[str, str]] = field(default_factory=list)
    # Micro-events currently active for this reading's zone (printer
    # plume, cleaning spray, door-open, etc). Each entry is
    # ``{"id", "name", "category", "description", "started_at"}``.
    micro_events: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "device_eui": self.device_eui,
            "device_name": self.device_name,
            "sensor_type": self.sensor_type,
            "zone_id": self.zone_id,
            "data": dict(self.data),
            "published": self.published,
            "error": self.error,
            "scenarios": list(self.scenarios),
            "micro_events": list(self.micro_events),
        }


class StreamBuffer:
    """Bounded in-memory ring buffer with async subscriber fan-out.

    The worker thread calls :meth:`push` to add an item. SSE subscribers
    register a :class:`asyncio.Queue` via :meth:`subscribe`; the buffer
    schedules ``put_nowait`` on the caller's event loop for each item.
    Subscribers must :meth:`unsubscribe` when done so we don't leak.
    """

    def __init__(self, maxlen: int = 200) -> None:
        self._items: deque[StreamItem] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []

    # -- writer side (called from worker thread) ---------------------------

    def push(self, item: StreamItem) -> None:
        with self._lock:
            self._items.append(item)
            subs = list(self._subscribers)
        # Fan out to each subscriber's loop. Drop on full queue rather
        # than block — a slow client must not stall the worker.
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(_safe_put, q, item)
            except RuntimeError:
                # Loop has been closed; the subscriber will be cleaned
                # up by its own teardown.
                continue

    # -- reader side (called from request handlers) ------------------------

    def snapshot(self) -> list[StreamItem]:
        with self._lock:
            return list(self._items)

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=512)
        with self._lock:
            self._subscribers.append((loop, q))
        return q

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers = [
                (loop, q) for (loop, q) in self._subscribers if q is not queue
            ]

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


def _safe_put(queue: asyncio.Queue, item: StreamItem) -> None:
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        # Drop the oldest item to make room; SSE is best-effort.
        try:
            queue.get_nowait()
            queue.put_nowait(item)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Live session (per project)
# ---------------------------------------------------------------------------


SessionEventCallback = Callable[[str, str, str, dict[str, Any]], None]
# (project_id, status, summary, details) — used to hook events into the
# EventService without importing api.* here.


@dataclass
class LiveSessionStatus:
    project_id: str
    state: str  # "starting" | "running" | "stopping" | "stopped" | "failed"
    started_at: str | None
    stopped_at: str | None
    ticks: int
    published: int
    failed: int
    dry_run: bool
    interval_seconds: int
    host: str
    port: int
    topic: str
    error: str | None = None
    subscribers: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "state": self.state,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "ticks": self.ticks,
            "published": self.published,
            "failed": self.failed,
            "dry_run": self.dry_run,
            "interval_seconds": self.interval_seconds,
            "host": self.host,
            "port": self.port,
            "topic": self.topic,
            "error": self.error,
            "subscribers": self.subscribers,
        }


class LiveSession:
    """A background simulation that publishes readings on a fixed cadence.

    Parameters
    ----------
    project_id:
        Owning project's id; used to scope event records.
    cfg:
        Parsed simulator config.
    publisher:
        Pre-built publisher (dry-run or real). The session owns its
        lifecycle: ``connect()`` on start, ``disconnect()`` on stop.
    on_event:
        Optional callback invoked for lifecycle events. Receives
        ``(project_id, status, summary, details)`` so callers can route
        these to the EventService without circular imports.
    """

    def __init__(
        self,
        project_id: str,
        cfg: SimulatorConfig,
        publisher: SensgreenMqttPublisher,
        *,
        on_event: SessionEventCallback | None = None,
        buffer_size: int = 200,
        logger: logging.Logger | None = None,
        scenario_assignments: list[Mapping[str, Any]] | None = None,
    ) -> None:
        self.project_id = project_id
        self.cfg = cfg
        self.publisher = publisher
        self._on_event = on_event
        self._log = logger or logging.getLogger("sensgreen.live")

        self.buffer = StreamBuffer(maxlen=buffer_size)
        self.builder = SensgreenMqttPayloadBuilder()
        # Per-session micro-event engine, seeded from cfg.simulation.seed
        # (or a time-derived seed if absent) so each live session
        # generates a different sequence of incidents.
        # ``probability_scale`` is bumped so an interactive demo (a few
        # minutes of watching the Live tab) actually sees a handful of
        # incidents — the per-template rates are tuned for "realistic
        # over a working day" which would otherwise be too sparse.
        engine_seed = cfg.simulation.seed
        if engine_seed is None:
            engine_seed = int(time.time())
        self.event_engine = MicroEventEngine(
            seed=engine_seed, probability_scale=6.0
        )
        self._service = SimulationService(
            cfg, seed=cfg.simulation.seed, micro_event_engine=self.event_engine
        )

        # Scenario annotations are pulled from the project's
        # ``<id>.scenarios.json`` file by the API layer and passed in
        # here. Keeping this dependency-injected means the simulator
        # package stays unaware of api.services.project_service.
        self._scenario_assignments: list[Mapping[str, Any]] = list(
            scenario_assignments or []
        )

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Re-entrant so methods like ``stop()`` can call ``status()``
        # while still holding the lock without deadlocking.
        self._lock = threading.RLock()

        # status
        self._state: str = "stopped"
        self._started_at: str | None = None
        self._stopped_at: str | None = None
        self._ticks: int = 0
        self._published: int = 0
        self._failed: int = 0
        self._error: str | None = None

    # -- public API --------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> LiveSessionStatus:
        with self._lock:
            return LiveSessionStatus(
                project_id=self.project_id,
                state=self._state,
                started_at=self._started_at,
                stopped_at=self._stopped_at,
                ticks=self._ticks,
                published=self._published,
                failed=self._failed,
                dry_run=self.publisher.dry_run,
                interval_seconds=int(self.cfg.simulation.interval_seconds),
                host=self.publisher.host,
                port=self.publisher.port,
                topic=self.publisher.topic_template,
                error=self._error,
                subscribers=self.buffer.subscriber_count(),
            )

    def start(self) -> LiveSessionStatus:
        with self._lock:
            if self.is_running:
                return self.status()
            self._stop.clear()
            self._state = "starting"
            self._started_at = _now_iso()
            self._stopped_at = None
            self._ticks = 0
            self._published = 0
            self._failed = 0
            self._error = None
            self._thread = threading.Thread(
                target=self._run, name=f"live-{self.project_id}", daemon=True
            )
            self._thread.start()
        self._emit("running", "live session started", {"dry_run": self.publisher.dry_run})
        return self.status()

    def stop(self, *, timeout: float = 5.0) -> LiveSessionStatus:
        with self._lock:
            if not self.is_running:
                return self.status()
            self._state = "stopping"
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        return self.status()

    # -- worker ------------------------------------------------------------

    def _run(self) -> None:
        interval = max(int(self.cfg.simulation.interval_seconds), 1)
        try:
            self.publisher.connect()
            with self._lock:
                self._state = "running"
        except Exception as exc:  # pragma: no cover - network branch
            self._log.exception("live session connect failed: %s", exc)
            with self._lock:
                self._state = "failed"
                self._stopped_at = _now_iso()
                self._error = f"connect failed: {exc}"
            self._emit("failed", "live session failed to connect",
                       {"error": str(exc)})
            return

        try:
            while not self._stop.is_set():
                tick_start = datetime.now(tz=timezone.utc)
                self._tick(tick_start, tick_start + timedelta(seconds=interval))
                with self._lock:
                    self._ticks += 1
                # Sleep but wake early if asked to stop.
                if self._stop.wait(interval):
                    break
        except Exception as exc:
            self._log.exception("live session crashed: %s", exc)
            with self._lock:
                self._state = "failed"
                self._error = f"crash: {exc}"
            self._emit("failed", "live session crashed", {"error": str(exc)})
        finally:
            try:
                self.publisher.disconnect()
            except Exception:  # pragma: no cover - defensive
                pass
            with self._lock:
                if self._state not in ("failed",):
                    self._state = "stopped"
                self._stopped_at = _now_iso()
            self._emit(
                "succeeded" if self._error is None else "failed",
                "live session stopped",
                {
                    "ticks": self._ticks,
                    "published": self._published,
                    "failed": self._failed,
                    "error": self._error,
                },
            )

    def _tick(self, start: datetime, end: datetime) -> None:
        for reading in self._service.iter_readings(start=start, end=end):
            if self._stop.is_set():
                break
            try:
                payload = self.builder.build(reading)
            except Exception as exc:
                self._log.error("payload build failed for %s: %s",
                                reading.device_eui, exc)
                with self._lock:
                    self._failed += 1
                self.buffer.push(StreamItem(
                    ts=reading.timestamp.isoformat(timespec="seconds"),
                    device_eui=reading.device_eui,
                    device_name=self._device_name(reading.device_eui),
                    sensor_type=reading.sensor_type,
                    zone_id=reading.metadata.get("zone_id") if reading.metadata else None,
                    data=reading.data,
                    published=False,
                    error=f"build: {exc}",
                    scenarios=self._scenarios_for(reading),
                    micro_events=self._micro_events_for(reading),
                ))
                continue

            result = self.publisher.publish(payload)
            with self._lock:
                if result.ok:
                    self._published += 1
                else:
                    self._failed += 1

            self.buffer.push(StreamItem(
                ts=reading.timestamp.isoformat(timespec="seconds"),
                device_eui=reading.device_eui,
                device_name=self._device_name(reading.device_eui),
                sensor_type=reading.sensor_type,
                zone_id=reading.metadata.get("zone_id") if reading.metadata else None,
                data=reading.data,
                published=result.ok,
                error=None if result.ok else f"rc={result.rc}",
                scenarios=self._scenarios_for(reading),
                micro_events=self._micro_events_for(reading),
            ))
        # After the tick we emit log entries for newly started /
        # finished micro-events. Doing this per-tick (not per-reading)
        # avoids duplicate events when several devices observe the
        # same incident in the same zone.
        self._flush_micro_events()

    def _flush_micro_events(self) -> None:
        for ev in self.event_engine.pop_started_events():
            tpl = get_event_template(ev.template_id)
            name = tpl.name if tpl else ev.template_id
            self._emit(
                "info",
                f"micro-event started: {name}",
                {
                    "kind": "micro_event_start",
                    "event_id": ev.template_id,
                    "zone_id": ev.zone_id,
                    "started_at": ev.started_at.isoformat(timespec="seconds"),
                    "duration_min": ev.duration_min,
                    "category": tpl.category if tpl else "unknown",
                    "channels": list(ev.peaks.keys()),
                },
            )
        for ev in self.event_engine.pop_ended_events():
            tpl = get_event_template(ev.template_id)
            name = tpl.name if tpl else ev.template_id
            self._emit(
                "info",
                f"micro-event ended: {name}",
                {
                    "kind": "micro_event_end",
                    "event_id": ev.template_id,
                    "zone_id": ev.zone_id,
                    "started_at": ev.started_at.isoformat(timespec="seconds"),
                },
            )

    def _micro_events_for(self, reading) -> list[dict[str, str]]:
        """Resolve micro-event annotations for one reading."""
        if not reading.metadata or "micro_events" not in reading.metadata:
            return []
        out: list[dict[str, str]] = []
        for entry in reading.metadata["micro_events"]:
            tpl = get_event_template(entry["id"])
            if tpl is None:
                continue
            out.append({
                "id": tpl.id,
                "name": tpl.name,
                "category": tpl.category,
                "description": tpl.description,
                "started_at": entry["started_at"],
            })
        return out

    def _scenarios_for(self, reading) -> list[dict[str, str]]:
        """Resolve scenario annotations for one reading.

        Honours per-assignment zone targeting (``target_hvac_zone_id`` /
        ``target_zone_ids``): untargeted assignments still fan out to
        every reading; targeted assignments only annotate readings from
        zones in their resolved target set.

        Lazy-imported to keep the live-session module independent of
        the scenarios package's import time on cold start.
        """
        if not self._scenario_assignments:
            return []
        from ..scenarios import (
            active_scenario_assignments_at,
            explain_reading,
            resolve_assignment_zone_targets,
        )
        active = active_scenario_assignments_at(
            reading.timestamp, self._scenario_assignments
        )
        if not active:
            return []
        reading_zone = (
            reading.metadata.get("zone_id") if reading.metadata else None
        )
        zone_to_hvac = self._zone_to_hvac_map()
        sids: list[str] = []
        for a in active:
            targets = resolve_assignment_zone_targets(a, zone_to_hvac)
            if targets is not None:
                if reading_zone is None or reading_zone not in targets:
                    continue
            sids.append(str(a.get("id") or ""))
        if not sids:
            return []
        return explain_reading(reading.sensor_type, sids)

    def _zone_to_hvac_map(self) -> dict[str, str | None]:
        """Cache ``{zone_id: hvac_zone_id}`` lookups for the active config."""
        cached = getattr(self, "_zone_hvac_cache", None)
        if cached is not None:
            return cached
        mapping: dict[str, str | None] = {}
        zones = getattr(self.cfg.building, "zones", ()) if self.cfg else ()
        for z in zones:
            mapping[z.id] = getattr(z, "hvac_zone_id", None)
        self._zone_hvac_cache = mapping
        return mapping

    def _device_name(self, eui: str) -> str:
        for d in self.cfg.devices:
            if d.device_eui == eui:
                return d.name
        return eui

    def _emit(self, status: str, summary: str, details: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(self.project_id, status, summary, details)
        except Exception:  # pragma: no cover - defensive
            self._log.exception("on_event callback raised")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Process-wide controller
# ---------------------------------------------------------------------------


class LiveRunController:
    """Process-wide registry of :class:`LiveSession` keyed by project id."""

    def __init__(self) -> None:
        self._sessions: dict[str, LiveSession] = {}
        self._lock = threading.Lock()

    # -- session lifecycle -------------------------------------------------

    def start(
        self,
        project_id: str,
        cfg: SimulatorConfig,
        publisher: SensgreenMqttPublisher,
        *,
        on_event: SessionEventCallback | None = None,
        buffer_size: int = 200,
        scenario_assignments: list[Mapping[str, Any]] | None = None,
    ) -> LiveSessionStatus:
        """Start (or restart) a live session for ``project_id``.

        If a session is already running it is left alone and its current
        status is returned. Stop it first if you want fresh config.
        """
        with self._lock:
            existing = self._sessions.get(project_id)
            if existing is not None and existing.is_running:
                return existing.status()
            session = LiveSession(
                project_id, cfg, publisher,
                on_event=on_event, buffer_size=buffer_size,
                scenario_assignments=scenario_assignments,
            )
            self._sessions[project_id] = session
        return session.start()

    def stop(self, project_id: str) -> LiveSessionStatus | None:
        with self._lock:
            session = self._sessions.get(project_id)
        if session is None:
            return None
        return session.stop()

    def status(self, project_id: str) -> LiveSessionStatus | None:
        with self._lock:
            session = self._sessions.get(project_id)
        if session is None:
            return None
        return session.status()

    def get(self, project_id: str) -> LiveSession | None:
        with self._lock:
            return self._sessions.get(project_id)

    def list(self) -> list[LiveSessionStatus]:
        with self._lock:
            sessions = list(self._sessions.values())
        return [s.status() for s in sessions]

    def stop_all(self) -> None:
        """Stop every running session — used at app shutdown / in tests."""
        with self._lock:
            sessions = list(self._sessions.values())
        for s in sessions:
            try:
                s.stop()
            except Exception:  # pragma: no cover - defensive
                pass


# Module-level singleton used by the API layer.
live_controller = LiveRunController()


__all__ = [
    "LiveRunController",
    "LiveSession",
    "LiveSessionStatus",
    "StreamBuffer",
    "StreamItem",
    "live_controller",
]
