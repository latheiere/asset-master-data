from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG_PATH = Path("config/config.yaml")


@dataclass(frozen=True)
class Settings:
    db_path: Path
    host: str
    port: int
    refresh_on_startup: str
    http_timeout_seconds: float
    collection_schedule: str
    entitlements_path: Path
    session_cookie_name: str
    session_ttl_seconds: int
    session_cookie_secure: bool
    token_info_url: str = "http://127.0.0.1:8091"

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
        auth = _mapping(payload, "auth")
        integration = _mapping(payload, "integration")
        refresh = str(server.get("refresh_on_startup", "if-empty")).strip().lower()
        if refresh not in {"always", "if-empty", "never"}:
            raise ValueError("server.refresh_on_startup must be always, if-empty, or never")

        settings = cls(
            db_path=Path(str(database.get("path", ".data/mdv.sqlite3"))).expanduser(),
            host=str(server.get("host", "127.0.0.1")),
            port=_positive_int(server.get("port", 8090), "server.port"),
            refresh_on_startup=refresh,
            http_timeout_seconds=_positive_float(
                collection.get("http_timeout_seconds", 20),
                "collection.http_timeout_seconds",
            ),
            collection_schedule=str(
                collection.get("schedule", "*-*-* 00:00:00 UTC")
            ).strip(),
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
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
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
