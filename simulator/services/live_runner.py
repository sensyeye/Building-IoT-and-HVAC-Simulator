"""Live-mode runner: generate readings and publish to the Sensgreen broker.

The runner does not implement a real-time scheduler. It walks the
configured time range (or "now → now+duration") at the configured
cadence and publishes each reading via :class:`SensgreenMqttPublisher`.
That keeps the code identical for true live mode and for ``--dry-run``
where we just want to see what would be sent.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..integrations.sensgreen_mqtt_payload_builder import SensgreenMqttPayloadBuilder
from ..integrations.sensgreen_mqtt_publisher import SensgreenMqttPublisher
from ..models.config import SimulatorConfig
from .simulation_service import SimulationService


@dataclass
class LiveRunResult:
    published: int
    failed: int
    dry_run: bool


class LiveRunner:
    """Drive a simulation and publish each reading to MQTT.

    Parameters
    ----------
    cfg:
        Parsed config — ``cfg.outputs.mqtt`` is used to build the publisher.
    publisher:
        Optional pre-built publisher (test hook). When ``None`` the
        runner builds one from ``cfg.outputs.mqtt`` with the supplied
        ``dry_run`` flag.
    dry_run:
        Used only when ``publisher`` is None. Routes payloads to stdout
        + logger instead of opening a network connection.
    realtime:
        When ``True`` the runner sleeps between ticks so it actually
        publishes in real time. When ``False`` (default for tests and
        replays) it walks through the time range as fast as possible.
    """

    def __init__(
        self,
        cfg: SimulatorConfig,
        *,
        publisher: SensgreenMqttPublisher | None = None,
        dry_run: bool = False,
        realtime: bool = False,
        logger: logging.Logger | None = None,
    ) -> None:
        self.cfg = cfg
        self.dry_run = dry_run if publisher is None else publisher.dry_run
        self.realtime = bool(realtime)
        self._log = logger or logging.getLogger("sensgreen.live")
        self._owns_publisher = publisher is None
        self.publisher = publisher or SensgreenMqttPublisher.from_config(
            cfg.outputs.mqtt, dry_run=dry_run, logger=self._log,
        )
        self.builder = SensgreenMqttPayloadBuilder()

    # -- public API --------------------------------------------------------

    def run(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        duration_seconds: int | None = None,
    ) -> LiveRunResult:
        """Publish readings between ``start`` and ``end``.

        If neither bound is supplied, runs from "now" for either
        ``duration_seconds`` or one full ``interval_seconds`` tick.
        """
        if start is None:
            start = datetime.now(tz=timezone.utc)
        if end is None:
            secs = int(duration_seconds or self.cfg.simulation.interval_seconds)
            end = start + timedelta(seconds=max(secs, 1))

        service = SimulationService(self.cfg, seed=self.cfg.simulation.seed)

        published = 0
        failed = 0
        try:
            self.publisher.connect()
            for reading in service.iter_readings(start=start, end=end):
                if self.realtime:
                    self._sleep_until(reading.timestamp)
                try:
                    payload = self.builder.build(reading)
                except Exception as e:
                    self._log.error(
                        "skipping reading from %s: %s", reading.device_eui, e
                    )
                    failed += 1
                    continue
                result = self.publisher.publish(payload)
                if result.ok:
                    published += 1
                else:
                    failed += 1
        finally:
            if self._owns_publisher:
                self.publisher.disconnect()

        self._log.info(
            "live run finished: published=%d failed=%d dry_run=%s",
            published, failed, self.dry_run,
        )
        return LiveRunResult(published=published, failed=failed, dry_run=self.dry_run)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _sleep_until(target: datetime) -> None:  # pragma: no cover - timing
        now = datetime.now(tz=timezone.utc)
        delta = (target - now).total_seconds()
        if delta > 0:
            time.sleep(delta)


__all__ = ["LiveRunner", "LiveRunResult"]
