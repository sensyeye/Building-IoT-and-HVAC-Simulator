"""Sensor implementations.

Each sensor type generates canonical internal readings:
  - IAQ sensors
  - Energy meters
  - Occupancy sensors
  - Entry/exit people counters
  - HVAC virtual points
  - Device health metrics
"""

from .energy_meter_simulator import EnergyContext, EnergyMeterSimulator
from .iaq_sensor_simulator import IaqSensorSimulator, ZoneState
from .device_personality import DevicePersonality, KNOWN_PROFILES, normalize_profile
from .door_contact_simulator import DoorContactSimulator
from .hvac_simulator import HvacVirtualSimulator
from .occupancy_sensor_simulator import OccupancySensorSimulator
from .people_counter_simulator import (
    EntryExitCounterSimulator,
    PeopleCounterSimulator,
)

__all__ = [
    "DevicePersonality",
    "DoorContactSimulator",
    "EnergyContext",
    "EnergyMeterSimulator",
    "EntryExitCounterSimulator",
    "HvacVirtualSimulator",
    "IaqSensorSimulator",
    "KNOWN_PROFILES",
    "OccupancySensorSimulator",
    "PeopleCounterSimulator",
    "ZoneState",
    "normalize_profile",
]
