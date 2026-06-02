"""Micro-event engine: rare, short-window incidents that mutate IAQ readings.

The :class:`MicroEventEngine` runs alongside the per-tick simulation and
randomly fires *micro-events* in each zone. A micro-event is a tiny
state machine with a rise/plateau/decay envelope and a set of channel
deltas (e.g. "+30 µg/m³ PM2.5 for ~6 minutes"). Active events are
applied additively to each ``SensorReading`` after the underlying
sensor model produced its baseline values.

Why this exists
---------------
The scenario catalog in :mod:`simulator.scenarios.catalog` is a *label*
system — it explains why a reading looks the way it does but does not
itself perturb the numbers. To produce more realistic-looking data
without rebuilding the physical models, this engine injects bounded,
plausible deltas at random times. The engine is fully deterministic
when seeded.

Design
------
* :class:`EventTemplate` describes one type of event: which channels it
  affects, the magnitudes, the rise / plateau / decay timings, the
  per-minute trigger probability per zone, and a soft cooldown so the
  same event does not chain.
* :class:`MicroEventInstance` is a live event with a creation timestamp
  and a deterministic peak magnitude per channel.
* :class:`MicroEventEngine` owns the RNG and the list of active events.
  Call :meth:`step` once per tick per zone to get current deltas. The
  engine also exposes :meth:`pop_started_events` and
  :meth:`pop_ended_events` so callers can route events into the
  EventService and the Live tab.

All probabilities are *per minute*. The engine compensates for the
actual tick length so the rate stays the same regardless of
``simulation.interval_seconds``.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EventTemplate:
    """Specification for one kind of micro-event.

    Attributes
    ----------
    id:
        Stable kebab-case id (used in events, logs, UI).
    name:
        Short human-readable label.
    category:
        Grouping tag for the UI — "occupant", "cleaning", "hvac",
        "weather", "fault", "equipment".
    description:
        One-line summary shown in the Events tab and Live drawer.
    channels:
        Map of reading channel name (key in ``reading.data``) → peak
        delta magnitude. The actual peak per instance is uniformly
        sampled from [0.5 * magnitude, 1.0 * magnitude] to add variety.
    rise_min, plateau_min, decay_min:
        Envelope timings in minutes. Total event lifetime is the sum.
        The envelope is piecewise linear: 0 → peak over ``rise_min``,
        peak for ``plateau_min``, peak → 0 over ``decay_min``.
    probability_per_min:
        Per-minute, per-zone probability of triggering this event when
        nothing of the same id is already active.
    zone_kinds:
        Optional whitelist of ``schedule_kind`` values (``"open_office"``,
        ``"meeting_room"``, etc.). ``None`` means any zone.
    occupied_only:
        When True, the event only fires while occupancy > 0.
    cooldown_min:
        Minimum minutes between two events of this kind in the same zone.
    """

    id: str
    name: str
    category: str
    description: str
    channels: dict[str, float]
    rise_min: float = 1.0
    plateau_min: float = 2.0
    decay_min: float = 4.0
    probability_per_min: float = 0.001
    zone_kinds: tuple[str, ...] | None = None
    occupied_only: bool = False
    cooldown_min: float = 30.0

    @property
    def duration_min(self) -> float:
        return self.rise_min + self.plateau_min + self.decay_min


# ---------------------------------------------------------------------------
# Catalog of templates
# ---------------------------------------------------------------------------
#
# Magnitudes are conservative — they nudge readings without smashing
# clamps. Probabilities are tuned so an average office sees a few
# events per day per zone but no single event dominates the timeline.

EVENT_TEMPLATES: tuple[EventTemplate, ...] = (
    EventTemplate(
        id="printer_toner_plume",
        name="Printer toner plume",
        category="equipment",
        description="Laser-printer toner release: brief PM2.5 + PM10 bump.",
        channels={"pm25": 18.0, "pm10": 28.0, "voc": 0.15},
        rise_min=0.5,
        plateau_min=1.5,
        decay_min=4.0,
        probability_per_min=0.004,
        occupied_only=True,
    ),
    EventTemplate(
        id="coffee_brewing",
        name="Coffee brewing",
        category="occupant",
        description="Aromatics + steam release near coffee machine.",
        channels={"voc": 0.35, "humidity": 3.0, "temperature": 0.4},
        rise_min=1.0,
        plateau_min=3.0,
        decay_min=5.0,
        probability_per_min=0.006,
        occupied_only=True,
    ),
    EventTemplate(
        id="cleaning_spray",
        name="Cleaning spray",
        category="cleaning",
        description="Alcohol / ammonia-based cleaner: sharp TVOC spike.",
        channels={"voc": 1.4, "humidity": 1.5},
        rise_min=0.5,
        plateau_min=2.0,
        decay_min=8.0,
        probability_per_min=0.0015,
        cooldown_min=180.0,
    ),
    EventTemplate(
        id="perfume_burst",
        name="Perfume / fragrance burst",
        category="occupant",
        description="Strong fragrance — VOC spike with no PM signature.",
        channels={"voc": 0.8},
        rise_min=0.3,
        plateau_min=1.0,
        decay_min=5.0,
        probability_per_min=0.003,
        occupied_only=True,
    ),
    EventTemplate(
        id="door_open_winter",
        name="Door opened — outdoor air",
        category="hvac",
        description="Door held open: temperature drops, humidity dips.",
        channels={"temperature": -1.8, "humidity": -4.0, "co2": -60.0, "pm25": 4.0},
        rise_min=0.5,
        plateau_min=1.0,
        decay_min=3.0,
        probability_per_min=0.005,
    ),
    EventTemplate(
        id="hvac_short_cycle",
        name="HVAC short-cycle",
        category="hvac",
        description="Cooling stage kicks in briefly — sharp temp drop, pressure blip.",
        channels={"temperature": -0.9, "pressure": 35.0, "humidity": -2.0},
        rise_min=0.3,
        plateau_min=1.5,
        decay_min=2.0,
        probability_per_min=0.012,
    ),
    EventTemplate(
        id="meeting_packed",
        name="Packed meeting",
        category="occupant",
        description="Tight meeting room fills up — CO₂ ramps fast, temp rises.",
        channels={"co2": 380.0, "temperature": 0.9, "humidity": 4.0},
        rise_min=4.0,
        plateau_min=8.0,
        decay_min=12.0,
        probability_per_min=0.0008,
        zone_kinds=("meeting_room",),
        occupied_only=True,
        cooldown_min=90.0,
    ),
    EventTemplate(
        id="sneeze_cluster",
        name="Sneeze / cough cluster",
        category="occupant",
        description="Brief respiratory aerosol burst — micro PM2.5 spike.",
        channels={"pm25": 6.0, "pm10": 9.0},
        rise_min=0.2,
        plateau_min=0.5,
        decay_min=2.0,
        probability_per_min=0.01,
        occupied_only=True,
        cooldown_min=15.0,
    ),
    EventTemplate(
        id="window_opened",
        name="Window opened",
        category="hvac",
        description="Manual vent: fresh-air ingress drops CO₂, mixes PM.",
        channels={"co2": -180.0, "pm25": 5.0, "temperature": -0.7, "humidity": 2.0},
        rise_min=1.0,
        plateau_min=5.0,
        decay_min=6.0,
        probability_per_min=0.0009,
        cooldown_min=120.0,
    ),
    EventTemplate(
        id="microwave_food",
        name="Microwave / hot food",
        category="occupant",
        description="Food heating — VOC + humidity bump for a few minutes.",
        channels={"voc": 0.45, "humidity": 5.0, "temperature": 0.3},
        rise_min=0.5,
        plateau_min=2.0,
        decay_min=5.0,
        probability_per_min=0.003,
        occupied_only=True,
    ),
    EventTemplate(
        id="construction_dust",
        name="Construction / drilling dust",
        category="fault",
        description="Nearby maintenance work: rare, large PM spike.",
        channels={"pm25": 45.0, "pm10": 90.0, "voc": 0.25},
        rise_min=2.0,
        plateau_min=6.0,
        decay_min=15.0,
        probability_per_min=0.00015,
        cooldown_min=720.0,
    ),
    EventTemplate(
        id="outdoor_smoke_ingress",
        name="Outdoor smoke ingress",
        category="weather",
        description="Wildfire smoke or nearby BBQ: PM2.5 dominates over PM10.",
        channels={"pm25": 60.0, "pm10": 75.0, "voc": 0.4},
        rise_min=5.0,
        plateau_min=20.0,
        decay_min=30.0,
        probability_per_min=0.00008,
        cooldown_min=1440.0,
    ),
    EventTemplate(
        id="pressure_front",
        name="Pressure front passing",
        category="weather",
        description="Weather front: pressure dips ~3–5 hPa over an hour.",
        channels={"pressure": -350.0},
        rise_min=20.0,
        plateau_min=30.0,
        decay_min=40.0,
        probability_per_min=0.00006,
        cooldown_min=1440.0,
    ),
    EventTemplate(
        id="sensor_glitch",
        name="Sensor read glitch",
        category="fault",
        description="Brief electronics glitch — one channel jumps and recovers.",
        channels={"co2": 220.0},
        rise_min=0.1,
        plateau_min=0.2,
        decay_min=0.5,
        probability_per_min=0.0005,
        cooldown_min=240.0,
    ),
    EventTemplate(
        id="hand_sanitizer",
        name="Hand sanitizer use",
        category="cleaning",
        description="Alcohol gel evaporating near the sensor.",
        channels={"voc": 0.55},
        rise_min=0.2,
        plateau_min=0.5,
        decay_min=2.0,
        probability_per_min=0.008,
        occupied_only=True,
        cooldown_min=20.0,
    ),
    EventTemplate(
        id="fan_off",
        name="HVAC fan switched off",
        category="hvac",
        description="Ventilation pause — CO₂ climbs faster than usual.",
        channels={"co2": 120.0, "humidity": 2.0},
        rise_min=3.0,
        plateau_min=6.0,
        decay_min=5.0,
        probability_per_min=0.0007,
        occupied_only=True,
        cooldown_min=240.0,
    ),
)


_TEMPLATES_BY_ID: dict[str, EventTemplate] = {t.id: t for t in EVENT_TEMPLATES}


def list_event_templates() -> tuple[EventTemplate, ...]:
    return EVENT_TEMPLATES


def get_event_template(event_id: str) -> EventTemplate | None:
    return _TEMPLATES_BY_ID.get(event_id)


# ---------------------------------------------------------------------------
# Live event instances + engine
# ---------------------------------------------------------------------------


@dataclass
class MicroEventInstance:
    """One live micro-event in a zone."""

    template_id: str
    zone_id: str
    started_at: datetime
    # Per-channel peak (scaled in [0.5x .. 1.0x] of template magnitude).
    peaks: dict[str, float]
    duration_min: float
    rise_min: float
    plateau_min: float
    decay_min: float

    def envelope(self, ts: datetime) -> float:
        """Return the envelope multiplier in [0, 1] at ``ts``."""
        elapsed = (ts - self.started_at).total_seconds() / 60.0
        if elapsed < 0.0:
            return 0.0
        if elapsed < self.rise_min:
            return elapsed / max(self.rise_min, 1e-6)
        if elapsed < self.rise_min + self.plateau_min:
            return 1.0
        if elapsed < self.duration_min:
            tail = elapsed - self.rise_min - self.plateau_min
            return max(0.0, 1.0 - tail / max(self.decay_min, 1e-6))
        return 0.0

    def is_finished(self, ts: datetime) -> bool:
        return (ts - self.started_at).total_seconds() / 60.0 >= self.duration_min

    def deltas_at(self, ts: datetime) -> dict[str, float]:
        env = self.envelope(ts)
        if env <= 0.0:
            return {}
        return {ch: peak * env for ch, peak in self.peaks.items()}


@dataclass
class MicroEventEngine:
    """Stochastic event scheduler — one engine per simulation.

    The engine is intentionally simple: a list of active events plus a
    map of last-fired timestamps per (zone, template). Trigger checks
    run once per zone per tick.
    """

    seed: int | None = None
    probability_scale: float = 1.0
    enabled_template_ids: set[str] | None = None
    rng: random.Random = field(init=False)
    _active: list[MicroEventInstance] = field(default_factory=list, init=False)
    _last_fired: dict[tuple[str, str], datetime] = field(default_factory=dict, init=False)
    _pending_started: list[MicroEventInstance] = field(default_factory=list, init=False)
    _pending_ended: list[MicroEventInstance] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed if self.seed is not None else 0)

    # -- public --------------------------------------------------------

    def templates(self) -> Iterable[EventTemplate]:
        if self.enabled_template_ids is None:
            return EVENT_TEMPLATES
        return tuple(t for t in EVENT_TEMPLATES if t.id in self.enabled_template_ids)

    def step(
        self,
        ts: datetime,
        zone_id: str,
        *,
        dt_min: float,
        occupancy: int,
        schedule_kind: str,
    ) -> tuple[dict[str, float], list[MicroEventInstance]]:
        """Advance the engine for one zone-tick.

        Returns ``(deltas, active_in_zone)`` where ``deltas`` is the
        summed channel impact to apply to the reading, and
        ``active_in_zone`` is the list of currently-active events in
        this zone (so the caller can attach them to metadata).
        """
        ts = _ensure_utc(ts)
        dt_min = max(dt_min, 0.0)

        # 1) Roll for new triggers in this zone.
        for tpl in self.templates():
            if tpl.zone_kinds is not None and schedule_kind not in tpl.zone_kinds:
                continue
            if tpl.occupied_only and occupancy <= 0:
                continue
            # Already active in this zone? Skip.
            if any(e.template_id == tpl.id and e.zone_id == zone_id for e in self._active):
                continue
            # Cooldown gate.
            last = self._last_fired.get((zone_id, tpl.id))
            if last is not None:
                if (ts - last).total_seconds() / 60.0 < tpl.cooldown_min:
                    continue
            # Bernoulli trial scaled to actual dt.
            p = 1.0 - math.exp(-tpl.probability_per_min * self.probability_scale * dt_min)
            if self.rng.random() < p:
                self._spawn(tpl, zone_id, ts)

        # 2) Sum active deltas for this zone, expire finished events.
        deltas: dict[str, float] = {}
        active_in_zone: list[MicroEventInstance] = []
        still_active: list[MicroEventInstance] = []
        for ev in self._active:
            if ev.is_finished(ts):
                self._pending_ended.append(ev)
                continue
            still_active.append(ev)
            if ev.zone_id != zone_id:
                continue
            active_in_zone.append(ev)
            for ch, dv in ev.deltas_at(ts).items():
                deltas[ch] = deltas.get(ch, 0.0) + dv
        self._active = still_active
        return deltas, active_in_zone

    def pop_started_events(self) -> list[MicroEventInstance]:
        out, self._pending_started = self._pending_started, []
        return out

    def pop_ended_events(self) -> list[MicroEventInstance]:
        out, self._pending_ended = self._pending_ended, []
        return out

    def active_snapshot(self) -> list[MicroEventInstance]:
        return list(self._active)

    # -- internals -----------------------------------------------------

    def _spawn(self, tpl: EventTemplate, zone_id: str, ts: datetime) -> None:
        peaks: dict[str, float] = {}
        for ch, mag in tpl.channels.items():
            scale = 0.5 + 0.5 * self.rng.random()  # 50% .. 100% of nominal
            peaks[ch] = mag * scale
        inst = MicroEventInstance(
            template_id=tpl.id,
            zone_id=zone_id,
            started_at=ts,
            peaks=peaks,
            duration_min=tpl.duration_min,
            rise_min=tpl.rise_min,
            plateau_min=tpl.plateau_min,
            decay_min=tpl.decay_min,
        )
        self._active.append(inst)
        self._pending_started.append(inst)
        self._last_fired[(zone_id, tpl.id)] = ts


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


__all__ = [
    "EVENT_TEMPLATES",
    "EventTemplate",
    "MicroEventEngine",
    "MicroEventInstance",
    "get_event_template",
    "list_event_templates",
]
