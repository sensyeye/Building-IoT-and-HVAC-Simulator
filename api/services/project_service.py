"""Project persistence service.

Projects are stored as individual JSON files under ``data/projects/``.
This is a deliberately simple persistence layer for the MVP — it can be
swapped for a real database later without touching routes or templates.

The service is the *only* place that reads or writes project files. Routes
must call into this service rather than touching the filesystem directly.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data" / "projects"


def _coverage_severity(ratio: float | None) -> str:
    """Bucket a coverage ratio into an UI severity tag.

    Returns one of ``"unknown" | "low" | "ok" | "over"``. The buckets
    match the thresholds in :meth:`ProjectService.derive_status` so the
    template only has to do a string compare.
    """
    if ratio is None:
        return "unknown"
    if ratio < 0.5:
        return "low"
    if ratio > 1.25:
        return "over"
    return "ok"


# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------


@dataclass
class Project:
    """Lightweight project record.

    Only fields needed by the dashboard / create form live here. Detailed
    building configuration is layered on later via dedicated services.
    """

    id: str
    name: str
    building_type: str
    city: str
    timezone: str
    area_m2: float
    floors: int
    demo_depth: str  # "light" | "standard" | "deep"
    created_at: str
    updated_at: str
    device_count: int = 0
    last_validation_score: float | None = None
    last_run_status: str | None = None  # "succeeded" | "failed" | "running" | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ProjectService:
    """CRUD-light service backed by a directory of JSON files."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = Path(data_dir) if data_dir else DATA_DIR
        self._data_dir.mkdir(parents=True, exist_ok=True)

    # -- helpers -----------------------------------------------------------

    def _path(self, project_id: str) -> Path:
        return self._data_dir / f"{project_id}.json"

    def _secrets_path(self, project_id: str) -> Path:
        """File holding broker credentials. Gitignored, never committed."""
        return self._data_dir / f"{project_id}.secrets.json"

    def _scenarios_path(self, project_id: str) -> Path:
        """File holding active scenario assignments for the project."""
        return self._data_dir / f"{project_id}.scenarios.json"

    def _config_path(self, project_id: str) -> Path:
        """File holding the project-managed simulator config (YAML)."""
        return self._data_dir / f"{project_id}.config.yaml"

    def has_managed_config(self, project_id: str) -> bool:
        return self._config_path(project_id).exists()

    def managed_config_path(self, project_id: str) -> Path:
        return self._config_path(project_id)

    @staticmethod
    def _slugify(name: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        return slug or "project"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    # -- public API --------------------------------------------------------

    def list(self) -> list[Project]:
        items: list[Project] = []
        for p in sorted(self._data_dir.glob("*.json")):
            if p.name.endswith(".secrets.json"):
                continue
            if p.name.endswith(".scenarios.json"):
                continue
            if p.name.endswith(".events.jsonl"):
                continue
            if p.name.endswith(".config.yaml"):
                continue
            try:
                items.append(self._load_path(p))
            except (OSError, json.JSONDecodeError, TypeError):
                # Skip corrupt records rather than crashing the dashboard.
                continue
        items.sort(key=lambda x: x.created_at, reverse=True)
        return items

    def get(self, project_id: str) -> Project | None:
        path = self._path(project_id)
        if not path.exists():
            return None
        return self._load_path(path)

    def create(self, payload: dict[str, Any]) -> Project:
        name = str(payload.get("name", "")).strip()
        if not name:
            raise ValueError("Project name is required")

        slug = self._slugify(name)
        # Append a short uuid suffix to avoid collisions.
        project_id = f"{slug}-{uuid.uuid4().hex[:6]}"
        now = self._now()

        project = Project(
            id=project_id,
            name=name,
            building_type=str(payload.get("building_type", "office")),
            city=str(payload.get("city", "")),
            timezone=str(payload.get("timezone", "UTC")),
            area_m2=float(payload.get("area_m2", 0) or 0),
            floors=int(payload.get("floors", 1) or 1),
            demo_depth=str(payload.get("demo_depth", "standard")),
            created_at=now,
            updated_at=now,
        )
        self._save(project)
        return project

    # -- integration / secrets --------------------------------------------

    # Allow-listed keys for the Sensgreen MQTT integration block. Anything
    # outside this set is dropped on save so unknown fields cannot leak
    # into the file.
    _INTEGRATION_FIELDS = (
        "host",
        "port",
        "username",
        "password",
        "topic",
        "error_topic",
        "tls",
        "client_id",
    )

    def get_integration(self, project_id: str) -> dict[str, Any] | None:
        """Return the Sensgreen MQTT integration record for ``project_id``.

        Returns ``None`` when no integration has been configured. The
        returned dict has the same shape that
        :meth:`SensgreenMqttPublisher.from_integration` consumes.
        """
        path = self._secrets_path(project_id)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        return data.get("sensgreen_mqtt") or None

    def set_integration(
        self, project_id: str, integration: dict[str, Any]
    ) -> dict[str, Any]:
        """Persist (or update) the Sensgreen MQTT integration block.

        Only the allow-listed keys are kept. Validation:
        ``host`` and ``topic`` must be non-empty; ``port`` defaults to 1881.
        """
        if self.get(project_id) is None:
            raise ValueError(f"unknown project: {project_id}")

        cleaned: dict[str, Any] = {}
        for key in self._INTEGRATION_FIELDS:
            if key in integration and integration[key] not in (None, ""):
                cleaned[key] = integration[key]

        host = str(cleaned.get("host", "")).strip()
        topic = str(cleaned.get("topic", "")).strip()
        if not host:
            raise ValueError("integration.host is required")
        if not topic:
            raise ValueError("integration.topic is required")

        cleaned["host"] = host
        cleaned["topic"] = topic
        cleaned["port"] = int(cleaned.get("port", 1881))
        cleaned["tls"] = bool(cleaned.get("tls", False))
        if "client_id" not in cleaned:
            cleaned["client_id"] = f"sensgreen-simulator-{project_id}"

        path = self._secrets_path(project_id)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(
                {
                    "project_id": project_id,
                    "updated_at": self._now(),
                    "sensgreen_mqtt": cleaned,
                },
                fh,
                indent=2,
                sort_keys=True,
            )
        return cleaned

    # -- scenarios --------------------------------------------------------

    def get_scenarios(self, project_id: str) -> list[dict[str, Any]]:
        """Return the list of active scenario assignments.

        Shape per entry::

            {"id": "<scenario_id>", "enabled": bool,
             "start": "<iso or null>", "end": "<iso or null>"}

        Order is preserved as the user set it. Returns ``[]`` when no
        scenarios file exists for the project.
        """
        path = self._scenarios_path(project_id)
        if not path.exists():
            return []
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return []
        items = data.get("scenarios") or []
        if not isinstance(items, list):
            return []
        return [self._normalize_scenario_entry(item) for item in items]

    def set_scenarios(
        self, project_id: str, scenarios: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Persist (overwrite) the project's scenario assignments.

        Unknown scenario ids are silently dropped — they cannot affect
        the run because the validator never sees them. Duplicate ids
        keep the *first* occurrence.
        """
        if self.get(project_id) is None:
            raise ValueError(f"unknown project: {project_id}")

        # Lazy import to avoid pulling the simulator module into tests
        # that only exercise the project service.
        from simulator.scenarios import known_ids as _known

        catalog_ids = _known()
        cleaned: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in scenarios:
            if not isinstance(raw, dict):
                continue
            sid = str(raw.get("id", "")).strip()
            if not sid or sid not in catalog_ids or sid in seen:
                continue
            seen.add(sid)
            cleaned.append(self._normalize_scenario_entry(raw))

        path = self._scenarios_path(project_id)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(
                {
                    "project_id": project_id,
                    "updated_at": self._now(),
                    "scenarios": cleaned,
                },
                fh,
                indent=2,
                sort_keys=True,
            )
        return cleaned

    def active_scenario_ids(self, project_id: str) -> list[str]:
        """Return scenario ids that are currently enabled (no window check)."""
        return [s["id"] for s in self.get_scenarios(project_id) if s.get("enabled")]

    @staticmethod
    def _normalize_scenario_entry(raw: dict[str, Any]) -> dict[str, Any]:
        target_zone_ids_raw = raw.get("target_zone_ids")
        target_zone_ids: list[str] | None
        if isinstance(target_zone_ids_raw, list):
            cleaned = [
                str(z).strip()
                for z in target_zone_ids_raw
                if str(z or "").strip()
            ]
            target_zone_ids = cleaned or None
        else:
            target_zone_ids = None
        target_hvac = raw.get("target_hvac_zone_id")
        target_hvac_str = (
            str(target_hvac).strip()
            if target_hvac not in (None, "", False)
            else None
        ) or None
        return {
            "id": str(raw.get("id", "")),
            "enabled": bool(raw.get("enabled", False)),
            "start": (str(raw["start"]) if raw.get("start") else None),
            "end": (str(raw["end"]) if raw.get("end") else None),
            "target_hvac_zone_id": target_hvac_str,
            "target_zone_ids": target_zone_ids,
        }

    # -- managed simulator config (YAML) ----------------------------------

    def get_config(self, project_id: str) -> dict[str, Any]:
        """Return the project-managed config as a plain dict.

        Creates a sensible default skeleton (building + one zone, no
        devices) the first time it is requested, so the UI always has
        something to render.
        """
        if self.get(project_id) is None:
            raise ValueError(f"unknown project: {project_id}")
        path = self._config_path(project_id)
        if not path.exists():
            return self._default_config_skeleton(project_id)
        import yaml  # local import keeps the cold-start cheap
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except (OSError, yaml.YAMLError):
            return self._default_config_skeleton(project_id)
        if not isinstance(data, dict):
            return self._default_config_skeleton(project_id)
        return data

    def get_config_yaml(self, project_id: str) -> str:
        """Return the raw YAML text of the managed config.

        When no file exists yet, the default skeleton is serialised so
        the editor never opens blank.
        """
        if self.get(project_id) is None:
            raise ValueError(f"unknown project: {project_id}")
        path = self._config_path(project_id)
        if path.exists():
            try:
                return path.read_text(encoding="utf-8")
            except OSError:
                pass
        import yaml
        return yaml.safe_dump(
            self._default_config_skeleton(project_id), sort_keys=False
        )

    def set_config_yaml(
        self, project_id: str, text: str, *, validate: bool = True
    ) -> dict[str, Any]:
        """Persist raw YAML text. Parsed dict is returned on success."""
        if self.get(project_id) is None:
            raise ValueError(f"unknown project: {project_id}")
        if not isinstance(text, str):
            raise ValueError("yaml text must be a string")
        import yaml
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"YAML parse error: {exc}") from exc
        if parsed is None:
            raise ValueError("YAML is empty")
        if not isinstance(parsed, dict):
            raise ValueError("top-level YAML must be a mapping")
        # Reuse set_config so the same atomic write + load_config
        # validation path applies.
        self.set_config(project_id, parsed, validate=validate)
        return parsed

    def set_config(
        self, project_id: str, cfg: dict[str, Any], *, validate: bool = True
    ) -> dict[str, Any]:
        """Persist ``cfg`` as the project's managed YAML config.

        When ``validate`` is True (default) the config is round-tripped
        through :func:`simulator.config_loader.load_config` so that
        malformed payloads are rejected before they ever reach a run.
        """
        if self.get(project_id) is None:
            raise ValueError(f"unknown project: {project_id}")
        if not isinstance(cfg, dict):
            raise ValueError("config payload must be a mapping")

        path = self._config_path(project_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        import yaml
        if validate:
            # The loader works on real files, so write atomically to a
            # temp file, validate, and only then promote.
            tmp = path.with_suffix(path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                yaml.safe_dump(cfg, fh, sort_keys=False)
            try:
                # Lazy import to avoid pulling the simulator at module
                # import time (tests stub this module independently).
                from simulator.config_loader import load_config
                load_config(tmp)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
            tmp.replace(path)
        else:
            with path.open("w", encoding="utf-8") as fh:
                yaml.safe_dump(cfg, fh, sort_keys=False)
        return cfg

    # -- derived status ----------------------------------------------------

    def derive_status(self, project_id: str) -> dict[str, Any]:
        """Return a derived snapshot used by the Overview tab.

        Pulls the **live** device and zone counts from the managed YAML
        (the same source the Devices tab edits), computes the
        modeled-area coverage versus the building's declared area, and
        attaches the most recent validation and run events.

        This is the single source of truth for the Overview card so the
        old persistent ``Project.device_count`` field cannot drift out
        of sync with what the user actually edits.
        """
        project = self.get(project_id)
        if project is None:
            raise ValueError(f"unknown project: {project_id}")

        cfg = self.get_config(project_id)
        building = cfg.get("building") or {}
        zones = building.get("zones") or []
        devices = cfg.get("devices") or []
        device_count = len([d for d in devices if isinstance(d, dict)])
        zone_count = len([z for z in zones if isinstance(z, dict)])

        # Coverage: sum of zone area_m2 vs declared building area.
        modeled_area = 0.0
        for z in zones:
            if not isinstance(z, dict):
                continue
            try:
                modeled_area += float(z.get("area_m2") or 0.0)
            except (TypeError, ValueError):
                continue
        building_area = float(project.area_m2 or 0.0)
        coverage_ratio: float | None
        coverage_recommendation: str | None
        if building_area <= 0.0:
            coverage_ratio = None
            coverage_recommendation = (
                "Set the building area on the project to track zone coverage."
            )
        else:
            coverage_ratio = modeled_area / building_area
            if coverage_ratio < 0.5:
                coverage_recommendation = (
                    "Add zones or generate them from the building area — "
                    "less than half of the building is modelled."
                )
            elif coverage_ratio > 1.25:
                coverage_recommendation = (
                    "Zone areas exceed the declared building area — "
                    "double-check zone sizes or building dimensions."
                )
            else:
                coverage_recommendation = None

        coverage = {
            "modeled_area_m2": round(modeled_area, 1),
            "building_area_m2": round(building_area, 1),
            "ratio": (round(coverage_ratio, 4) if coverage_ratio is not None else None),
            "recommendation": coverage_recommendation,
            # Severity is what the UI uses to choose banner colour.
            "severity": _coverage_severity(coverage_ratio),
        }

        # Last validation + last run (live or historical). Done lazily
        # so the project service never imports the runners.
        last_validation = self._latest_event(project_id, kinds=("scenario",))
        # "scenario" is the current home for validation events; once a
        # dedicated kind exists we'll widen this tuple.
        last_run = self._latest_event(
            project_id, kinds=("live_run", "historical_run", "bridge_test")
        )

        return {
            "device_count": device_count,
            "zone_count": zone_count,
            "coverage": coverage,
            "last_validation": last_validation,
            "last_run": last_run,
        }

    def _latest_event(
        self, project_id: str, *, kinds: tuple[str, ...]
    ) -> dict[str, Any] | None:
        """Return the newest event whose kind ∈ ``kinds`` (or ``None``).

        Imports the EventService lazily so unit tests that only
        exercise ProjectService don't pay the price.
        """
        from api.services.event_service import event_service

        # ``recent`` already returns newest-first; we pull a small page
        # for each requested kind and pick the global max by ts.
        candidates: list[dict[str, Any]] = []
        for k in kinds:
            for ev in event_service.recent(project_id, limit=5, kind=k):
                candidates.append(ev.to_dict())
        if not candidates:
            return None
        candidates.sort(key=lambda e: e.get("ts") or "", reverse=True)
        latest = candidates[0]
        # Trim to the fields the dashboard needs — keeps the JSON
        # response small and avoids leaking secret-bearing details.
        return {
            "ts": latest.get("ts"),
            "kind": latest.get("kind"),
            "status": latest.get("status"),
            "summary": latest.get("summary"),
        }

    # -- zone CRUD ---------------------------------------------------------

    def add_zone(self, project_id: str, zone: dict[str, Any]) -> dict[str, Any]:
        cfg = self.get_config(project_id)
        zones = cfg.setdefault("building", {}).setdefault("zones", [])
        zid = str(zone.get("id", "")).strip()
        if not zid:
            raise ValueError("zone.id is required")
        if any(str(z.get("id")) == zid for z in zones):
            raise ValueError(f"zone id already exists: {zid}")
        zones.append(self._normalize_zone(zone))
        self.set_config(project_id, cfg, validate=False)
        return cfg

    def update_zone(
        self, project_id: str, zone_id: str, zone: dict[str, Any]
    ) -> dict[str, Any]:
        """Patch an existing zone in place.

        Merges the incoming fields onto the stored zone, then re-runs
        ``_normalize_zone`` so unknown keys are dropped and types are
        coerced. The zone id cannot be changed through this path.
        """
        cfg = self.get_config(project_id)
        zones = cfg.setdefault("building", {}).setdefault("zones", [])
        target = str(zone_id).strip()
        for i, z in enumerate(zones):
            if str(z.get("id")) == target:
                merged = {**z, **zone, "id": target}
                zones[i] = self._normalize_zone(merged)
                self.set_config(project_id, cfg, validate=False)
                return cfg
        raise KeyError(f"zone not found: {zone_id}")

    def apply_generated_zones(
        self, project_id: str, generated_zones: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Replace ``building.zones`` with the generator output.

        Refuses if any device is currently assigned to a zone that would not
        exist after the replacement, to avoid orphaning devices on disk.
        Returns the post-replacement config dict.
        """
        cfg = self.get_config(project_id)
        new_ids = {str(z.get("id")) for z in generated_zones if isinstance(z, dict)}
        devices = cfg.get("devices") or []
        orphaned = sorted(
            {
                str(d.get("device_eui"))
                for d in devices
                if isinstance(d, dict) and str(d.get("zone_id")) not in new_ids
            }
        )
        if orphaned:
            raise ValueError(
                "cannot replace zones: these devices would be orphaned — "
                f"reassign or remove them first: {', '.join(orphaned[:5])}"
                + (" …" if len(orphaned) > 5 else "")
            )
        cfg.setdefault("building", {})["zones"] = [
            self._normalize_zone(z) for z in generated_zones
        ]
        self.set_config(project_id, cfg, validate=False)
        return cfg

    def remove_zone(self, project_id: str, zone_id: str) -> dict[str, Any]:
        cfg = self.get_config(project_id)
        zones = cfg.get("building", {}).get("zones", []) or []
        keep = [z for z in zones if str(z.get("id")) != zone_id]
        if len(keep) == len(zones):
            raise KeyError(f"zone not found: {zone_id}")
        # Refuse to delete a zone that still has devices attached.
        used = {
            str(d.get("zone_id"))
            for d in cfg.get("devices", []) or []
        }
        if zone_id in used:
            raise ValueError(
                f"cannot remove zone '{zone_id}': devices are still assigned to it"
            )
        cfg["building"]["zones"] = keep
        self.set_config(project_id, cfg, validate=False)
        return cfg

    # -- HVAC zone CRUD ----------------------------------------------------

    def add_hvac_zone(
        self, project_id: str, hvac_zone: dict[str, Any]
    ) -> dict[str, Any]:
        cfg = self.get_config(project_id)
        building = cfg.setdefault("building", {})
        hvac_zones = building.setdefault("hvac_zones", [])
        normalized = self._normalize_hvac_zone(hvac_zone)
        if any(str(z.get("id")) == normalized["id"] for z in hvac_zones):
            raise ValueError(f"hvac_zone id already exists: {normalized['id']}")
        hvac_zones.append(normalized)
        self.set_config(project_id, cfg, validate=False)
        return cfg

    def remove_hvac_zone(self, project_id: str, hvac_zone_id: str) -> dict[str, Any]:
        cfg = self.get_config(project_id)
        building = cfg.get("building") or {}
        hvac_zones = building.get("hvac_zones") or []
        keep = [z for z in hvac_zones if str(z.get("id")) != hvac_zone_id]
        if len(keep) == len(hvac_zones):
            raise KeyError(f"hvac_zone not found: {hvac_zone_id}")
        # Refuse to delete an HVAC zone still referenced by any room.
        served = sorted(
            str(z.get("id"))
            for z in building.get("zones") or []
            if str(z.get("hvac_zone_id") or "") == hvac_zone_id
        )
        if served:
            raise ValueError(
                f"cannot remove hvac_zone '{hvac_zone_id}': "
                f"rooms still reference it ({', '.join(served[:5])}"
                + (" …)" if len(served) > 5 else ")")
            )
        building["hvac_zones"] = keep
        self.set_config(project_id, cfg, validate=False)
        return cfg

    # -- device CRUD -------------------------------------------------------

    def add_device(self, project_id: str, device: dict[str, Any]) -> dict[str, Any]:
        cfg = self.get_config(project_id)
        devices = cfg.setdefault("devices", [])
        eui = str(device.get("device_eui", "")).strip().lower()
        if not eui:
            raise ValueError("device_eui is required")
        if any(str(d.get("device_eui", "")).lower() == eui for d in devices):
            raise ValueError(f"device_eui already exists: {eui}")
        devices.append(self._normalize_device(device))
        self.set_config(project_id, cfg, validate=True)
        return cfg

    def update_device(
        self, project_id: str, device_eui: str, device: dict[str, Any]
    ) -> dict[str, Any]:
        cfg = self.get_config(project_id)
        devices = cfg.get("devices", []) or []
        target = device_eui.strip().lower()
        for i, d in enumerate(devices):
            if str(d.get("device_eui", "")).lower() == target:
                merged = self._normalize_device({**d, **device})
                # Don't allow EUI changes through this path.
                merged["device_eui"] = d["device_eui"]
                devices[i] = merged
                cfg["devices"] = devices
                self.set_config(project_id, cfg, validate=True)
                return cfg
        raise KeyError(f"device not found: {device_eui}")

    def remove_device(self, project_id: str, device_eui: str) -> dict[str, Any]:
        cfg = self.get_config(project_id)
        devices = cfg.get("devices", []) or []
        target = device_eui.strip().lower()
        keep = [d for d in devices if str(d.get("device_eui", "")).lower() != target]
        if len(keep) == len(devices):
            raise KeyError(f"device not found: {device_eui}")
        cfg["devices"] = keep
        # No validate=True here: the loader rejects empty device lists,
        # but the user may legitimately delete all devices while editing.
        self.set_config(project_id, cfg, validate=False)
        return cfg

    # -- bulk device generation -------------------------------------------

    def bulk_add_devices(
        self,
        project_id: str,
        items: list[dict[str, Any]],
        *,
        zone_strategy: str = "round_robin",
        name_prefix: str | None = None,
    ) -> dict[str, Any]:
        """Add many devices at once with smart placement and naming.

        ``items`` is a list of ``{"type": str, "count": int,
        "zone_id"?: str, "metadata"?: dict}``. When ``zone_id`` is
        omitted, devices are spread across the project's zones using
        ``zone_strategy``:

        * ``"round_robin"`` — distribute one per zone, cycling.
        * ``"fill"`` — pack as many as possible into the first zone
          before moving on.
        * ``"by_capacity"`` — weight per zone capacity (falls back to
          area_m2, then equal-weight).

        Names are auto-generated as ``"{Zone} {ShortName} {NN}"`` (e.g.
        ``"Lobby IAQ 01"``), continuing the numbering from any existing
        devices in that zone. EUIs are deterministic via
        :func:`generate_eui`; collisions are resolved by appending a
        disambiguating suffix to the name input.
        """
        # Lazy imports — keeps the catalog out of the cold start path
        # for callers that never touch devices.
        from simulator.devices import get_sensor_type, short_name_for
        from simulator.utils.eui import generate_eui

        if not isinstance(items, list) or not items:
            raise ValueError("items must be a non-empty list")
        if zone_strategy not in {"round_robin", "fill", "by_capacity"}:
            raise ValueError(f"unknown zone_strategy: {zone_strategy!r}")

        cfg = self.get_config(project_id)
        zones = ((cfg.get("building") or {}).get("zones")) or []
        if not zones:
            raise ValueError("project has no zones yet — add one first")
        devices = cfg.setdefault("devices", [])

        # Build per-zone state: name + running counter per short-tag.
        zone_by_id: dict[str, dict[str, Any]] = {str(z["id"]): z for z in zones}
        # Seed counters from existing names so we don't collide.
        counters: dict[tuple[str, str], int] = {}
        for d in devices:
            zid = str(d.get("zone_id", ""))
            tag = short_name_for(str(d.get("type", "")))
            counters[(zid, tag)] = counters.get((zid, tag), 0) + 1

        taken_euis = {str(d.get("device_eui", "")).lower() for d in devices}

        # Pre-compute zone assignment for the *full* expansion.
        plan: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        # tuple: (zone, sensor_type_def, item_metadata_override)
        for raw_item in items:
            if not isinstance(raw_item, dict):
                raise ValueError("each item must be an object")
            type_id = str(raw_item.get("type", "")).strip()
            count = int(raw_item.get("count", 0))
            if count <= 0:
                continue
            if count > 1000:
                raise ValueError("count per item is capped at 1000")
            st = get_sensor_type(type_id)
            if st is None:
                raise ValueError(f"unknown sensor type: {type_id!r}")

            explicit_zone = raw_item.get("zone_id")
            if explicit_zone:
                if str(explicit_zone) not in zone_by_id:
                    raise ValueError(f"unknown zone_id: {explicit_zone!r}")
                target_zones = [zone_by_id[str(explicit_zone)]] * count
            else:
                target_zones = self._spread_zones(zones, count, zone_strategy)

            meta_override = raw_item.get("metadata") or {}
            if not isinstance(meta_override, dict):
                raise ValueError("item.metadata must be a mapping")

            for z in target_zones:
                plan.append((z, st.__dict__.copy() | {"_obj": st}, meta_override))

        if not plan:
            raise ValueError("nothing to add (all counts were zero)")

        created: list[dict[str, Any]] = []
        prefix = (name_prefix or "").strip()

        for zone, st_view, meta_override in plan:
            st = st_view["_obj"]
            tag = short_name_for(st.id)
            zid = str(zone["id"])
            counters[(zid, tag)] = counters.get((zid, tag), 0) + 1
            idx = counters[(zid, tag)]
            zone_label = str(zone.get("name") or zid)
            name = f"{zone_label} {tag} {idx:02d}"
            if prefix:
                name = f"{prefix} {name}"

            # Merge sensible defaults + user overrides.
            metadata = dict(st.default_metadata())
            metadata.update({k: v for k, v in meta_override.items() if v not in ("", None)})

            eui = self._unique_eui(project_id, name, taken_euis, generate_eui)
            taken_euis.add(eui)

            device = {
                "device_eui": eui,
                "name": name,
                "type": st.id,
                "zone_id": zid,
                "metadata": metadata,
            }
            devices.append(self._normalize_device(device))
            created.append(device)

        self.set_config(project_id, cfg, validate=True)
        return {"config": cfg, "created": created}

    # -- auto-provision from monitoring profiles --------------------------

    def auto_provision_devices(
        self,
        project_id: str,
        *,
        dry_run: bool = True,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Stage one device per ``required_sensor_type`` per zone, using
        each zone's monitoring profile (explicit or inferred from
        ``room_type``).

        When ``overwrite`` is False (default), zones that already have a
        device of a given sensor type are skipped for that type. When
        ``dry_run`` is True (default), no changes are persisted; the
        caller only gets the preview payload.

        Returns ``{"to_add": [...], "skipped": [...], "config"?: ...}``.
        Each ``to_add`` / ``skipped`` entry is ``{"zone_id", "zone_name",
        "sensor_type", "name", "device_eui", "profile_id",
        "profile_inferred", "reason"?}``.
        """
        from simulator.catalogs import (
            default_profile_for_room_type,
            get_monitoring_profile,
            infer_room_type,
            recommend_devices_for_room,
        )
        from simulator.devices import get_sensor_type, short_name_for
        from simulator.utils.eui import generate_eui

        cfg = self.get_config(project_id)
        building = cfg.get("building") or {}
        building_id = str(building.get("id") or f"bld-{project_id}")
        zones = building.get("zones") or []
        if not isinstance(zones, list) or not zones:
            raise ValueError("project has no zones to provision against")

        devices: list[dict[str, Any]] = list(cfg.get("devices") or [])
        # zone_id -> set of dedup keys already present. For most sensor
        # types the dedup key is the sensor type id; for energy_meter we
        # use ``energy_meter:<submeter>`` so a room can legitimately host
        # multiple meters with different submeter roles.
        present_by_zone: dict[str, set[str]] = {}
        for d in devices:
            if not isinstance(d, dict):
                continue
            zid = str(d.get("zone_id") or "")
            stype = str(d.get("type") or "")
            if not (zid and stype):
                continue
            key = stype
            if stype == "energy_meter":
                meta = d.get("metadata") or {}
                submeter = str((meta.get("submeter") if isinstance(meta, dict) else "") or "main")
                key = f"energy_meter:{submeter}"
            present_by_zone.setdefault(zid, set()).add(key)
        taken_euis: set[str] = {
            str(d.get("device_eui", "")).lower()
            for d in devices
            if isinstance(d, dict)
        }

        to_add: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        staged_devices: list[dict[str, Any]] = []

        for zone in zones:
            if not isinstance(zone, dict):
                continue
            zid = str(zone.get("id") or "")
            if not zid:
                continue
            zname = str(zone.get("name") or zid)

            # ----- decide planning source -----------------------------------
            # If the room declares monitoring_intent or monitoring_richness,
            # use the P9.4 recommendation engine. Otherwise fall back to the
            # legacy monitoring-profile required-list path.
            uses_recommendation = bool(
                str(zone.get("monitoring_intent") or "").strip()
                or str(zone.get("monitoring_richness") or "").strip()
            )

            if uses_recommendation:
                specs = recommend_devices_for_room(zone)
                source = "recommendation"
                # Carry the (intent, richness) on each entry for UI display.
                plan_meta = {
                    "intent": str(zone.get("monitoring_intent") or "").strip() or None,
                    "richness": str(zone.get("monitoring_richness") or "").strip() or None,
                    "profile_id": None,
                    "profile_inferred": False,
                    "source": source,
                }
            else:
                # Resolve profile: explicit → inferred from room_type →
                # inferred from name.
                explicit_profile = str(zone.get("monitoring_profile") or "").strip()
                profile_id: str | None = explicit_profile or None
                profile_inferred = False
                if profile_id is None:
                    room_type = str(zone.get("room_type") or "").strip()
                    if not room_type:
                        room_type, _ = infer_room_type(zname, zid)
                    profile_id = default_profile_for_room_type(room_type)
                    profile_inferred = profile_id is not None

                profile = (
                    get_monitoring_profile(profile_id) if profile_id else None
                )
                if not profile:
                    skipped.append({
                        "zone_id": zid,
                        "zone_name": zname,
                        "sensor_type": None,
                        "profile_id": None,
                        "profile_inferred": False,
                        "source": "profile",
                        "reason": "no monitoring profile for zone",
                    })
                    continue

                required = [
                    str(s) for s in (profile.get("required_sensor_types") or [])
                ]
                # Convert profile required-list into the same DeviceSpec shape
                # the recommendation engine uses, so the rest of the loop is
                # identical for both paths.
                specs = [{"type": s, "role": s, "metadata": {}} for s in required]
                source = "profile"
                plan_meta = {
                    "intent": None,
                    "richness": None,
                    "profile_id": profile_id,
                    "profile_inferred": profile_inferred,
                    "source": source,
                }

            present = present_by_zone.get(zid, set())

            for spec in specs:
                stype_id = str(spec.get("type") or "")
                role = str(spec.get("role") or stype_id)
                extra_meta = dict(spec.get("metadata") or {})
                # Per-role dedup key — lets a single zone hold multiple
                # energy_meter rows distinguished by submeter, while still
                # rejecting two "hvac" submeter mains.
                dedup_key = stype_id
                if stype_id == "energy_meter":
                    submeter = str(extra_meta.get("submeter") or role or "main")
                    dedup_key = f"energy_meter:{submeter}"

                st = get_sensor_type(stype_id)
                if st is None:
                    skipped.append({
                        "zone_id": zid,
                        "zone_name": zname,
                        "sensor_type": stype_id,
                        "role": role,
                        **plan_meta,
                        "reason": f"unknown sensor type '{stype_id}'",
                    })
                    continue
                if not st.implemented:
                    skipped.append({
                        "zone_id": zid,
                        "zone_name": zname,
                        "sensor_type": stype_id,
                        "role": role,
                        **plan_meta,
                        "reason": f"sensor type '{stype_id}' is planned but not emitting yet",
                    })
                    continue
                if dedup_key in present and not overwrite:
                    skipped.append({
                        "zone_id": zid,
                        "zone_name": zname,
                        "sensor_type": stype_id,
                        "role": role,
                        **plan_meta,
                        "reason": "already present",
                    })
                    continue

                tag = short_name_for(st.id)
                # Differentiate by role only when it adds information.
                if role and role not in {stype_id, tag.lower()}:
                    name = f"{zname} {tag} {role}"
                else:
                    name = f"{zname} {tag}"
                eui = self._unique_eui(building_id, name, taken_euis, generate_eui)
                taken_euis.add(eui)
                metadata = dict(st.default_metadata())
                metadata.update(extra_meta)
                entry = {
                    "zone_id": zid,
                    "zone_name": zname,
                    "sensor_type": stype_id,
                    "role": role,
                    "name": name,
                    "device_eui": eui,
                    **plan_meta,
                }
                to_add.append(entry)
                staged_devices.append({
                    "device_eui": eui,
                    "name": name,
                    "type": st.id,
                    "zone_id": zid,
                    "metadata": metadata,
                })
                # Mark as present so a follow-up spec for the same type
                # in the same zone is correctly skipped.
                present_by_zone.setdefault(zid, set()).add(dedup_key)

        result: dict[str, Any] = {
            "to_add": to_add,
            "skipped": skipped,
            "dry_run": dry_run,
        }
        if dry_run or not staged_devices:
            return result

        for d in staged_devices:
            devices.append(self._normalize_device(d))
        cfg["devices"] = devices
        self.set_config(project_id, cfg, validate=True)
        result["config"] = cfg
        return result

    @staticmethod
    def _spread_zones(
        zones: list[dict[str, Any]], count: int, strategy: str
    ) -> list[dict[str, Any]]:
        """Return ``count`` zone references chosen by ``strategy``."""
        if count <= 0 or not zones:
            return []
        if strategy == "fill":
            # Pack into the first zone first; once "full" (no defined
            # capacity → unlimited) move on. For the simple case we
            # just dump everything in the first zone.
            return [zones[0]] * count
        if strategy == "by_capacity":
            weights: list[float] = []
            for z in zones:
                w = z.get("capacity") or z.get("area_m2") or 1.0
                try:
                    weights.append(max(float(w), 0.0))
                except (TypeError, ValueError):
                    weights.append(1.0)
            total = sum(weights) or float(len(zones))
            # Largest-remainder allocation so the counts sum to ``count``.
            raw = [w / total * count for w in weights]
            base = [int(x) for x in raw]
            remainder = count - sum(base)
            # Distribute the remainder to the zones with the largest fractional part.
            order = sorted(
                range(len(zones)),
                key=lambda i: (raw[i] - base[i]),
                reverse=True,
            )
            for i in order[:remainder]:
                base[i] += 1
            out: list[dict[str, Any]] = []
            for z, n in zip(zones, base):
                out.extend([z] * n)
            return out
        # default: round_robin
        return [zones[i % len(zones)] for i in range(count)]

    @staticmethod
    def _unique_eui(
        namespace: str,
        name: str,
        taken: set[str],
        generator,
    ) -> str:
        """Generate an EUI for ``name``, avoiding ``taken`` collisions."""
        candidate = generator(namespace, name)
        if candidate not in taken:
            return candidate
        # Collision (very rare for the SHA-based generator). Append a
        # suffix until we find a free EUI.
        for i in range(2, 1000):
            alt = generator(namespace, f"{name}#{i}")
            if alt not in taken:
                return alt
        raise RuntimeError("could not allocate a unique EUI after 1000 tries")

    # -- helpers -----------------------------------------------------------

    def _default_config_skeleton(self, project_id: str) -> dict[str, Any]:
        proj = self.get(project_id)
        building_name = proj.name if proj else project_id
        building_id = f"bld-{project_id}"
        timezone = proj.timezone if proj else "UTC"
        return {
            "building": {
                "id": building_id,
                "name": building_name,
                "timezone": timezone,
                "building_type": (proj.building_type if proj else "office"),
                "zones": [
                    {
                        "id": "zone-default",
                        "name": "Default Zone",
                        "area_m2": 100.0,
                        "capacity": 20,
                        "metadata": {"type": "open_office"},
                    }
                ],
            },
            "devices": [],
            "outputs": {
                "mqtt": {"enabled": False},
                "csv": {"enabled": False},
            },
            "simulation": {
                "mode": "live",
                "interval_seconds": 60,
                "seed": 42,
            },
        }

    @staticmethod
    def _normalize_zone(zone: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": str(zone["id"]).strip(),
            "name": str(zone.get("name", zone["id"])).strip(),
        }
        if zone.get("area_m2") is not None:
            out["area_m2"] = float(zone["area_m2"])
        if zone.get("capacity") is not None:
            out["capacity"] = int(zone["capacity"])
        for key in (
            "room_type",
            "floor_id",
            "exposure",
            "ventilation_quality",
            "infiltration_level",
            "hvac_zone_id",
            "monitoring_profile",
            "monitoring_intent",
            "monitoring_richness",
        ):
            val = zone.get(key)
            if val in (None, ""):
                continue
            out[key] = str(val).strip()
        if zone.get("metadata"):
            out["metadata"] = dict(zone["metadata"])
        return out

    @staticmethod
    def _normalize_hvac_zone(zone: dict[str, Any]) -> dict[str, Any]:
        hz_id = str(zone.get("id", "")).strip()
        if not hz_id:
            raise ValueError("hvac_zone.id is required")
        out: dict[str, Any] = {
            "id": hz_id,
            "name": str(zone.get("name", hz_id)).strip(),
        }
        for key in ("system_type", "system_id"):
            val = zone.get(key)
            if val in (None, ""):
                continue
            out[key] = str(val).strip()
        for key in ("setpoint_c", "capacity_kw"):
            val = zone.get(key)
            if val in (None, ""):
                continue
            try:
                out[key] = float(val)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"hvac_zone.{key} must be a number") from exc
        if zone.get("metadata"):
            out["metadata"] = dict(zone["metadata"])
        return out

    @staticmethod
    def _normalize_device(device: dict[str, Any]) -> dict[str, Any]:
        eui = str(device["device_eui"]).strip().lower()
        out: dict[str, Any] = {
            "device_eui": eui,
            "name": str(device.get("name", eui)).strip(),
            "type": str(device["type"]).strip(),
            "zone_id": str(device["zone_id"]).strip(),
        }
        meta = device.get("metadata") or {}
        if not isinstance(meta, dict):
            raise ValueError("device.metadata must be a mapping")
        cleaned: dict[str, Any] = {}
        for k, v in meta.items():
            if v in ("", None):
                continue
            cleaned[str(k)] = v
        if cleaned:
            out["metadata"] = cleaned
        return out

    # -- internal ----------------------------------------------------------

    def _load_path(self, path: Path) -> Project:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return Project(**data)

    def _save(self, project: Project) -> None:
        path = self._path(project.id)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(project.to_dict(), fh, indent=2, sort_keys=True)


# Module-level singleton — routes import this directly.
project_service = ProjectService()
