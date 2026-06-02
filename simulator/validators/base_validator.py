"""Common base class for validators."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from ..models.reading import SensorReading
from .validation_report import Finding


class BaseValidator:
    """Subclasses implement :meth:`validate` returning a list of findings.

    The constructor stores a context dict containing:
      - ``building``: optional building / zone config
      - ``hierarchy``: optional meter hierarchy map
      - ``scenarios``: set of active scenario IDs
      - ``rules``: parsed global rules dict
    """

    name: str = "base"

    def __init__(self, ctx: dict[str, Any]) -> None:
        self.ctx = ctx

    # -- helpers shared by subclasses --------------------------------------

    def scenarios_active(self) -> set[str]:
        return set(self.ctx.get("scenarios", set()))

    def has_scenario(self, name: str) -> bool:
        return name in self.scenarios_active()

    @staticmethod
    def group_by_device(
        readings: Iterable[SensorReading],
    ) -> dict[str, list[SensorReading]]:
        out: dict[str, list[SensorReading]] = defaultdict(list)
        for r in readings:
            out[r.device_eui].append(r)
        for device_id in out:
            out[device_id].sort(key=lambda r: r.timestamp)
        return dict(out)

    # -- abstract ---------------------------------------------------------

    def validate(self, readings: list[SensorReading]) -> list[Finding]:
        raise NotImplementedError


__all__ = ["BaseValidator"]
