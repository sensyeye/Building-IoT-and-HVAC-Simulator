"""Simulation service: drives sensors over a time range and yields readings.

Used by both the historical runner (collects readings into a list and
exports to CSV) and the live runner (publishes to MQTT as it goes).

Per-device frequency
--------------------
Each device may declare its own sample frequency via
``metadata.interval_seconds``. When absent, the global
``simulation.interval_seconds`` is used.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from ..models.config import DeviceConfig, SimulatorConfig
from ..models.reading import SensorReading
from ..sensors import EnergyContext, EnergyMeterSimulator
from ..scenarios.micro_events import MicroEventEngine
from .scenario_context import ScenarioContext
from .sensor_factory import UnsupportedDeviceTypeError, build_sensor


@dataclass
class _DeviceRuntime:
    device: DeviceConfig
    sim: Any
    interval_s: int
    next_due: datetime


class SimulationService:
    """Generate readings for every configured device over a time range.

    Parameters
    ----------
    cfg:
        Parsed :class:`SimulatorConfig`.
    seed:
        Optional global seed. Combined with each device EUI's hash to
        keep streams reproducible *and* decorrelated.
    skip_unsupported:
        When ``True`` (default), unsupported device types are silently
        skipped — useful while not every sensor type is implemented.
    micro_event_engine:
        Optional :class:`MicroEventEngine`. When provided, IAQ readings
        receive additive deltas from stochastic short-window events and
        the events are attached to ``reading.metadata['micro_events']``.
        Engine state is owned by the caller so the live session can
        also peek at started/ended events between ticks.
    """

    def __init__(
        self,
        cfg: SimulatorConfig,
        *,
        seed: int | None = None,
        skip_unsupported: bool = True,
        micro_event_engine: MicroEventEngine | None = None,
    ) -> None:
        self.cfg = cfg
        self.context = ScenarioContext.from_building(cfg.building)
        self.micro_event_engine = micro_event_engine
        # Track the most recent (zone_id, ts) we stepped the event
        # engine for so multiple devices in the same zone share one
        # engine step per tick instead of multiplying the trigger
        # rate. The dict maps zone_id → datetime of last step.
        self._last_engine_step: dict[str, datetime] = {}

        seed_used = cfg.simulation.seed if seed is None else seed
        global_interval = max(int(cfg.simulation.interval_seconds), 1)

        self._runtimes: list[_DeviceRuntime] = []
        for device in cfg.devices:
            try:
                sim = build_sensor(device, seed=seed_used)
            except UnsupportedDeviceTypeError:
                if skip_unsupported:
                    continue
                raise
            interval = int(
                (device.metadata or {}).get("interval_seconds", global_interval)
            )
            interval = max(interval, 1)
            self._runtimes.append(
                _DeviceRuntime(
                    device=device,
                    sim=sim,
                    interval_s=interval,
                    next_due=datetime.min.replace(tzinfo=timezone.utc),
                )
            )

    # -- public API --------------------------------------------------------

    @property
    def device_count(self) -> int:
        return len(self._runtimes)

    def iter_readings(
        self,
        *,
        start: datetime,
        end: datetime,
    ) -> Iterator[SensorReading]:
        """Yield readings between ``start`` (inclusive) and ``end`` (exclusive).

        Each device samples on its own ``interval_seconds`` cadence. The
        overall master tick is ``gcd_or_min`` of those intervals.
        """
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if end <= start:
            return

        # Initialise next_due so the first tick fires at start.
        for rt in self._runtimes:
            rt.next_due = start

        # Master tick = smallest device interval (keeps things simple).
        tick = min((rt.interval_s for rt in self._runtimes), default=60)
        ts = start
        while ts < end:
            self.context.update(ts)
            for rt in self._runtimes:
                if ts >= rt.next_due:
                    yield self._sample_one(rt, ts)
                    rt.next_due = ts + timedelta(seconds=rt.interval_s)
            ts += timedelta(seconds=tick)

    # -- internals ---------------------------------------------------------

    def _sample_one(self, rt: _DeviceRuntime, ts: datetime) -> SensorReading:
        device = rt.device
        ctx = self.context

        if device.type == "iaq":
            zone_state = ctx.zone_state(device.zone_id)
            # P10.4: room owns the physics; IAQ observes through its
            # personality. Falls back gracefully for tests that build
            # SimulationService without a ScenarioContext tick.
            reading = rt.sim.sample(ts, zone_state, room_driven=True)
            if self.micro_event_engine is not None:
                self._apply_micro_events(reading, ts, device, zone_state)
            return reading

        if device.type == "energy_meter":
            zone_id = device.zone_id
            try:
                cap = ctx.capacity(zone_id)
                occ = ctx.occupancy(zone_id)
            except KeyError:
                cap, occ = 10, 0
            energy_ctx = EnergyContext(
                outdoor_temperature_c=ctx.outdoor_temperature(ts),
                occupancy=occ,
                occupancy_capacity=cap,
            )
            return rt.sim.sample(ts, energy_ctx)

        if device.type == "people_counter":
            occ = ctx.occupancy(device.zone_id) if device.zone_id in ctx.zone_states else 0
            return rt.sim.sample(ts, true_occupancy=occ)

        if device.type == "occupancy_sensor":
            occ = ctx.occupancy(device.zone_id) if device.zone_id in ctx.zone_states else 0
            return rt.sim.sample(ts, true_occupancy=occ)

        if device.type == "door_contact":
            occ = ctx.occupancy(device.zone_id) if device.zone_id in ctx.zone_states else 0
            reading = rt.sim.sample(ts, true_occupancy=occ)
            # P10.4: propagate door state into the room so the next
            # physics tick sees boosted air-exchange + outdoor coupling.
            if device.zone_id in ctx.zone_states:
                zone_state = ctx.zone_state(device.zone_id)
                door_state = reading.data.get("door_state")
                if door_state == "open":
                    zone_state.open_door()
                elif door_state == "closed":
                    zone_state.close_door()
            return reading

        if device.type == "entry_exit_counter":
            return rt.sim.sample(ts)

        if device.type == "hvac":
            zone_state = (
                ctx.zone_state(device.zone_id)
                if device.zone_id in ctx.zone_states else None
            )
            return rt.sim.sample(
                ts, zone_state, outdoor_c=ctx.outdoor_temperature(ts)
            )

        # Should never get here because build_sensor would have raised.
        raise UnsupportedDeviceTypeError(  # pragma: no cover
            f"Unhandled device.type='{device.type}' in SimulationService"
        )

    # -- micro-event integration ------------------------------------------

    def _apply_micro_events(self, reading, ts, device, zone_state) -> None:
        """Apply additive micro-event deltas to an IAQ reading.

        The engine is stepped at most once per zone per timestamp so
        multiple devices in the same zone share the same incident
        timeline (and the trigger probability isn't multiplied by the
        device count). Devices that arrive *after* the engine has
        already stepped for this (zone, ts) still see the active
        deltas — we just don't roll new dice.
        """
        engine = self.micro_event_engine
        assert engine is not None  # mypy
        sched = self.context.schedulers.get(device.zone_id)
        schedule_kind = sched.schedule_kind if sched else "open_office"
        last = self._last_engine_step.get(device.zone_id)
        if last == ts:
            # Already stepped this zone for this timestamp — derive
            # active events + summed deltas directly from the engine's
            # active list without rolling new triggers.
            deltas: dict[str, float] = {}
            active = []
            for ev in engine.active_snapshot():
                if ev.zone_id != device.zone_id or ev.is_finished(ts):
                    continue
                active.append(ev)
                for ch, dv in ev.deltas_at(ts).items():
                    deltas[ch] = deltas.get(ch, 0.0) + dv
        else:
            # dt_min: time since this zone's previous engine step. On
            # the first call we fall back to the device's sample
            # interval as a conservative estimate.
            if last is not None:
                dt_min = max((ts - last).total_seconds() / 60.0, 0.0)
            else:
                dt_min = max(
                    (device.metadata or {}).get(
                        "interval_seconds", self.cfg.simulation.interval_seconds
                    ),
                    1,
                ) / 60.0
            deltas, active = engine.step(
                ts,
                device.zone_id,
                dt_min=dt_min,
                occupancy=zone_state.occupancy,
                schedule_kind=schedule_kind,
            )
            self._last_engine_step[device.zone_id] = ts

        if deltas:
            for ch, dv in deltas.items():
                if ch in reading.data:
                    new = float(reading.data[ch]) + float(dv)
                    reading.data[ch] = round(new, 2)
        if active:
            reading.metadata["micro_events"] = [
                {
                    "id": ev.template_id,
                    "started_at": ev.started_at.isoformat(timespec="seconds"),
                }
                for ev in active
            ]


__all__ = ["SimulationService"]
