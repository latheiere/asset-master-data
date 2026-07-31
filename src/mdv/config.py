from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from mdv.lifecycle import SourceLifecycle, SourceLifecyclePhase
from mdv.normalization import STATUS_VALUES, normalize_status


def _xdg_path(environment_name: str, fallback: str) -> Path:
    root = Path(os.environ.get(environment_name, Path.home() / fallback)).expanduser()
    return root / "asset-master-data"


LEGACY_CONFIG_PATH = Path("config/config.yaml")
XDG_CONFIG_DIR = _xdg_path("XDG_CONFIG_HOME", ".config")
XDG_DATA_DIR = _xdg_path("XDG_DATA_HOME", ".local/share")
DEFAULT_CONFIG_PATH = (
    Path(os.environ["MDV_CONFIG_PATH"]).expanduser()
    if os.environ.get("MDV_CONFIG_PATH")
    else LEGACY_CONFIG_PATH if LEGACY_CONFIG_PATH.exists() else XDG_CONFIG_DIR / "config.yaml"
)


@dataclass(frozen=True)
class Settings:
    db_path: Path
    host: str
    port: int
    http_timeout_seconds: float
    collection_schedule: str
    entitlements_path: Path
    session_cookie_name: str
    session_ttl_seconds: int
    session_cookie_secure: bool
    token_info_url: str = "http://127.0.0.1:8091"
    max_concurrent_fetches: int = 2
    collection_stale_after_seconds: int = 7200
    collection_readiness_max_age_seconds: int = 0
    unchanged_observation_retention_days: int = 30
    changed_payload_retention_days: int = 7
    max_retained_observations_per_table: int = 100_000
    auth_max_concurrent_hashes: int = 2
    auth_failed_attempt_limit: int = 10
    auth_failed_attempt_window_seconds: int = 60
    source_lifecycles: tuple[SourceLifecycle, ...] = ()

    @classmethod
    def from_yaml(cls, path: str | Path = DEFAULT_CONFIG_PATH) -> "Settings":
        config_path = Path(path).expanduser()
        try:
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError as exc:
            raise ValueError(f"configuration file does not exist: {config_path}") from exc
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid YAML in {config_path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"configuration root must be a mapping: {config_path}")

        database = _mapping(payload, "database")
        server = _mapping(payload, "server")
        collection = _mapping(payload, "collection")
        audit = _mapping(payload, "audit")
        auth = _mapping(payload, "auth")
        integration = _mapping(payload, "integration")

        settings = cls(
            db_path=Path(
                str(database.get("path", ".local/state/mdv.sqlite3"))
            ).expanduser(),
            host=str(server.get("host", "127.0.0.1")),
            port=_bounded_positive_int(
                server.get("port", 8090), "server.port", maximum=65_535
            ),
            http_timeout_seconds=_bounded_positive_float(
                collection.get("http_timeout_seconds", 20),
                "collection.http_timeout_seconds",
                maximum=300,
            ),
            collection_schedule=str(
                collection.get("schedule", "*-*-* 00:40:00 UTC")
            ).strip(),
            max_concurrent_fetches=_bounded_positive_int(
                collection.get("max_concurrent_fetches", 2),
                "collection.max_concurrent_fetches",
                maximum=32,
            ),
            collection_stale_after_seconds=_positive_int(
                collection.get("stale_after_seconds", 7200),
                "collection.stale_after_seconds",
            ),
            collection_readiness_max_age_seconds=_nonnegative_int(
                collection.get("readiness_max_age_seconds", 0),
                "collection.readiness_max_age_seconds",
            ),
            source_lifecycles=_source_lifecycles(
                collection.get("source_lifecycles", {})
            ),
            unchanged_observation_retention_days=_nonnegative_int(
                audit.get("unchanged_observation_retention_days", 30),
                "audit.unchanged_observation_retention_days",
            ),
            changed_payload_retention_days=_nonnegative_int(
                audit.get("changed_payload_retention_days", 7),
                "audit.changed_payload_retention_days",
            ),
            max_retained_observations_per_table=_bounded_nonnegative_int(
                audit.get("max_retained_observations_per_table", 100_000),
                "audit.max_retained_observations_per_table",
                maximum=1_000_000,
            ),
            entitlements_path=Path(
                str(auth.get("entitlements_path", "config/entitlements.yaml"))
            ).expanduser(),
            session_cookie_name=str(auth.get("session_cookie_name", "mdv_session")).strip(),
            session_ttl_seconds=_positive_int(
                auth.get("session_ttl_seconds", 43200),
                "auth.session_ttl_seconds",
            ),
            session_cookie_secure=_boolean(
                auth.get("session_cookie_secure", False),
                "auth.session_cookie_secure",
            ),
            auth_max_concurrent_hashes=_bounded_positive_int(
                auth.get("max_concurrent_hashes", 2),
                "auth.max_concurrent_hashes",
                maximum=8,
            ),
            auth_failed_attempt_limit=_bounded_positive_int(
                auth.get("failed_attempt_limit", 10),
                "auth.failed_attempt_limit",
                maximum=100,
            ),
            auth_failed_attempt_window_seconds=_bounded_positive_int(
                auth.get("failed_attempt_window_seconds", 60),
                "auth.failed_attempt_window_seconds",
                maximum=3600,
            ),
            token_info_url=_http_url(
                integration.get("token_info_url", "http://127.0.0.1:8091"),
                "integration.token_info_url",
            ),
        )
        if not settings.collection_schedule:
            raise ValueError("collection.schedule must not be empty")
        if not settings.session_cookie_name:
            raise ValueError("auth.session_cookie_name must not be empty")
        return settings


def load_config_value(path: str | Path, dotted_key: str) -> Any:
    config_path = Path(path).expanduser()
    try:
        value: Any = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read configuration {config_path}: {exc}") from exc
    for key in dotted_key.split("."):
        if not isinstance(value, dict) or key not in value:
            raise ValueError(f"configuration key does not exist: {dotted_key}")
        value = value[key]
    if isinstance(value, (dict, list)):
        raise ValueError(f"configuration key must be scalar: {dotted_key}")
    return value


def _mapping(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be a mapping")
    return value


def _source_lifecycles(value: Any) -> tuple[SourceLifecycle, ...]:
    name = "collection.source_lifecycles"
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    lifecycles = []
    for raw_source, raw_phases in value.items():
        source = str(raw_source or "").strip().upper()
        source_name = f"{name}.{source or '<empty>'}"
        if not source:
            raise ValueError(f"{name} source names must not be empty")
        if not isinstance(raw_phases, list) or not raw_phases:
            raise ValueError(f"{source_name} must be a non-empty list")
        phases = []
        previous_at = None
        for index, raw_phase in enumerate(raw_phases):
            phase_name = f"{source_name}[{index}]"
            if not isinstance(raw_phase, dict):
                raise ValueError(f"{phase_name} must be a mapping")
            if "effective_at" not in raw_phase:
                raise ValueError(f"{phase_name}.effective_at is required")
            effective_at = _timezone_datetime(
                raw_phase["effective_at"],
                f"{phase_name}.effective_at",
            )
            if previous_at is not None and effective_at <= previous_at:
                raise ValueError(
                    f"{source_name} phases must have increasing effective_at values"
                )
            if "collect" not in raw_phase:
                raise ValueError(f"{phase_name}.collect is required")
            collect = _boolean(raw_phase["collect"], f"{phase_name}.collect")
            raw_status = raw_phase.get("status")
            status = None
            if raw_status not in (None, ""):
                status = normalize_status(raw_status)
                if status == "UNKNOWN" and str(raw_status).strip().upper() != "UNKNOWN":
                    raise ValueError(
                        f"{phase_name}.status must be one of: {', '.join(STATUS_VALUES)}"
                    )
            if not collect and status not in {"CLOSED", "MISSING"}:
                raise ValueError(
                    f"{phase_name}.status must be terminal when collect is false"
                )
            phases.append(SourceLifecyclePhase(effective_at, collect, status))
            previous_at = effective_at
        lifecycles.append(SourceLifecycle(source, tuple(phases)))
    return tuple(lifecycles)


def _timezone_datetime(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _positive_int(value: Any, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _positive_float(value: Any, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _bounded_positive_int(value: Any, name: str, *, maximum: int) -> int:
    parsed = _positive_int(value, name)
    if parsed > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return parsed


def _bounded_positive_float(value: Any, name: str, *, maximum: float) -> float:
    parsed = _positive_float(value, name)
    if parsed > maximum:
        raise ValueError(f"{name} must be at most {maximum:g}")
    return parsed


def _nonnegative_int(value: Any, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < 0:
        raise ValueError(f"{name} must not be negative")
    return parsed


def _bounded_nonnegative_int(value: Any, name: str, *, maximum: int) -> int:
    parsed = _nonnegative_int(value, name)
    if parsed > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return parsed


def _boolean(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "yes", "1"}:
        return True
    if normalized in {"false", "no", "0"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _http_url(value: Any, name: str) -> str:
    normalized = str(value).strip().rstrip("/")
    if not normalized.startswith(("http://", "https://")):
        raise ValueError(f"{name} must be an http or https URL")
    return normalized
