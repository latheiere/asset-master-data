from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
from pathlib import Path

import yaml

from mdv import __version__
from mdv.auth import hash_password
from mdv.collection import CollectionService, collection_json, results_json
from mdv.connectors import supported_venues
from mdv.config import DEFAULT_CONFIG_PATH, Settings, load_config_value
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
    subparsers.add_parser("init", help="initialize or migrate the database")
    collect = subparsers.add_parser("collect", help="refresh exchange universes")
    collect.add_argument(
        "--venue",
        help=f"refresh one venue: {', '.join(supported_venues())}",
    )
    subparsers.add_parser("stats", help="print collection statistics")
    serve = subparsers.add_parser("serve", help="serve HTML and JSON API")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--refresh", action="store_true", help="collect before starting")
    config_value = subparsers.add_parser("config-value", help="print one scalar YAML setting")
    config_value.add_argument("key", help="dotted setting name, for example collection.schedule")
    entitlement = subparsers.add_parser("entitlement", help="create or update an API user")
    entitlement.add_argument("username")
    entitlement.add_argument(
        "--password-file",
        required=True,
        help="file containing the password; the first trailing newline is removed",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "config-value":
        print(load_config_value(args.config, args.key))
        return 0
    settings = Settings.from_yaml(args.config)
    if args.command == "entitlement":
        password = Path(args.password_file).read_text(encoding="utf-8").rstrip("\r\n")
        if not args.username.strip() or not password:
            raise ValueError("username and password must not be empty")
        _set_entitlement(settings.entitlements_path, args.username, password)
        print(json.dumps({"ok": True, "username": args.username, "path": str(settings.entitlements_path)}))
        return 0
    store = SQLiteStore(settings.db_path)
    if args.command == "init":
        store.migrate()
        print(json.dumps({"ok": True, "database": str(store.path)}))
        return 0
    if args.command == "collect":
        try:
            results = asyncio.run(
                CollectionService(store, timeout_seconds=settings.http_timeout_seconds).collect(
                    venue=args.venue,
                )
            )
        except ValueError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
            return 2
        print(
            json.dumps(
                collection_json(results, scope=str(args.venue or "ALL").strip().upper()),
                indent=2,
            )
        )
        return 0 if all(result.ok for result in results) else 1
    if args.command == "stats":
        print(json.dumps(store.stats(), indent=2))
        return 0
    if args.command == "serve":
        if args.refresh:
            results = asyncio.run(CollectionService(store, timeout_seconds=settings.http_timeout_seconds).collect_all())
            if not all(result.ok for result in results):
                print(json.dumps(results_json(results), indent=2))
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


def _set_entitlement(path: Path, username: str, password: str) -> None:
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
    users[username] = {"password_hash": hash_password(password)}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)


if __name__ == "__main__":
    raise SystemExit(main())
