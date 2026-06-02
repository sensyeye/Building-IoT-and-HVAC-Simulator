"""External integrations.

Hosts clients for:
  - Sensgreen MQTT broker (payload builder + publisher)
  - Sensgreen database / API (where applicable)
"""

from .sensgreen_metric_mapper import (
    SensgreenMetricMapper,
    UnknownMetricError,
    UnsupportedSensorTypeError,
)
from .sensgreen_mqtt_payload_builder import (
    PayloadValidationError,
    SensgreenMqttPayloadBuilder,
)
from .sensgreen_mqtt_publisher import (
    ENV_PASSWORD,
    ENV_USERNAME,
    PublishResult,
    SensgreenMqttPublisher,
)

__all__ = [
    "ENV_PASSWORD",
    "ENV_USERNAME",
    "PayloadValidationError",
    "PublishResult",
    "SensgreenMetricMapper",
    "SensgreenMqttPayloadBuilder",
    "SensgreenMqttPublisher",
    "UnknownMetricError",
    "UnsupportedSensorTypeError",
]
