"""Cross-sensor correlation checks.

The first version implements two practical, lagged correlation tests:

1. Occupancy → CO₂ (within the same zone), where occupancy comes either
   from the IAQ reading's metadata (``metadata.occupancy``) or from a
   colocated occupancy/people sensor.
2. Outdoor temperature → HVAC active power (or ``cooling_energy``).

Both use Pearson correlation across configured lag windows. We use the
*best* (max) correlation across allowed lags — if even that is below the
``medium`` threshold, we emit a warning.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import fmean
from typing import Any

from ..models.reading import SensorReading
from .base_validator import BaseValidator
from .validation_report import Finding, _iso


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = min(len(xs), len(ys))
    if n < 5:
        return None
    xs = xs[:n]
    ys = ys[:n]
    mx, my = fmean(xs), fmean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx2 = sum((x - mx) ** 2 for x in xs)
    dy2 = sum((y - my) ** 2 for y in ys)
    if dx2 <= 0 or dy2 <= 0:
        return None
    return num / ((dx2 ** 0.5) * (dy2 ** 0.5))


def _best_lagged(xs: list[float], ys: list[float], lags: list[int]) -> tuple[float | None, int]:
    """Return (best_corr, best_lag) where lag shifts xs forward by ``lag`` steps."""
    best: tuple[float | None, int] = (None, 0)
    for lag in lags:
        if lag >= len(xs) or lag >= len(ys):
            continue
        # Align: xs leads ys by `lag` samples.
        xs_lag = xs[: len(xs) - lag] if lag > 0 else xs
        ys_lag = ys[lag:] if lag > 0 else ys
        c = _pearson(xs_lag, ys_lag)
        if c is None:
            continue
        if best[0] is None or c > best[0]:
            best = (c, lag)
    return best


class CorrelationValidator(BaseValidator):
    name = "correlation"

    def validate(self, readings: list[SensorReading]) -> list[Finding]:
        findings: list[Finding] = []
        rules = self.ctx.get("rules", {})
        lags_cfg = rules.get("correlation_lags_minutes", {})
        thresholds = rules.get(
            "correlation_thresholds", {"strong": 0.55, "medium": 0.30, "weak": 0.10}
        )

        # 1) Occupancy → CO2 (per zone)
        findings.extend(
            self._occupancy_to_co2(
                readings,
                lags_min=lags_cfg.get("occupancy_to_co2", [5, 10, 15, 20, 30]),
                medium=float(thresholds.get("medium", 0.30)),
            )
        )

        # 2) Outdoor temperature → HVAC power / cooling energy
        findings.extend(
            self._outdoor_temp_to_hvac(
                readings,
                lags_min=lags_cfg.get("outdoor_temp_to_hvac", [15, 30, 60]),
                medium=float(thresholds.get("medium", 0.30)),
            )
        )

        return findings

    # -- check 1: occupancy → CO2 -----------------------------------------

    def _occupancy_to_co2(
        self,
        readings: list[SensorReading],
        *,
        lags_min: list[int],
        medium: float,
    ) -> list[Finding]:
        # Index per zone.
        per_zone: dict[str, list[SensorReading]] = defaultdict(list)
        for r in readings:
            zone_id = r.metadata.get("zone_id") if r.metadata else None
            if zone_id:
                per_zone[zone_id].append(r)

        findings: list[Finding] = []
        for zone_id, zr in per_zone.items():
            zr.sort(key=lambda x: x.timestamp)
            occ_series: list[float] = []
            co2_series: list[float] = []
            for r in zr:
                if r.sensor_type == "iaq" and "co2" in r.data:
                    occ = r.metadata.get("occupancy") if r.metadata else None
                    if occ is None:
                        continue
                    occ_series.append(float(occ))
                    co2_series.append(float(r.data["co2"]))
                elif r.sensor_type in ("people_counter", "occupancy_sensor"):
                    occ = r.data.get("occupancy") or r.data.get("people_count")
                    if occ is not None and r.metadata.get("co2") is not None:
                        occ_series.append(float(occ))
                        co2_series.append(float(r.metadata["co2"]))

            if len(occ_series) < 10:
                continue
            # Sample interval in minutes (median-ish).
            best_corr, best_lag = _best_lagged(occ_series, co2_series, lags_min)
            if best_corr is None:
                continue
            if best_corr < medium:
                findings.append(
                    Finding(
                        severity="warning",
                        category="cross_sensor_correlation",
                        entity_type="zone",
                        entity_id=zone_id,
                        message=(
                            f"Weak occupancy→CO2 correlation in zone '{zone_id}': "
                            f"best={best_corr:.2f} at lag={best_lag} (expected ≥ {medium})"
                        ),
                        start_timestamp=_iso(zr[0].timestamp),
                        end_timestamp=_iso(zr[-1].timestamp),
                        suggested_fix=(
                            "Check that CO2 generator responds to occupancy with "
                            "appropriate buildup and decay constants."
                        ),
                    )
                )
        return findings

    # -- check 2: outdoor temp → HVAC -------------------------------------

    def _outdoor_temp_to_hvac(
        self,
        readings: list[SensorReading],
        *,
        lags_min: list[int],
        medium: float,
    ) -> list[Finding]:
        # Pull a single outdoor temperature time-series from any device that
        # carries it (typically an HVAC virtual point or weather feed).
        outdoor: list[tuple[Any, float]] = []
        hvac: list[tuple[Any, float]] = []
        for r in readings:
            if r.sensor_type == "hvac" and "outside_air_temperature" in r.data:
                outdoor.append((r.timestamp, float(r.data["outside_air_temperature"])))
            if r.sensor_type == "energy_meter" and r.metadata.get("submeter") == "hvac":
                if "active_power" in r.data:
                    hvac.append((r.timestamp, float(r.data["active_power"])))
            if r.sensor_type == "hvac" and "active_power" in r.data:
                hvac.append((r.timestamp, float(r.data["active_power"])))

        if len(outdoor) < 10 or len(hvac) < 10:
            return []
        outdoor.sort(); hvac.sort()
        # Align by index (assumes shared cadence; this is a v1 heuristic).
        n = min(len(outdoor), len(hvac))
        xs = [v for _, v in outdoor[:n]]
        ys = [v for _, v in hvac[:n]]
        best_corr, best_lag = _best_lagged(xs, ys, lags_min)
        if best_corr is None:
            return []
        if best_corr < medium:
            return [
                Finding(
                    severity="warning",
                    category="cross_sensor_correlation",
                    entity_type="building",
                    entity_id="<building>",
                    message=(
                        f"Weak outdoor-temperature→HVAC-power correlation: "
                        f"best={best_corr:.2f} at lag={best_lag} (expected ≥ {medium})"
                    ),
                    start_timestamp=_iso(outdoor[0][0]),
                    end_timestamp=_iso(outdoor[-1][0]),
                    suggested_fix=(
                        "Increase coupling between outdoor temperature and HVAC "
                        "active power in the simulator."
                    ),
                )
            ]
        return []


__all__ = ["CorrelationValidator"]
