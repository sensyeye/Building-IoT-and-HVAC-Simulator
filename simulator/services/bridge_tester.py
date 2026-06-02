"""Bridge tester: one-shot smoke test of the Sensgreen MQTT integration.

The bridge tester is a deliberately small piece of code: it publishes
one synthetic payload **per device** in a project, subscribes to the
configured ``error_topic`` (if any), and returns a structured result the
CLI and dashboard can both render.

It exists to answer one question fast::

    "If I click Run-Live now, will my data actually land in Sensgreen?"

Implementation notes
--------------------
* Generates exactly one :class:`SensorReading` per IAQ device using the
  matching simulator, so the payload shape is realistic (not a stub
  ``{"x": 1}``).
* Devices without an IAQ simulator (energy meters, counters, …) get a
  minimal placeholder payload with ``deviceEui`` + ``timestamp`` + a
  single canonical metric, so the broker exercise is identical for them.
* Waits ``error_listen_seconds`` (default 5 s) after the last publish to
  collect any error-topic messages. Sensgreen's broker rejects messages
  asynchronously — we cannot rely on the publish ACK alone.
* Never raises on broker errors; collects them into the result instead.
  The caller decides whether to surface them as failures.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from ..integrations.sensgreen_mqtt_payload_builder import SensgreenMqttPayloadBuilder
from ..integrations.sensgreen_mqtt_publisher import SensgreenMqttPublisher
from ..models.config import DeviceConfig, SimulatorConfig
from ..models.reading import SensorReading


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class DeviceBridgeResult:
    device_eui: str
    device_name: str
    sensor_type: str
    published: bool
    topic: str
    error: str | None = None
    payload_sample: dict[str, Any] | None = None


@dataclass
class BridgeTestResult:
    project_id: str | None
    host: str
    port: int
    topic: str
    error_topic: str | None
    dry_run: bool
    devices: list[DeviceBridgeResult] = field(default_factory=list)
    broker_errors: list[dict[str, Any]] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    @property
    def published_count(self) -> int:
        return sum(1 for d in self.devices if d.published)

    @property
    def failed_count(self) -> int:
        return sum(1 for d in self.devices if not d.published)

    @property
    def all_ok(self) -> bool:
        return self.failed_count == 0 and not self.broker_errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "host": self.host,
            "port": self.port,
            "topic": self.topic,
            "error_topic": self.error_topic,
            "dry_run": self.dry_run,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "published_count": self.published_count,
            "failed_count": self.failed_count,
            "broker_errors": self.broker_errors,
            "all_ok": self.all_ok,
            "devices": [
                {
                    "device_eui": d.device_eui,
                    "device_name": d.device_name,
                    "sensor_type": d.sensor_type,
                    "published": d.published,
                    "topic": d.topic,
                    "error": d.error,
                    "payload_sample": d.payload_sample,
                }
                for d in self.devices
            ],
        }


# ---------------------------------------------------------------------------
# Tester
# ---------------------------------------------------------------------------


class BridgeTester:
    """Run a one-shot bridge test.

    Parameters
    ----------
    cfg:
        Parsed :class:`SimulatorConfig` — used for the device roster.
    publisher:
        Pre-built publisher. The caller decides whether it came from
        ``cfg.outputs.mqtt`` or from a per-project integration dict.
    error_listen_seconds:
        How long to wait after the last publish before declaring "no
        errors". 5 s is enough for Sensgreen's broker round-trip; tests
        override to ``0``.
    """

    # Minimal-but-valid sample for sensor types that don't have a quick
    # one-shot simulator wired up. Every value here is in the Sensgreen
    # metric table.
    _PLACEHOLDER_DATA: Mapping[str, dict[str, float | bool]] = {
        "iaq": {"temperature": 22.0, "humidity": 45.0, "co2": 600.0},
        "energy_meter": {"active_power": 1.2, "voltage_1": 230.0},
        "people_counter": {"people_count": 0.0},
        "entry_exit_counter": {"periodic_counter_in": 0.0, "periodic_counter_out": 0.0},
        "occupancy_sensor": {"occupancy": False},
        "door_contact": {"door_state": False, "periodic_open_events": 0.0},
        "hvac": {"setpoint_c": 22.0, "fan_speed_pct": 0.0, "supply_temp_c": 22.0},
    }

    def __init__(
        self,
        cfg: SimulatorConfig,
        publisher: SensgreenMqttPublisher,
        *,
        builder: SensgreenMqttPayloadBuilder | None = None,
        error_listen_seconds: float = 5.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self.cfg = cfg
        self.publisher = publisher
        self.builder = builder or SensgreenMqttPayloadBuilder()
        self.error_listen_seconds = float(error_listen_seconds)
        self._log = logger or logging.getLogger("sensgreen.bridge")
        self._error_lock = threading.Lock()
        self._error_messages: list[dict[str, Any]] = []

    # -- public API --------------------------------------------------------

    def run(self, *, project_id: str | None = None) -> BridgeTestResult:
        now = lambda: datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
        started = now()

        # Subscribe to the error topic *before* publishing anything so
        # we don't miss the first rejection.
        if self.publisher.error_topic:
            self.publisher.set_error_callback(self._on_error_message)

        try:
            self.publisher.connect()
            device_results = [self._test_device(d) for d in self.cfg.devices]
            if self.error_listen_seconds > 0 and self.publisher.error_topic:
                time.sleep(self.error_listen_seconds)
        finally:
            try:
                self.publisher.disconnect()
            except Exception:  # pragma: no cover - defensive
                pass

        with self._error_lock:
            broker_errors = list(self._error_messages)

        return BridgeTestResult(
            project_id=project_id,
            host=self.publisher.host,
            port=self.publisher.port,
            topic=self.publisher.topic_template,
            error_topic=self.publisher.error_topic,
            dry_run=self.publisher.dry_run,
            devices=device_results,
            broker_errors=broker_errors,
            started_at=started,
            finished_at=now(),
        )

    # -- internal ----------------------------------------------------------

    def _test_device(self, device: DeviceConfig) -> DeviceBridgeResult:
        sample = self._placeholder_data(device.type)
        if sample is None:
            return DeviceBridgeResult(
                device_eui=device.device_eui,
                device_name=device.name,
                sensor_type=device.type,
                published=False,
                topic="",
                error=f"no payload template for sensor type '{device.type}'",
            )

        reading = SensorReading(
            device_eui=device.device_eui,
            sensor_type=device.type,
            timestamp=datetime.now(tz=timezone.utc),
            data=dict(sample),
            metadata={"zone_id": device.zone_id} if device.zone_id else {},
        )

        try:
            payload = self.builder.build(reading)
        except Exception as exc:
            return DeviceBridgeResult(
                device_eui=device.device_eui,
                device_name=device.name,
                sensor_type=device.type,
                published=False,
                topic="",
                error=f"payload build failed: {exc}",
            )

        topic = self.publisher.topic_for(payload)
        try:
            result = self.publisher.publish(payload)
        except Exception as exc:
            return DeviceBridgeResult(
                device_eui=device.device_eui,
                device_name=device.name,
                sensor_type=device.type,
                published=False,
                topic=topic,
                error=f"publish failed: {exc}",
                payload_sample=payload,
            )

        if not result.ok:
            return DeviceBridgeResult(
                device_eui=device.device_eui,
                device_name=device.name,
                sensor_type=device.type,
                published=False,
                topic=topic,
                error=f"broker rejected publish (rc={result.rc})",
                payload_sample=payload,
            )

        return DeviceBridgeResult(
            device_eui=device.device_eui,
            device_name=device.name,
            sensor_type=device.type,
            published=True,
            topic=topic,
            payload_sample=payload,
        )

    def _placeholder_data(self, sensor_type: str) -> dict[str, Any] | None:
        sample = self._PLACEHOLDER_DATA.get(sensor_type)
        return dict(sample) if sample else None

    def _on_error_message(self, topic: str, payload: bytes) -> None:
        try:
            body = payload.decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - defensive
            body = repr(payload)
        with self._error_lock:
            self._error_messages.append({"topic": topic, "body": body})


__all__ = [
    "BridgeTester",
    "BridgeTestResult",
    "DeviceBridgeResult",
]
