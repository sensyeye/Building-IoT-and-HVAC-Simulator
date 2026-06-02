"""Canonical internal sensor reading model.

A ``SensorReading`` is the single in-memory representation produced by
sensors. Output adapters (MQTT, CSV) convert from this — they must not
add or rename metrics on the way out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class SensorReading:
    """A single timestamped reading from a virtual device.

    Attributes
    ----------
    device_eui:
        Unique device identifier (Sensgreen ``deviceEui``).
    sensor_type:
        Internal sensor type, e.g. ``"iaq"``, ``"energy_meter"``,
        ``"people_counter"``, ``"entry_exit_counter"``, ``"hvac"``.
    timestamp:
        Timezone-aware UTC datetime when the reading was produced.
    data:
        Mapping of *internal* metric names to values. These will be
        translated to Sensgreen metric ids by output adapters.
    metadata:
        Optional non-telemetry metadata (zone id, scenario tags, etc).
    """

    device_eui: str
    sensor_type: str
    timestamp: datetime
    data: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)

    def timestamp_ms(self) -> int:
        """Return the timestamp as Unix epoch milliseconds in UTC."""
        ts = self.timestamp
        if ts.tzinfo is None:
            # Treat naive timestamps as UTC to keep the contract explicit.
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)
        return int(ts.timestamp() * 1000)
