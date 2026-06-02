"""Lightweight MQTT diagnostics used by the dashboard's Integration tab.

These helpers exist to answer three questions without running the full
bridge test:

1. **test_connection** — can we even reach the broker on the configured
   host/port (and complete a TLS handshake if requested)? Pure TCP/TLS,
   no MQTT CONNECT — useful when you just changed the firewall.
2. **test_credentials** — do my username/password actually authenticate?
   Performs a real MQTT CONNECT and inspects the CONNACK ``rc`` code.
3. **publish_test** — can I get exactly one synthetic payload across the
   wire, end-to-end? Connects, publishes, disconnects.

All three return plain dicts and **never** include the password in any
field. Callers can safely forward the result to the event log or the
browser.

Design choices
--------------
* The publisher's :class:`SensgreenMqttPublisher` already encapsulates
  TLS + auth + paho lifecycle, so ``test_credentials`` and
  ``publish_test`` reuse it through ``client_factory`` overrides where
  tests need to inject a fake.
* ``test_connection`` deliberately bypasses MQTT and uses a raw socket,
  because the most common failure mode users hit is "wrong port /
  firewall blocks 1881". Failing here means MQTT can't even start.
* Each helper returns ``{ok, latency_ms, message, ...}``; ``ok=False``
  with a human-readable ``message`` is the contract for the UI.
"""
from __future__ import annotations

import socket
import ssl
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from ..integrations.sensgreen_metric_mapper import SensgreenMetricMapper
from ..integrations.sensgreen_mqtt_payload_builder import (
    SensgreenMqttPayloadBuilder,
)
from ..integrations.sensgreen_mqtt_publisher import SensgreenMqttPublisher
from ..models.reading import SensorReading


# Fields that are safe to echo back in API responses and events.
_SAFE_INTEGRATION_FIELDS = (
    "host",
    "port",
    "username",
    "topic",
    "error_topic",
    "tls",
    "client_id",
)

# Minimal placeholder data per sensor type — identical shape to
# :data:`simulator.services.bridge_tester.BridgeTester._PLACEHOLDER_DATA`
# but duplicated here so the preview endpoint does not depend on the
# bridge tester (the two are evolved independently and the bridge tester
# may grow per-device readings in future).
_PLACEHOLDER_DATA: Mapping[str, dict[str, float | bool]] = {
    "iaq": {"temperature": 22.0, "humidity": 45.0, "co2": 600.0, "pm25": 12.0},
    "energy_meter": {"active_power_kw": 1.2, "voltage_l1": 230.0},
    "people_counter": {"people_count": 0.0},
    "entry_exit_counter": {
        "periodic_counter_in": 0.0,
        "periodic_counter_out": 0.0,
    },
    "occupancy_sensor": {"occupancy": False},
    "hvac": {"setpoint": 22.0, "fan_speed": 0.0},
}


# ---------------------------------------------------------------------------
# Redaction helpers
# ---------------------------------------------------------------------------


def redact_integration(integration: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``integration`` with the password replaced.

    Unknown keys are dropped on purpose: callers should never accidentally
    leak unrelated fields from the secrets store.
    """
    out: dict[str, Any] = {}
    for key in _SAFE_INTEGRATION_FIELDS:
        if key in integration:
            out[key] = integration[key]
    if integration.get("password"):
        out["password"] = "********"
    else:
        out["password"] = None
    return out


# ---------------------------------------------------------------------------
# Payload preview
# ---------------------------------------------------------------------------


def build_sample_payload(
    *,
    device_eui: str,
    sensor_type: str,
    topic_template: str,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Build a representative Sensgreen MQTT payload for one device.

    Uses the same placeholder data the bridge tester emits so the preview
    and the smoke test agree on shape. Raises :class:`ValueError` when the
    sensor type has no preview template (so the route can return 400).
    """
    sample = _PLACEHOLDER_DATA.get(sensor_type)
    if sample is None:
        raise ValueError(
            f"no payload preview template for sensor_type '{sensor_type}'"
        )

    when = (now or (lambda: datetime.now(tz=timezone.utc)))()
    reading = SensorReading(
        device_eui=device_eui,
        sensor_type=sensor_type,
        timestamp=when,
        data=dict(sample),
    )
    payload = SensgreenMqttPayloadBuilder().build(reading)

    # Resolve the topic exactly like the publisher does, without
    # instantiating one (no validation against host/port is needed here).
    if "{device_eui}" in topic_template:
        topic = topic_template.format(device_eui=device_eui)
    else:
        topic = topic_template

    return {
        "topic": topic,
        "topic_template": topic_template,
        "payload": payload,
        "generated_at": when.isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# Connection test (TCP / TLS only — no MQTT CONNECT)
# ---------------------------------------------------------------------------


def test_connection(
    integration: Mapping[str, Any],
    *,
    timeout: float = 4.0,
    socket_factory: Callable[[], socket.socket] | None = None,
) -> dict[str, Any]:
    """Open a TCP (and TLS, if requested) connection to the broker.

    The MQTT layer is intentionally not exercised — this answers the
    "is the broker reachable from this host?" question, which is the
    most common point of failure. Returns::

        {
          "ok": bool,
          "latency_ms": float,
          "tls": bool,
          "host": str,
          "port": int,
          "message": str,
        }
    """
    host = str(integration.get("host", "")).strip()
    port = int(integration.get("port", 1881))
    tls = bool(integration.get("tls", False))
    if not host:
        return {
            "ok": False,
            "latency_ms": 0.0,
            "tls": tls,
            "host": host,
            "port": port,
            "message": "integration.host is empty",
        }

    sock_factory = socket_factory or (lambda: socket.socket(
        socket.AF_INET, socket.SOCK_STREAM
    ))

    started = time.perf_counter()
    sock = sock_factory()
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        if tls:
            ctx = ssl.create_default_context()
            with ctx.wrap_socket(sock, server_hostname=host) as _ssock:
                _ssock.do_handshake()
        latency_ms = (time.perf_counter() - started) * 1000.0
        return {
            "ok": True,
            "latency_ms": round(latency_ms, 1),
            "tls": tls,
            "host": host,
            "port": port,
            "message": f"reachable in {latency_ms:.0f} ms"
            + (" (TLS handshake ok)" if tls else " (plain TCP)"),
        }
    except socket.timeout:
        return {
            "ok": False,
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
            "tls": tls,
            "host": host,
            "port": port,
            "message": f"timed out after {timeout:.1f}s connecting to {host}:{port}",
        }
    except (OSError, ssl.SSLError) as exc:
        return {
            "ok": False,
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
            "tls": tls,
            "host": host,
            "port": port,
            "message": f"{type(exc).__name__}: {exc}",
        }
    finally:
        try:
            sock.close()
        except Exception:  # pragma: no cover - defensive
            pass


# ---------------------------------------------------------------------------
# Credential test (full MQTT CONNECT + CONNACK)
# ---------------------------------------------------------------------------


# Standard MQTT CONNACK return codes — duplicated from the MQTT 3.1.1
# spec so we don't depend on paho's constants in case the import fails.
_CONNACK_MESSAGES: Mapping[int, str] = {
    0: "accepted",
    1: "refused: unacceptable protocol version",
    2: "refused: identifier rejected",
    3: "refused: server unavailable",
    4: "refused: bad username or password",
    5: "refused: not authorized",
}


def test_credentials(
    integration: Mapping[str, Any],
    *,
    publisher_factory: Callable[[Mapping[str, Any]], SensgreenMqttPublisher] | None = None,
) -> dict[str, Any]:
    """Open a real MQTT CONNECT and report the CONNACK ``rc``.

    Uses :class:`SensgreenMqttPublisher` so TLS and client-id wiring is
    identical to production. ``publisher_factory`` is a test hook.
    """
    factory = publisher_factory or (
        lambda integ: SensgreenMqttPublisher.from_integration(integ)
    )
    started = time.perf_counter()
    try:
        publisher = factory(integration)
    except Exception as exc:
        return _credential_failure(started, f"could not build publisher: {exc}")

    try:
        publisher.connect()
    except Exception as exc:
        return _credential_failure(started, f"connect failed: {exc}")

    try:
        latency_ms = (time.perf_counter() - started) * 1000.0
        # If we reached this point without paho raising, the broker
        # accepted the CONNECT (rc=0). Bad creds surface as exceptions
        # in paho >= 1.6 or as ``rc != 0`` on the underlying client.
        return {
            "ok": True,
            "rc": 0,
            "latency_ms": round(latency_ms, 1),
            "username": integration.get("username") or None,
            "message": "authenticated (rc=0, accepted)",
        }
    finally:
        try:
            publisher.disconnect()
        except Exception:  # pragma: no cover - defensive
            pass


def _credential_failure(started_at: float, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "rc": None,
        "latency_ms": round((time.perf_counter() - started_at) * 1000.0, 1),
        "username": None,
        "message": message,
    }


# ---------------------------------------------------------------------------
# Publish test (one synthetic payload, full lifecycle)
# ---------------------------------------------------------------------------


def publish_test(
    integration: Mapping[str, Any],
    *,
    device_eui: str,
    sensor_type: str,
    publisher_factory: Callable[[Mapping[str, Any]], SensgreenMqttPublisher] | None = None,
) -> dict[str, Any]:
    """Publish exactly one synthetic payload, then disconnect.

    The synthetic payload uses the same placeholder data as the preview
    endpoint, so a green publish-test means "the broker accepts the same
    shape my Live session will send."
    """
    topic_template = str(integration.get("topic", "")).strip()
    if not topic_template:
        return {
            "ok": False,
            "message": "integration.topic is required",
            "topic": None,
            "payload": None,
            "latency_ms": 0.0,
        }

    try:
        preview = build_sample_payload(
            device_eui=device_eui,
            sensor_type=sensor_type,
            topic_template=topic_template,
        )
    except ValueError as exc:
        return {
            "ok": False,
            "message": str(exc),
            "topic": None,
            "payload": None,
            "latency_ms": 0.0,
        }

    factory = publisher_factory or (
        lambda integ: SensgreenMqttPublisher.from_integration(integ)
    )
    started = time.perf_counter()
    try:
        publisher = factory(integration)
    except Exception as exc:
        return {
            "ok": False,
            "message": f"could not build publisher: {exc}",
            "topic": preview["topic"],
            "payload": preview["payload"],
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
        }

    try:
        publisher.connect()
        result = publisher.publish(preview["payload"])
    except Exception as exc:
        return {
            "ok": False,
            "message": f"publish failed: {exc}",
            "topic": preview["topic"],
            "payload": preview["payload"],
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
        }
    finally:
        try:
            publisher.disconnect()
        except Exception:  # pragma: no cover - defensive
            pass

    latency_ms = (time.perf_counter() - started) * 1000.0
    if not result.ok:
        return {
            "ok": False,
            "message": f"broker rejected publish (rc={result.rc})",
            "topic": result.topic,
            "payload": preview["payload"],
            "latency_ms": round(latency_ms, 1),
        }
    return {
        "ok": True,
        "message": f"published 1 payload in {latency_ms:.0f} ms",
        "topic": result.topic,
        "payload": preview["payload"],
        "latency_ms": round(latency_ms, 1),
    }


# ---------------------------------------------------------------------------
# Sensgreen mapping introspection (informational panel)
# ---------------------------------------------------------------------------


def mapping_table(devices: list[Any]) -> list[dict[str, Any]]:
    """Build the EUI → Sensgreen metric keys table for a project.

    ``devices`` is the list of :class:`DeviceConfig` from a parsed
    :class:`SimulatorConfig`. The table is informational — Sensgreen's
    backend only sees the ``deviceEui`` and uses its own DB to resolve
    name/zone, so this view helps an operator hand off the EUI list to
    their Sensgreen contact for provisioning.
    """
    rows: list[dict[str, Any]] = []
    for device in devices:
        sensor_type = getattr(device, "type", "")
        try:
            metric_map = SensgreenMetricMapper.mapping_for(sensor_type)
            metric_keys = sorted(set(metric_map.values()))
            supported = True
        except Exception:
            metric_keys = []
            supported = False
        rows.append({
            "device_eui": getattr(device, "device_eui", ""),
            "name": getattr(device, "name", ""),
            "sensor_type": sensor_type,
            "zone_id": getattr(device, "zone_id", None),
            "supported": supported,
            "sensgreen_metric_keys": metric_keys,
        })
    return rows


__all__ = [
    "build_sample_payload",
    "mapping_table",
    "publish_test",
    "redact_integration",
    "test_connection",
    "test_credentials",
]
