"""Service layer for the Sensgreen Sensor Simulator.

Thin orchestration on top of the sensor / output / validator modules.
CLI commands defined in :mod:`simulator.main` delegate here so the
runners stay test-friendly and library-usable.
"""

from .bridge_tester import BridgeTester, BridgeTestResult, DeviceBridgeResult
from .historical_runner import HistoricalRunner, HistoricalRunResult
from .live_runner import LiveRunner, LiveRunResult
from .readings_io import dump_readings_jsonl, load_readings_jsonl
from .scenario_context import OccupancyScheduler, ScenarioContext
from .sensor_factory import build_sensor, supported_device_types
from .simulation_service import SimulationService

__all__ = [
    "BridgeTestResult",
    "BridgeTester",
    "DeviceBridgeResult",
    "HistoricalRunResult",
    "HistoricalRunner",
    "LiveRunResult",
    "LiveRunner",
    "OccupancyScheduler",
    "ScenarioContext",
    "SimulationService",
    "build_sensor",
    "dump_readings_jsonl",
    "load_readings_jsonl",
    "supported_device_types",
]
