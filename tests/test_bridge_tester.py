"""Tests for :class:`BridgeTester`.

The bridge tester is a one-shot smoke test of the Sensgreen MQTT
integration. These tests use a fake paho client (same fixture pattern
as ``test_sensgreen_mqtt_publisher``) so no network is hit.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

# Reuse the fake paho client from the publisher tests.
sys.path.insert(0, str(Path(__file__).parent))
from test_sensgreen_mqtt_publisher import (  # noqa: E402
    FakeMqttClient,
    _PublishInfo,
    fake_factory,  # noqa: F401  (re-exported as a pytest fixture)
)

from simulator.config_loader import load_config  # noqa: E402
from simulator.integrations.sensgreen_mqtt_publisher import (  # noqa: E402
    SensgreenMqttPublisher,
)
from simulator.services import BridgeTester  # noqa: E402


CONFIG_PATH = "configs/dubai_office.yaml"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_bridge_tester_publishes_one_payload_per_device(fake_factory):
    cfg = load_config(CONFIG_PATH)
    pub = SensgreenMqttPublisher(
        host="b",
        port=1881,
        topic_template="sensor/data/925255",
        client_factory=fake_factory,
    )

    tester = BridgeTester(cfg, pub, error_listen_seconds=0)
    result = tester.run(project_id="dubai_office")

    assert result.published_count == len(cfg.devices)
    assert result.failed_count == 0
    assert result.all_ok is True
    # All publishes used the static building topic.
    fake: FakeMqttClient = fake_factory.created[0]
    assert len(fake.publishes) == len(cfg.devices)
    for call in fake.publishes:
        assert call.topic == "sensor/data/925255"
    # Every device result carries a payload sample shaped like the spec.
    for d in result.devices:
        assert d.published is True
        assert d.payload_sample is not None
        assert set(d.payload_sample.keys()) == {"deviceEui", "timestamp", "data"}
        assert d.payload_sample["deviceEui"] == d.device_eui
        assert isinstance(d.payload_sample["data"], dict)
        assert d.payload_sample["data"]  # non-empty


def test_bridge_tester_subscribes_to_error_topic(fake_factory):
    cfg = load_config(CONFIG_PATH)
    pub = SensgreenMqttPublisher(
        host="b",
        port=1881,
        topic_template="sensor/data/925255",
        error_topic="sensor/error/925255",
        client_factory=fake_factory,
    )

    tester = BridgeTester(cfg, pub, error_listen_seconds=0)
    result = tester.run()

    fake: FakeMqttClient = fake_factory.created[0]
    assert ("sensor/error/925255", 1) in fake.subscriptions
    assert result.error_topic == "sensor/error/925255"


def test_bridge_tester_collects_error_messages(fake_factory):
    cfg = load_config(CONFIG_PATH)
    pub = SensgreenMqttPublisher(
        host="b",
        port=1881,
        topic_template="sensor/data/925255",
        error_topic="sensor/error/925255",
        client_factory=fake_factory,
    )

    tester = BridgeTester(cfg, pub, error_listen_seconds=0)
    # Start the test, then simulate a broker-side rejection by feeding
    # a message into the publisher's on_message callback.
    result_holder: dict[str, Any] = {}

    # Patch run() flow: simulate error delivery before disconnect.
    original_run = tester.run

    def run_with_error(*args, **kwargs):
        # We can't easily intercept inside run(); instead, invoke run()
        # and feed errors directly via the callback while the publisher
        # is still connected. Run a tiny custom flow:
        pub.set_error_callback(tester._on_error_message)
        pub.connect()
        # Simulate a single rejected message.
        msg = type(
            "M", (), {"topic": "sensor/error/925255", "payload": b'{"err":"bad"}'}
        )()
        pub._on_message(None, None, msg)
        pub.disconnect()
        return tester._error_messages

    errs = run_with_error()
    assert len(errs) == 1
    assert errs[0]["topic"] == "sensor/error/925255"
    assert "bad" in errs[0]["body"]


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------

def test_bridge_tester_records_publish_failure(fake_factory):
    cfg = load_config(CONFIG_PATH)
    pub = SensgreenMqttPublisher(
        host="b",
        port=1881,
        topic_template="sensor/data/925255",
        client_factory=fake_factory,
    )
    tester = BridgeTester(cfg, pub, error_listen_seconds=0)

    # Make the fake client return non-zero rc on every publish.
    pub.connect()
    fake: FakeMqttClient = fake_factory.created[0]
    fake.rc = 1
    pub.disconnect()  # reset; BridgeTester will reconnect

    # Re-run with rc=1 baked in: monkey-patch factory to keep returning
    # the same fake instance.
    def factory(_cid):
        c = FakeMqttClient(client_id=_cid)
        c.rc = 1
        fake_factory.created.append(c)
        return c

    pub2 = SensgreenMqttPublisher(
        host="b",
        port=1881,
        topic_template="sensor/data/925255",
        client_factory=factory,
    )
    tester2 = BridgeTester(cfg, pub2, error_listen_seconds=0)
    result = tester2.run()
    assert result.failed_count == len(cfg.devices)
    assert result.published_count == 0
    assert result.all_ok is False
    for d in result.devices:
        assert d.published is False
        assert d.error is not None
