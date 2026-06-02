"""Adapter to the MQTT publisher for the web UI.

The Live MQTT Monitor page reads from this service. Connection,
authentication, and payload formatting all live in
``simulator.integrations`` / the publisher module — never in routes.
"""
from __future__ import annotations

from typing import Any


class MqttService:
    """Thin facade around the Sensgreen MQTT publisher."""

    def status(self) -> dict[str, Any]:  # pragma: no cover - stub
        return {"state": "disconnected", "broker": None, "last_error": None}

    def recent_payloads(self, limit: int = 50) -> list[dict[str, Any]]:  # pragma: no cover - stub
        return []

    def start(self, project_id: str) -> dict[str, Any]:  # pragma: no cover - stub
        raise NotImplementedError("Live publishing will be wired up in a follow-up task")

    def stop(self) -> dict[str, Any]:  # pragma: no cover - stub
        raise NotImplementedError("Live publishing will be wired up in a follow-up task")


mqtt_service = MqttService()
