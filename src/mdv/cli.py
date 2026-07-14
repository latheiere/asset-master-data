from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import tempfile
from pathlib import Path

import yaml

from mdv import __version__, build_revision
from mdv.auth import Entitlements, hash_password
from mdv.bundles import (
    apply_collection_bundle,
    bundle_succeeded,
    canonical_json,
    export_collection_bundle,
)
from mdv.collection import CollectionService, collection_json
from mdv.connectors import supported_venues
from mdv.config import (
    DEFAULT_CONFIG_PATH,
    XDG_CONFIG_DIR,
    XDG_DATA_DIR,
    Settings,
    load_config_value,
)
from mdv.db import SQLiteStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mdv", description="Asset master-data service")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"YAML configuration path (default: {DEFAULT_CONFIG_PATH})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_config = subparsers.add_parser(
        "init-config", help="create a standalone XDG-compatible configuration"
    )
    init_config.add_argument("--force", action="store_true")
    subparsers.add_parser("init", help="initialize or migrate the database")
    collect = subparsers.add_parser("collect", help="collect exchange universes")
    collect.add_argument(
        "--venue",
        help=f"collect one venue: {', '.join(supported_venues())}",
    )
    collect.add_argument(
        "--exclude-venue",
        action="append",
        default=[],
        help="exclude one venue from an all-venue refresh; may be repeated",
    )
    bundle_export = subparsers.add_parser(
        "bundle-export", help="fetch one venue and write a portable collection bundle"
    )
    bundle_export.add_argument("--venue", required=True)
    bundle_export.add_argument("--output", required=True, help="output file or - for stdout")
    bundle_import = subparsers.add_parser(
        "bundle-import", help="validate and apply a portable collection bundle"
    )
    bundle_import.add_argument("path", help="bundle file path")
    subparsers.add_parser("stats", help="print collection statistics")
    compact = subparsers.add_parser(
        "compact", help="prune expired unchanged observation rows"
    )
    compact.add_argument("--retention-days", type=int)
    doctor = subparsers.add_parser("doctor", help="validate runtime configuration and state")
    doctor.add_argument("--require-ready", action="store_true")
    serve = subparsers.add_parser("serve", help="serve HTML and JSON API")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    config_value = subparsers.add_parser("config-value", help="print one scalar YAML setting")
    config_value.add_argument("key", help="dotted setting name, for example collection.schedule")
    entitlement = subparsers.add_parser("entitlement", help="create or update an API user")
    entitlement.add_argument("username")
    entitlement.add_argument(
        "--password-file",
        required=True,
        help="file containing the password; the first trailing newline is removed",
    )
    entitlement.add_argument(
        "--role", choices=("reader", "operator"), default="operator"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init-config":
        _write_default_config(Path(args.config), force=args.force)
        print(json.dumps({"ok": True, "config": str(Path(args.config).expanduser())}))
        return 0
    if args.command == "config-value":
        print(load_config_value(args.config, args.key))
        return 0
    settings = Settings.from_yaml(args.config)
    if args.command == "entitlement":
        password = Path(args.password_file).read_text(encoding="utf-8").rstrip("\r\n")
        if not args.username.strip() or not password:
            raise ValueError("username and password must not be empty")
        _set_entitlement(
            settings.entitlements_path, args.username, password, role=args.role
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "username": args.username,
                    "role": args.role,
                    "path": str(settings.entitlements_path),
                }
            )
        )
        return 0
    if args.command == "bundle-export":
        try:
            bundle = asyncio.run(
                export_collection_bundle(
                    venue=args.venue,
                    timeout_seconds=settings.http_timeout_seconds,
                    max_concurrent_fetches=settings.max_concurrent_fetches,
                )
            )
        except ValueError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
            return 2
        encoded = canonical_json(bundle) + "\n"
        if args.output == "-":
            print(encoded, end="")
        else:
            output = Path(args.output).expanduser()
            _atomic_write_private(output, encoded)
        return 0 if bundle_succeeded(bundle) else 1
    store = SQLiteStore(settings.db_path)
    if args.command == "bundle-import":
        try:
            payload = json.loads(Path(args.path).read_text(encoding="utf-8"))
            store.reconcile_stale_collection_runs(
                stale_after_seconds=settings.collection_stale_after_seconds
            )
            results = apply_collection_bundle(store, payload)
            store.compact_audit_history(
                unchanged_retention_days=settings.unchanged_observation_retention_days,
                changed_payload_retention_days=settings.changed_payload_retention_days,
                max_retained_observations_per_table=(
                    settings.max_retained_observations_per_table
                ),
            )
        except (OSError, ValueError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
            return 2
        print(
            json.dumps(
                collection_json(results, scope=str(payload.get("scope") or "BUNDLE")),
                indent=2,
            )
        )
        return 0 if all(result.ok for result in results) else 1
    if args.command == "init":
        store.migrate()
        print(json.dumps({"ok": True, "database": str(store.path)}))
        return 0
    if args.command == "doctor":
        try:
            Entitlements.load(settings.entitlements_path)
            entitlement_mode = settings.entitlements_path.stat().st_mode & 0o777
            store.reconcile_stale_collection_runs(
                stale_after_seconds=settings.collection_stale_after_seconds
            )
            readiness = store.readiness(
                max_collection_age_seconds=settings.collection_readiness_max_age_seconds
            )
            result = {
                "ok": bool(readiness["ready"]) and entitlement_mode & 0o077 == 0,
                "service": "asset-master-data",
                "version": __version__,
                "revision": build_revision(),
                "config": str(Path(args.config).expanduser()),
                "database": str(settings.db_path),
                "entitlements": str(settings.entitlements_path),
                "entitlements_mode": f"{entitlement_mode:04o}",
                "entitlements_private": entitlement_mode & 0o077 == 0,
                "readiness": readiness,
            }
        except (OSError, RuntimeError, ValueError) as exc:
            result = {"ok": False, "error": str(exc)}
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] or not args.require_ready else 1
    if args.command == "compact":
        retention_days = (
            settings.unchanged_observation_retention_days
            if args.retention_days is None
            else args.retention_days
        )
        if retention_days < 0:
            raise ValueError("--retention-days must not be negative")
        print(
            json.dumps(
                store.compact_audit_history(
                    unchanged_retention_days=retention_days,
                    changed_payload_retention_days=(
                        settings.changed_payload_retention_days
                    ),
                    max_retained_observations_per_table=(
                        settings.max_retained_observations_per_table
                    ),
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "collect":
        try:
            results = asyncio.run(
                CollectionService(
                    store,
                    timeout_seconds=settings.http_timeout_seconds,
                    max_concurrent_fetches=settings.max_concurrent_fetches,
                    stale_after_seconds=settings.collection_stale_after_seconds,
                    unchanged_observation_retention_days=(
                        settings.unchanged_observation_retention_days
                    ),
                    changed_payload_retention_days=(
                        settings.changed_payload_retention_days
                    ),
                    max_retained_observations_per_table=(
                        settings.max_retained_observations_per_table
                    ),
                ).collect(
                    venue=args.venue,
                    exclude_venues=args.exclude_venue,
                )
            )
        except ValueError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
            return 2
        print(
            json.dumps(
                collection_json(
                    results,
                    scope=(
                        str(args.venue).strip().upper()
                        if args.venue
                        else (
                            "ALL_EXCEPT_"
                            + "_".join(
                                sorted({item.strip().upper() for item in args.exclude_venue})
                            )
                            if args.exclude_venue
                            else "ALL"
                        )
                    ),
                ),
                indent=2,
            )
        )
        return 0 if all(result.ok for result in results) else 1
    if args.command == "stats":
        print(json.dumps(store.stats(), indent=2))
        return 0
    if args.command == "serve":
        import uvicorn
        from mdv.web import create_app

        uvicorn.run(
            create_app(settings=settings),
            host=args.host or settings.host,
            port=args.port or settings.port,
            reload=False,
        )
        return 0
    raise AssertionError(f"unknown command: {args.command}")


def _set_entitlement(
    path: Path, username: str, password: str, *, role: str = "operator"
) -> None:
    payload: dict[str, object]
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"entitlements root must be a mapping: {path}")
        payload = loaded
    else:
        payload = {"session_secret": secrets.token_urlsafe(48), "users": {}}
    users = payload.setdefault("users", {})
    if not isinstance(users, dict):
        raise ValueError(f"entitlements users must be a mapping: {path}")
    users[username] = {"password_hash": hash_password(password), "role": role}
    _atomic_write_private(path, yaml.safe_dump(payload, sort_keys=False))


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_private(
    path: Path, contents: str, *, replace: bool = True
) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError(f"output must not be a symlink: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    promoted = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        if path.is_symlink():
            raise ValueError(f"output must not be a symlink: {path}")
        if replace:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise ValueError(f"output already exists: {path}") from exc
            temporary.unlink()
        promoted = True
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()
        if not promoted:
            _fsync_directory(path.parent)


def _write_default_config(path: Path, *, force: bool) -> None:
    path = path.expanduser()
    if path.is_symlink():
        raise ValueError(f"output must not be a symlink: {path}")
    if path.exists() and not force:
        raise ValueError(f"configuration already exists: {path}; pass --force to replace")
    payload = {
        "database": {"path": str(XDG_DATA_DIR / "mdv.sqlite3")},
        "server": {"host": "127.0.0.1", "port": 8090},
        "collection": {
            "http_timeout_seconds": 20,
            "max_concurrent_fetches": 2,
            "stale_after_seconds": 7200,
            "readiness_max_age_seconds": 129600,
            "schedule": "*-*-* 00:40:00 UTC",
        },
        "audit": {
            "unchanged_observation_retention_days": 30,
            "changed_payload_retention_days": 7,
            "max_retained_observations_per_table": 100000,
        },
        "auth": {
            "entitlements_path": str(XDG_CONFIG_DIR / "entitlements.yaml"),
            "session_cookie_name": "mdv_session",
            "session_ttl_seconds": 43200,
            "session_cookie_secure": False,
            "max_concurrent_hashes": 2,
            "failed_attempt_limit": 10,
            "failed_attempt_window_seconds": 60,
        },
        "integration": {"token_info_url": "http://127.0.0.1:8091"},
    }
    _atomic_write_private(
        path,
        yaml.safe_dump(payload, sort_keys=False),
        replace=force,
    )


if __name__ == "__main__":
    raise SystemExit(main())
