"""Helpers for loading the YAML rule files under ``simulator/rules/``."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

RULES_DIR = Path(__file__).resolve().parent.parent / "rules"


@lru_cache(maxsize=None)
def load_rules(name: str) -> dict[str, Any]:
    """Load and cache a YAML rules file from ``simulator/rules/``.

    Parameters
    ----------
    name:
        File stem (without ``.yaml``), e.g. ``"iaq_rules"``.
    """
    path = RULES_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Rules file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Top-level rules in {path} must be a mapping")
    return data


def load_all_rules() -> dict[str, dict[str, Any]]:
    """Load every shipped rule file keyed by stem."""
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(RULES_DIR.glob("*.yaml")):
        out[path.stem] = load_rules(path.stem)
    return out
