"""Factory mapping ``DeviceConfig.type`` to a concrete simulator class."""

from __future__ import annotations

from typing import Any

from ..models.config import DeviceConfig
from ..sensors import (
    DoorContactSimulator,
    EnergyMeterSimulator,
    EntryExitCounterSimulator,
    HvacVirtualSimulator,
    IaqSensorSimulator,
    OccupancySensorSimulator,
    PeopleCounterSimulator,
)


_BUILDERS = {
    "iaq": lambda d, seed: IaqSensorSimulator(d, seed=seed),
    "energy_meter": lambda d, seed: EnergyMeterSimulator(d, seed=seed),
    "people_counter": lambda d, seed: PeopleCounterSimulator(d, seed=seed),
    "entry_exit_counter": lambda d, seed: EntryExitCounterSimulator(d, seed=seed),
    "occupancy_sensor": lambda d, seed: OccupancySensorSimulator(d, seed=seed),
    "door_contact": lambda d, seed: DoorContactSimulator(d, seed=seed),
    "hvac": lambda d, seed: HvacVirtualSimulator(d, seed=seed),
}


class UnsupportedDeviceTypeError(ValueError):
    """Raised when ``DeviceConfig.type`` has no implemented simulator."""


def supported_device_types() -> tuple[str, ...]:
    return tuple(sorted(_BUILDERS.keys()))


def build_sensor(device: DeviceConfig, *, seed: int | None = None) -> Any:
    """Instantiate the right simulator for ``device``.

    Parameters
    ----------
    device:
        Device configuration entry from the YAML file.
    seed:
        Optional RNG seed; combined with the device EUI hash so different
        devices produce decorrelated streams.
    """
    builder = _BUILDERS.get(device.type)
    if builder is None:
        raise UnsupportedDeviceTypeError(
            f"No simulator implemented for device.type='{device.type}'. "
            f"Supported: {', '.join(supported_device_types())}"
        )
    derived_seed = None
    if seed is not None:
        derived_seed = (seed + hash(device.device_eui)) & 0x7FFF_FFFF
    return builder(device, derived_seed)


__all__ = ["UnsupportedDeviceTypeError", "build_sensor", "supported_device_types"]
