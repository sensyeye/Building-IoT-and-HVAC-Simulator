"""JSON API routes for the MQTT publisher (skeleton)."""
from __future__ import annotations

from fastapi import APIRouter

from api.services.mqtt_service import mqtt_service

router = APIRouter()


@router.get("/status")
def status() -> dict:
    return mqtt_service.status()
