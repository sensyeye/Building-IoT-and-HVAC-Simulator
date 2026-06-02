"""Tests for ProjectService.get_integration / set_integration."""

from __future__ import annotations

import pytest

from api.services.project_service import ProjectService


@pytest.fixture()
def svc(tmp_path):
    return ProjectService(data_dir=tmp_path)


@pytest.fixture()
def project(svc):
    return svc.create(
        {
            "name": "Dubai Office",
            "building_type": "office",
            "city": "Dubai",
            "timezone": "Asia/Dubai",
            "area_m2": 1500,
            "floors": 2,
            "demo_depth": "standard",
        }
    )


def test_get_integration_returns_none_when_unset(svc, project):
    assert svc.get_integration(project.id) is None


def test_set_and_get_integration_roundtrip(svc, project):
    saved = svc.set_integration(
        project.id,
        {
            "host": "ankara.sensgreen.com",
            "port": 1881,
            "username": "sensoffice-970440",
            "password": "wNby6QuSQbxp",
            "topic": "sensor/data/925255",
            "error_topic": "sensor/error/925255",
            "tls": False,
        },
    )
    assert saved["host"] == "ankara.sensgreen.com"
    assert saved["port"] == 1881
    assert saved["topic"] == "sensor/data/925255"
    assert saved["tls"] is False
    assert saved["client_id"].endswith(project.id)

    loaded = svc.get_integration(project.id)
    assert loaded == saved


def test_set_integration_drops_unknown_keys(svc, project):
    saved = svc.set_integration(
        project.id,
        {
            "host": "h",
            "topic": "t",
            "rogue_field": "should-be-dropped",
            "__proto__": "x",
        },
    )
    assert "rogue_field" not in saved
    assert "__proto__" not in saved


def test_set_integration_requires_host(svc, project):
    with pytest.raises(ValueError):
        svc.set_integration(project.id, {"topic": "t"})


def test_set_integration_requires_topic(svc, project):
    with pytest.raises(ValueError):
        svc.set_integration(project.id, {"host": "h"})


def test_set_integration_rejects_unknown_project(svc):
    with pytest.raises(ValueError):
        svc.set_integration("does-not-exist", {"host": "h", "topic": "t"})


def test_secrets_file_is_separate_from_project_file(svc, project, tmp_path):
    svc.set_integration(project.id, {"host": "h", "topic": "t"})
    secrets_path = tmp_path / f"{project.id}.secrets.json"
    project_path = tmp_path / f"{project.id}.json"
    assert secrets_path.exists()
    assert project_path.exists()
    # Secrets must not leak into the project record itself.
    import json
    project_data = json.loads(project_path.read_text())
    for key in ("host", "username", "password", "topic"):
        assert key not in project_data


def test_list_ignores_secrets_files(svc, project):
    svc.set_integration(project.id, {"host": "h", "topic": "t"})
    items = svc.list()
    assert len(items) == 1
    assert items[0].id == project.id


# ---------------------------------------------------------------------------
# Publisher.from_integration
# ---------------------------------------------------------------------------


def test_from_integration_builds_publisher_with_static_topic():
    from simulator.integrations.sensgreen_mqtt_publisher import SensgreenMqttPublisher

    pub = SensgreenMqttPublisher.from_integration(
        {
            "host": "ankara.sensgreen.com",
            "port": 1881,
            "username": "u",
            "password": "p",
            "topic": "sensor/data/925255",
            "tls": False,
        },
        dry_run=True,
    )
    assert pub.host == "ankara.sensgreen.com"
    assert pub.port == 1881
    assert pub.tls is False
    assert pub.topic_template == "sensor/data/925255"
    # Static topic: same for every device.
    assert pub.topic_for({"deviceEui": "70b3d57ed005f1a4"}) == "sensor/data/925255"


def test_from_integration_requires_host_and_topic():
    from simulator.integrations.sensgreen_mqtt_publisher import SensgreenMqttPublisher

    with pytest.raises(ValueError):
        SensgreenMqttPublisher.from_integration({"topic": "t"})
    with pytest.raises(ValueError):
        SensgreenMqttPublisher.from_integration({"host": "h"})
