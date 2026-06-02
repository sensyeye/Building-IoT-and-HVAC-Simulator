"""Build Sensgreen MQTT payloads from canonical SensorReadings.

The builder runs the reading's data through :class:`SensgreenMetricMapper`,
producing the standard Sensgreen MQTT envelope::

    {
      "deviceEui": "...",
      "timestamp": 1772445600000,   # ms since epoch, UTC
      "data": { "<sensgreen_metric_id>": <value>, ... }
    }

This module contains no networking. Connecting and publishing happens
elsewhere.
"""

from __future__ import annotations

from typing import Any

from ..models.reading import SensorReading
from .sensgreen_metric_mapper import SensgreenMetricMapper


class PayloadValidationError(ValueError):
    """Raised when a built payload fails validation."""


class SensgreenMqttPayloadBuilder:
    """Convert :class:`SensorReading` instances to Sensgreen MQTT payloads.

    Parameters
    ----------
    mapper:
        Optional pre-configured :class:`SensgreenMetricMapper`. If not
        provided, a default (non-strict) mapper is used.
    strict_mode:
        Convenience flag — used only when ``mapper`` is ``None`` to
        construct a strict mapper.
    """

    def __init__(
        self,
        mapper: SensgreenMetricMapper | None = None,
        *,
        strict_mode: bool = False,
    ) -> None:
        self.mapper = mapper or SensgreenMetricMapper(strict_mode=strict_mode)

    # -- public API --------------------------------------------------------

    def build(self, reading: SensorReading) -> dict[str, Any]:
        """Build and validate a Sensgreen MQTT payload from ``reading``."""
        if not isinstance(reading, SensorReading):
            raise PayloadValidationError(
                f"reading must be a SensorReading, got {type(reading).__name__}"
            )

        device_eui = (reading.device_eui or "").strip()
        if not device_eui:
            raise PayloadValidationError("deviceEui must not be empty")

        if not reading.sensor_type:
            raise PayloadValidationError("sensor_type must not be empty")

        if not isinstance(reading.data, dict) or not reading.data:
            raise PayloadValidationError("data must be a non-empty dict")

        try:
            timestamp_ms = reading.timestamp_ms()
        except (AttributeError, TypeError, ValueError) as e:
            raise PayloadValidationError(f"invalid timestamp: {e}") from e

        mapped = self.mapper.map(reading.sensor_type, reading.data)
        if not mapped:
            raise PayloadValidationError(
                "data is empty after mapping — no recognised Sensgreen metrics"
            )

        payload: dict[str, Any] = {
            "deviceEui": device_eui,
            "timestamp": timestamp_ms,
            "data": mapped,
        }

        self._validate(payload)
        return payload

    # -- validation --------------------------------------------------------

    @staticmethod
    def _validate(payload: dict[str, Any]) -> None:
        device_eui = payload.get("deviceEui")
        if not isinstance(device_eui, str) or not device_eui:
            raise PayloadValidationError("payload.deviceEui must be a non-empty string")

        ts = payload.get("timestamp")
        if not isinstance(ts, int) or isinstance(ts, bool):
            raise PayloadValidationError(
                "payload.timestamp must be an int (Unix ms, UTC)"
            )
        # Sanity bound: must be milliseconds (>= ~year 2001) and not absurdly far ahead.
        if ts < 1_000_000_000_000:
            raise PayloadValidationError(
                "payload.timestamp must be Unix milliseconds (looks like seconds)"
            )

        data = payload.get("data")
        if not isinstance(data, dict) or not data:
            raise PayloadValidationError("payload.data must be a non-empty dict")
        for key, value in data.items():
            if not isinstance(key, str) or not key:
                raise PayloadValidationError(
                    "payload.data keys must be non-empty strings"
                )
            if value is None:
                raise PayloadValidationError(
                    f"payload.data['{key}'] must not be None"
                )


__all__ = [
    "SensgreenMqttPayloadBuilder",
    "PayloadValidationError",
]
