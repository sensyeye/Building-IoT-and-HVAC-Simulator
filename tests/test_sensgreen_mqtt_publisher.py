"""Tests for :class:`SensgreenMqttPublisher`.

We avoid hitting a real broker by injecting a fake client through the
``client_factory`` test hook.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import pytest

from simulator.integrations.sensgreen_mqtt_publisher import (
    ENV_PASSWORD,
    ENV_USERNAME,
    PublishResult,
    SensgreenMqttPublisher,
)
from simulator.models.config import MQTTOutputConfig


# ---------------------------------------------------------------------------
# Fake paho client
# ---------------------------------------------------------------------------

@dataclass
class _PublishCall:
    topic: str
    payload: str
    qos: int
    retain: bool


@dataclass
class _PublishInfo:
    rc: int = 0


@dataclass
class FakeMqttClient:
    client_id: str
    username: str | None = None
    password: str | None = None
    tls_called: bool = False
    reconnect_min: int | None = None
    reconnect_max: int | None = None
    connect_args: tuple | None = None
    loop_started: bool = False
    loop_stopped: bool = False
    disconnected: bool = False
    publishes: list[_PublishCall] = field(default_factory=list)
    subscriptions: list[tuple[str, int]] = field(default_factory=list)
    rc: int = 0  # rc to return from publish

    # paho-like attributes
    on_connect: Any = None
    on_disconnect: Any = None
    on_message: Any = None

    def username_pw_set(self, username, password):
        self.username = username
        self.password = password

    def tls_set(self, *args, **kwargs):
        self.tls_called = True

    def reconnect_delay_set(self, *, min_delay, max_delay):
        self.reconnect_min = min_delay
        self.reconnect_max = max_delay

    def connect(self, host, port, keepalive):
        self.connect_args = (host, port, keepalive)
        return 0

    def loop_start(self):
        self.loop_started = True
        return 0

    def loop_stop(self):
        self.loop_stopped = True
        return 0

    def disconnect(self):
        self.disconnected = True
        return 0

    def publish(self, topic, payload, qos, retain):
        self.publishes.append(_PublishCall(topic, payload, qos, retain))
        return _PublishInfo(rc=self.rc)

    def subscribe(self, topic, qos=0):
        self.subscriptions.append((topic, qos))
        return (0, 1)


@pytest.fixture
def fake_factory():
    created: list[FakeMqttClient] = []

    def factory(client_id: str) -> FakeMqttClient:
        c = FakeMqttClient(client_id=client_id)
        created.append(c)
        return c

    factory.created = created  # type: ignore[attr-defined]
    return factory


def _payload(eui: str = "001122", t: int = 1_772_445_600_000) -> dict:
    return {
        "deviceEui": eui,
        "timestamp": t,
        "data": {"temperature": 23.4, "humidity": 55.2},
    }


# ---------------------------------------------------------------------------
# Construction / config
# ---------------------------------------------------------------------------

def test_requires_host_when_not_dry_run():
    with pytest.raises(ValueError):
        SensgreenMqttPublisher(host="")


def test_requires_topic_template_non_empty():
    with pytest.raises(ValueError):
        SensgreenMqttPublisher(host="b", topic_template="   ", dry_run=True)


def test_static_topic_is_allowed():
    """Sensgreen-native topics are building-scoped strings without placeholders."""
    pub = SensgreenMqttPublisher(
        host="b", topic_template="sensor/data/925255", dry_run=True
    )
    # Same topic for every device — the EUI lives in the payload.
    assert pub.topic_for(_payload("AA")) == "sensor/data/925255"
    assert pub.topic_for(_payload("BB")) == "sensor/data/925255"


def test_tls_on_plain_port_warns(caplog):
    """Heuristic: tls=True with Sensgreen's plain-TCP port emits a warning."""
    with caplog.at_level("WARNING", logger="sensgreen.mqtt"):
        SensgreenMqttPublisher(
            host="ankara.sensgreen.com", port=1881, tls=True, dry_run=True
        )
    assert any("plain TCP" in rec.message for rec in caplog.records)


def test_credentials_from_env_when_not_provided():
    pub = SensgreenMqttPublisher(
        host="broker", dry_run=True,
        env={ENV_USERNAME: "u-env", ENV_PASSWORD: "p-env"},
    )
    assert pub.username == "u-env"
    assert pub.password == "p-env"


def test_explicit_credentials_win_over_env():
    pub = SensgreenMqttPublisher(
        host="broker", username="u-arg", password="p-arg", dry_run=True,
        env={ENV_USERNAME: "u-env", ENV_PASSWORD: "p-env"},
    )
    assert pub.username == "u-arg"
    assert pub.password == "p-arg"


def test_from_config_roundtrip(fake_factory):
    cfg = MQTTOutputConfig(
        enabled=True,
        host="b",
        port=8883,
        username="u",
        password="p",
        client_id="cid",
        tls=True,
        topic_template="sensgreen/{device_eui}",
    )
    pub = SensgreenMqttPublisher.from_config(cfg, client_factory=fake_factory)
    pub.connect()
    fake = fake_factory.created[0]
    assert fake.client_id == "cid"
    assert fake.username == "u"
    assert fake.password == "p"
    assert fake.tls_called is True
    assert fake.connect_args == ("b", 8883, 60)
    assert fake.loop_started is True
    pub.disconnect()
    assert fake.loop_stopped and fake.disconnected


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def test_serialize_is_compact_and_roundtrips():
    body = SensgreenMqttPublisher.serialize(_payload())
    # No spaces between separators (compact form).
    assert " " not in body.replace('"', "")
    # Round-trips via json.
    parsed = json.loads(body)
    assert parsed == _payload()


def test_serialize_preserves_key_order():
    p = _payload()
    body = SensgreenMqttPublisher.serialize(p)
    # deviceEui must precede timestamp must precede data.
    assert body.index("deviceEui") < body.index("timestamp") < body.index("data")


def test_serialize_unicode_metric_name():
    p = {"deviceEui": "x", "timestamp": 1_772_445_600_000, "data": {"sıcaklık": 22}}
    body = SensgreenMqttPublisher.serialize(p)
    assert "sıcaklık" in body  # ensure_ascii=False
    assert json.loads(body)["data"]["sıcaklık"] == 22


def test_serialize_rejects_non_mapping():
    with pytest.raises(TypeError):
        SensgreenMqttPublisher.serialize([1, 2, 3])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Topic templating
# ---------------------------------------------------------------------------

def test_topic_for_uses_device_eui():
    pub = SensgreenMqttPublisher(host="b", dry_run=True,
                                 topic_template="sensgreen/{device_eui}/up")
    assert pub.topic_for(_payload("ABC123")) == "sensgreen/ABC123/up"


def test_topic_for_rejects_missing_device_eui_when_template_uses_placeholder():
    pub = SensgreenMqttPublisher(
        host="b", topic_template="sensgreen/{device_eui}", dry_run=True
    )
    with pytest.raises(ValueError):
        pub.topic_for({"deviceEui": "", "timestamp": 1, "data": {"x": 1}})


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------

def test_dry_run_does_not_create_client(fake_factory, capsys):
    pub = SensgreenMqttPublisher(
        host="b", dry_run=True, client_factory=fake_factory,
    )
    pub.connect()
    res = pub.publish(_payload("ABC"))
    pub.disconnect()

    assert fake_factory.created == []  # never instantiated
    assert isinstance(res, PublishResult)
    assert res.ok is True
    assert res.dry_run is True
    assert res.topic == "sensgreen/ABC"
    out = capsys.readouterr().out
    assert "[dry-run]" in out and "ABC" in out


# ---------------------------------------------------------------------------
# Live-mode publish (against fake client)
# ---------------------------------------------------------------------------

def test_publish_calls_client_with_serialized_payload(fake_factory):
    pub = SensgreenMqttPublisher(
        host="b", port=1883, tls=False, client_factory=fake_factory,
        topic_template="sensgreen/{device_eui}",
    )
    pub.connect()
    p = _payload("AABB")
    res = pub.publish(p, qos=2, retain=True)
    pub.disconnect()

    fake = fake_factory.created[0]
    assert len(fake.publishes) == 1
    call = fake.publishes[0]
    assert call.topic == "sensgreen/AABB"
    assert json.loads(call.payload) == p
    assert call.qos == 2
    assert call.retain is True
    assert res.ok is True


def test_publish_failure_returns_not_ok(fake_factory, caplog):
    pub = SensgreenMqttPublisher(
        host="b", tls=False, client_factory=fake_factory,
    )
    pub.connect()
    fake = fake_factory.created[0]
    fake.rc = 4  # MQTT_ERR_NO_CONN
    with caplog.at_level(logging.ERROR, logger="sensgreen.mqtt"):
        res = pub.publish(_payload())
    pub.disconnect()
    assert res.ok is False
    assert res.rc == 4
    assert any("publish failed" in r.message for r in caplog.records)


def test_publish_before_connect_logs_error(fake_factory, caplog):
    pub = SensgreenMqttPublisher(host="b", tls=False, client_factory=fake_factory)
    with caplog.at_level(logging.ERROR, logger="sensgreen.mqtt"):
        res = pub.publish(_payload())
    assert res.ok is False
    assert any("before connect" in r.message for r in caplog.records)


def test_reconnect_delay_configured(fake_factory):
    pub = SensgreenMqttPublisher(
        host="b", tls=False, client_factory=fake_factory,
        reconnect_min_delay=2, reconnect_max_delay=120,
    )
    pub.connect()
    fake = fake_factory.created[0]
    assert fake.reconnect_min == 2
    assert fake.reconnect_max == 120


def test_subscribe_errors_attached_after_connect(fake_factory):
    pub = SensgreenMqttPublisher(
        host="b", tls=False, client_factory=fake_factory,
        error_topic="sensgreen/errors",
    )
    pub.connect()
    fake = fake_factory.created[0]
    assert ("sensgreen/errors", 1) in fake.subscriptions


def test_subscribe_errors_dry_run_is_noop(fake_factory):
    pub = SensgreenMqttPublisher(
        host="b", dry_run=True, client_factory=fake_factory,
        error_topic="sensgreen/errors",
    )
    pub.connect()
    pub.subscribe_errors("sensgreen/errors")
    assert fake_factory.created == []


def test_context_manager_connects_and_disconnects(fake_factory):
    with SensgreenMqttPublisher(
        host="b", tls=False, client_factory=fake_factory,
    ) as pub:
        pub.publish(_payload())
    fake = fake_factory.created[0]
    assert fake.loop_started and fake.loop_stopped and fake.disconnected


def test_on_message_logs_error_payload(fake_factory, caplog):
    pub = SensgreenMqttPublisher(
        host="b", tls=False, client_factory=fake_factory,
    )

    class _Msg:
        topic = "sensgreen/errors"
        payload = b"device offline"

    with caplog.at_level(logging.ERROR, logger="sensgreen.mqtt"):
        pub._on_message(None, None, _Msg())
    assert any("device offline" in r.message for r in caplog.records)
