"""Sensgreen MQTT publisher.

Thin wrapper around ``paho-mqtt`` that publishes already-built Sensgreen
payloads to the configured topic. It does not generate sensor data, does
not map metrics, and never sees raw :class:`SensorReading` objects.

Highlights
----------
- Credentials may come from the config object or environment variables
  (``SENSGREEN_MQTT_USERNAME`` / ``SENSGREEN_MQTT_PASSWORD``).
- Auto-reconnect with exponential backoff (paho's built-in).
- Topic per device via ``topic_template`` (e.g.
  ``"sensgreen/{device_eui}"``).
- Optional error-topic subscription; received messages are forwarded to
  the configured logger at ``ERROR`` level.
- ``dry_run=True`` prints / logs the would-be publish without opening
  any network connection. Useful for CI and local demos.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from ..models.config import MQTTOutputConfig

# We import paho lazily so unit tests (and dry-run users) do not require
# the package to be installed and can substitute a fake client.
try:  # pragma: no cover - import-time branch
    import paho.mqtt.client as mqtt  # type: ignore
except Exception:  # pragma: no cover
    mqtt = None  # type: ignore[assignment]


# Environment variable names used as fallback for credentials.
ENV_USERNAME = "SENSGREEN_MQTT_USERNAME"
ENV_PASSWORD = "SENSGREEN_MQTT_PASSWORD"


class _ClientLike(Protocol):
    """The slice of the paho-mqtt client API the publisher relies on.

    Defined as a Protocol so tests can pass a tiny fake client.
    """

    def username_pw_set(self, username: str | None, password: str | None) -> None: ...
    def tls_set(self, *args: Any, **kwargs: Any) -> None: ...
    def reconnect_delay_set(self, *, min_delay: int, max_delay: int) -> None: ...
    def connect(self, host: str, port: int, keepalive: int) -> int: ...
    def loop_start(self) -> int: ...
    def loop_stop(self) -> int: ...
    def disconnect(self) -> int: ...
    def publish(self, topic: str, payload: str, qos: int, retain: bool) -> Any: ...
    def subscribe(self, topic: str, qos: int) -> Any: ...


ClientFactory = Callable[[str], _ClientLike]


@dataclass
class PublishResult:
    """Outcome of a single :meth:`SensgreenMqttPublisher.publish` call."""

    topic: str
    payload: str
    ok: bool
    rc: int | None = None
    dry_run: bool = False


class SensgreenMqttPublisher:
    """Publishes Sensgreen-formatted payloads to an MQTT broker.

    Parameters
    ----------
    host, port:
        Broker address.
    username, password:
        Credentials. If both are ``None`` and the environment variables
        ``SENSGREEN_MQTT_USERNAME`` / ``SENSGREEN_MQTT_PASSWORD`` are
        set, those are used.
    client_id:
        MQTT client id (default ``"sensgreen-simulator"``).
    tls:
        Whether to wrap the connection in TLS.
    topic_template:
        Used by :meth:`topic_for` when the caller does not pass an
        explicit topic. Must contain ``"{device_eui}"``.
    error_topic:
        Optional subscription for broker/server-side error messages.
    keepalive:
        MQTT keepalive seconds.
    qos, retain:
        Default publish QoS / retain flag.
    reconnect_min_delay, reconnect_max_delay:
        Exponential backoff bounds for paho's auto-reconnect.
    dry_run:
        If ``True``, never opens a network connection. ``connect()`` is
        a no-op and ``publish()`` logs/prints the payload instead.
    logger:
        Logger to use; defaults to ``logging.getLogger("sensgreen.mqtt")``.
    client_factory:
        Test hook. Called as ``client_factory(client_id)`` to construct
        the underlying MQTT client. Defaults to creating a paho client.
    """

    def __init__(
        self,
        *,
        host: str = "",
        port: int = 8883,
        username: str | None = None,
        password: str | None = None,
        client_id: str = "sensgreen-simulator",
        tls: bool = True,
        topic_template: str = "sensgreen/{device_eui}",
        error_topic: str | None = None,
        keepalive: int = 60,
        qos: int = 1,
        retain: bool = False,
        reconnect_min_delay: int = 1,
        reconnect_max_delay: int = 60,
        dry_run: bool = False,
        logger: logging.Logger | None = None,
        env: Mapping[str, str] | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._log = logger or logging.getLogger("sensgreen.mqtt")
        self.host = host
        self.port = int(port)
        self.client_id = client_id
        self.tls = bool(tls)
        self.topic_template = topic_template
        # Two supported topic styles:
        #   * Building-scoped (Sensgreen native): a literal string such as
        #     ``sensor/data/925255`` shared by every device of a project.
        #     The deviceEui lives inside the JSON payload.
        #   * Per-device: a template containing ``{device_eui}``, used by
        #     other brokers / test setups.
        # No placeholder is required — both styles are valid.
        if not isinstance(topic_template, str) or not topic_template.strip():
            raise ValueError("topic_template must be a non-empty string")
        self._topic_has_placeholder = "{device_eui}" in topic_template
        self.error_topic = error_topic
        self.keepalive = int(keepalive)
        self.qos = int(qos)
        self.retain = bool(retain)
        self.reconnect_min_delay = int(reconnect_min_delay)
        self.reconnect_max_delay = int(reconnect_max_delay)
        self.dry_run = bool(dry_run)

        # Heuristic: Sensgreen's native broker exposes a plain-TCP port
        # (e.g. 1881). If a caller enables TLS on such a port we almost
        # certainly have a misconfigured project — warn loudly but don't
        # refuse: there are legitimate setups with non-default TLS ports.
        if self.tls and self.port in (1881, 1883):
            self._log.warning(
                "tls=True with port=%s looks unusual — Sensgreen's native "
                "broker uses plain TCP on 1881. Set tls=false if you are "
                "pointing at ankara.sensgreen.com or similar.",
                self.port,
            )

        env = env or os.environ
        self.username = username if username is not None else env.get(ENV_USERNAME)
        self.password = password if password is not None else env.get(ENV_PASSWORD)

        # Real connections require a host. Dry-run does not.
        if not self.dry_run and not self.host:
            raise ValueError("host is required when dry_run is False")

        self._client_factory = client_factory or _default_client_factory
        self._client: _ClientLike | None = None
        self._connected: bool = False
        self._error_callback: Callable[[str, bytes], None] | None = None

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        cfg: MQTTOutputConfig,
        *,
        dry_run: bool = False,
        logger: logging.Logger | None = None,
        env: Mapping[str, str] | None = None,
        client_factory: ClientFactory | None = None,
        error_topic: str | None = None,
    ) -> "SensgreenMqttPublisher":
        """Build a publisher from the typed :class:`MQTTOutputConfig`."""
        return cls(
            host=cfg.host,
            port=cfg.port,
            username=cfg.username,
            password=cfg.password,
            client_id=cfg.client_id,
            tls=cfg.tls,
            topic_template=cfg.topic_template,
            error_topic=error_topic if error_topic is not None else cfg.error_topic,
            dry_run=dry_run,
            logger=logger,
            env=env,
            client_factory=client_factory,
        )

    @classmethod
    def from_integration(
        cls,
        integration: Mapping[str, Any],
        *,
        dry_run: bool = False,
        logger: logging.Logger | None = None,
        env: Mapping[str, str] | None = None,
        client_factory: ClientFactory | None = None,
    ) -> "SensgreenMqttPublisher":
        """Build a publisher from a per-project integration dict.

        Shape matches what :meth:`ProjectService.get_integration` returns::

            {
              "host": "ankara.sensgreen.com",
              "port": 1881,
              "username": "sensoffice-970440",
              "password": "...",
              "topic": "sensor/data/925255",
              "error_topic": "sensor/error/925255",   # optional
              "tls": false,
              "client_id": "..."
            }

        ``topic`` is treated as a literal (Sensgreen building-scoped) topic
        unless it contains ``{device_eui}``.
        """
        host = str(integration.get("host", "")).strip()
        topic = str(integration.get("topic", "")).strip()
        if not host:
            raise ValueError("integration.host is required")
        if not topic:
            raise ValueError("integration.topic is required")

        return cls(
            host=host,
            port=int(integration.get("port", 1881)),
            username=integration.get("username"),
            password=integration.get("password"),
            client_id=str(integration.get("client_id", "sensgreen-simulator")),
            tls=bool(integration.get("tls", False)),
            topic_template=topic,
            error_topic=(
                str(integration["error_topic"])
                if integration.get("error_topic")
                else None
            ),
            dry_run=dry_run,
            logger=logger,
            env=env,
            client_factory=client_factory,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Connect to the broker and start the network loop.

        No-op when ``dry_run`` is True.
        """
        if self.dry_run:
            self._log.info("dry-run: not connecting to %s:%s", self.host, self.port)
            return
        if self._connected:
            return

        client = self._client_factory(self.client_id)
        if self.username or self.password:
            client.username_pw_set(self.username, self.password)
        if self.tls:
            try:
                client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
            except Exception as e:  # pragma: no cover - defensive
                self._log.warning("tls_set failed: %s", e)
        client.reconnect_delay_set(
            min_delay=self.reconnect_min_delay,
            max_delay=self.reconnect_max_delay,
        )

        # Wire callbacks if the client supports attribute assignment.
        try:
            client.on_connect = self._on_connect      # type: ignore[attr-defined]
            client.on_disconnect = self._on_disconnect  # type: ignore[attr-defined]
            client.on_message = self._on_message      # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - paho always allows this
            pass

        self._log.info("connecting to %s:%s (tls=%s)", self.host, self.port, self.tls)
        client.connect(self.host, self.port, self.keepalive)
        client.loop_start()
        self._client = client
        self._connected = True

        if self.error_topic:
            self.subscribe_errors(self.error_topic)

    def disconnect(self) -> None:
        """Stop the network loop and disconnect."""
        if self.dry_run or not self._connected:
            return
        assert self._client is not None
        try:
            self._client.loop_stop()
        finally:
            self._client.disconnect()
        self._connected = False
        self._client = None
        self._log.info("disconnected")

    def __enter__(self) -> "SensgreenMqttPublisher":
        self.connect()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.disconnect()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def topic_for(self, payload: Mapping[str, Any]) -> str:
        """Resolve the MQTT topic to use for ``payload``.

        - If ``topic_template`` contains ``{device_eui}``, substitute it
          from the payload (raising if absent).
        - Otherwise, return ``topic_template`` as a literal string. This
          is the Sensgreen-native model where every device of a building
          publishes to one shared topic and the EUI lives inside the
          JSON body.
        """
        if not self._topic_has_placeholder:
            return self.topic_template
        device_eui = str(payload.get("deviceEui", "")).strip()
        if not device_eui:
            raise ValueError("payload.deviceEui is required to build topic")
        return self.topic_template.format(device_eui=device_eui)

    @staticmethod
    def serialize(payload: Mapping[str, Any]) -> str:
        """Serialize a payload to the canonical Sensgreen JSON form.

        - Keys are not reordered, matching insertion order from the builder.
        - No whitespace between separators (compact form).
        - ``ensure_ascii=False`` so unicode metric names round-trip.
        """
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def publish(
        self,
        payload: Mapping[str, Any],
        *,
        topic: str | None = None,
        qos: int | None = None,
        retain: bool | None = None,
    ) -> PublishResult:
        """Publish one already-built Sensgreen payload.

        In ``dry_run`` mode this just logs / prints and returns
        ``PublishResult(ok=True, dry_run=True)``.
        """
        topic = topic or self.topic_for(payload)
        body = self.serialize(payload)
        eff_qos = self.qos if qos is None else int(qos)
        eff_retain = self.retain if retain is None else bool(retain)

        if self.dry_run:
            self._log.info("[dry-run] %s %s", topic, body)
            print(f"[dry-run] {topic} {body}")
            return PublishResult(topic=topic, payload=body, ok=True, dry_run=True)

        if not self._connected or self._client is None:
            self._log.error("publish called before connect(): topic=%s", topic)
            return PublishResult(topic=topic, payload=body, ok=False, rc=None)

        try:
            info = self._client.publish(topic, body, eff_qos, eff_retain)
        except Exception as e:
            self._log.exception("publish raised: topic=%s err=%s", topic, e)
            return PublishResult(topic=topic, payload=body, ok=False, rc=None)

        rc = getattr(info, "rc", 0)
        ok = rc == 0
        if ok:
            self._log.debug("published topic=%s bytes=%d", topic, len(body))
        else:
            self._log.error("publish failed: topic=%s rc=%s", topic, rc)
        return PublishResult(topic=topic, payload=body, ok=ok, rc=rc)

    def subscribe_errors(self, topic: str) -> None:
        """Subscribe to ``topic``; received messages are logged at ERROR."""
        if self.dry_run:
            self._log.info("[dry-run] would subscribe to error topic %s", topic)
            return
        if not self._connected or self._client is None:
            raise RuntimeError("subscribe_errors called before connect()")
        self._client.subscribe(topic, qos=1)
        self._log.info("subscribed to error topic: %s", topic)

    def set_error_callback(
        self, callback: "Callable[[str, bytes], None] | None"
    ) -> None:
        """Register a callback invoked for every error-topic message.

        The default behaviour is to log error-topic messages at ERROR.
        Callers that want to *collect* errors (e.g. the bridge tester)
        register a callback here; it receives ``(topic, payload_bytes)``
        for each received message. Pass ``None`` to clear it.
        """
        self._error_callback = callback

    # ------------------------------------------------------------------
    # paho callbacks
    # ------------------------------------------------------------------

    # Signatures match paho's CallbackAPIVersion.VERSION2.
    def _on_connect(self, _client, _userdata, _flags, reason_code, _properties=None) -> None:  # noqa: D401, ANN001
        if int(getattr(reason_code, "value", reason_code)) == 0:
            self._log.info("connected to %s:%s", self.host, self.port)
        else:
            self._log.error("connect failed: %s", reason_code)

    def _on_disconnect(self, _client, _userdata, _flags, reason_code, _properties=None) -> None:  # noqa: ANN001
        self._log.warning("disconnected: %s (auto-reconnect enabled)", reason_code)

    def _on_message(self, _client, _userdata, message) -> None:  # noqa: ANN001
        try:
            body = message.payload.decode("utf-8", errors="replace")
        except Exception:
            body = repr(message.payload)
        self._log.error("error topic %s: %s", message.topic, body)
        cb = getattr(self, "_error_callback", None)
        if cb is not None:
            try:
                cb(message.topic, message.payload)
            except Exception as exc:  # pragma: no cover - defensive
                self._log.warning("error callback raised: %s", exc)


# ---------------------------------------------------------------------------
# Default client factory
# ---------------------------------------------------------------------------

def _default_client_factory(client_id: str) -> _ClientLike:  # pragma: no cover
    if mqtt is None:
        raise RuntimeError(
            "paho-mqtt is not installed; install requirements.txt or use dry_run=True"
        )
    api = getattr(mqtt, "CallbackAPIVersion", None)
    if api is not None:
        return mqtt.Client(callback_api_version=api.VERSION2, client_id=client_id)  # type: ignore[arg-type]
    return mqtt.Client(client_id=client_id)


__all__ = [
    "ENV_PASSWORD",
    "ENV_USERNAME",
    "PublishResult",
    "SensgreenMqttPublisher",
]
