#!/usr/bin/env python3
"""Create and verify portable runtime-state archives.

SQLite inputs are copied through SQLite's online backup API. Other paths are
copied as ordinary files. Archives are intentionally unencrypted and mode 0600;
encrypt them before transferring them off host.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path


MANIFEST = "manifest.json"


def _safe_name(path: Path, used: set[str]) -> str:
    base = path.name or "state"
    candidate = base
    counter = 2
    while candidate in used:
        candidate = f"{base}-{counter}"
        counter += 1
    used.add(candidate)
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
    destination_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(destination_conn)
    finally:
        destination_conn.close()
        source_conn.close()


def _copy_path(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def create_archive(output: Path, sqlite_paths: list[Path], paths: list[Path]) -> dict:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="runtime-backup-") as temporary:
        staging = Path(temporary) / "state"
        staging.mkdir()
        sqlite_names: set[str] = set()
        missing: list[str] = []

        for source in sqlite_paths:
            source = source.expanduser()
            if not source.exists():
                missing.append(str(source))
                continue
            name = _safe_name(source, used)
            _sqlite_backup(source, staging / name)
            sqlite_names.add(name)

        for source in paths:
            source = source.expanduser()
            if not source.exists():
                missing.append(str(source))
                continue
            name = _safe_name(source, used)
            _copy_path(source, staging / name)

        entries = []
        for file_path in _files(staging):
            relative = file_path.relative_to(staging).as_posix()
            entries.append(
                {
                    "path": relative,
                    "sha256": _sha256(file_path),
                    "size": file_path.stat().st_size,
                    "sqlite": relative in sqlite_names,
                }
            )
        if not entries:
            raise ValueError("no backup inputs exist")
        manifest = {
            "format": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "entries": entries,
            "missing_optional_inputs": missing,
        }
        (staging / MANIFEST).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary_output = output.with_suffix(output.suffix + ".tmp")
        with tarfile.open(temporary_output, "w:gz") as archive:
            archive.add(staging, arcname="state")
        os.chmod(temporary_output, 0o600)
        temporary_output.replace(output)
    return {"ok": True, "archive": str(output), **manifest}


def _safe_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    for member in members:
        path = Path(member.name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not (member.isfile() or member.isdir())
        ):
            raise ValueError(f"unsafe archive member: {member.name}")
    return members


def verify_archive(path: Path) -> dict:
    path = path.expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="runtime-restore-check-") as temporary:
        root = Path(temporary)
        with tarfile.open(path, "r:gz") as archive:
            archive.extractall(root, members=_safe_members(archive))
        state = root / "state"
        manifest = json.loads((state / MANIFEST).read_text(encoding="utf-8"))
        entries = manifest.get("entries")
        if not isinstance(entries, list) or not entries:
            raise ValueError("backup manifest has no entries")
        checked = 0
        for entry in entries:
            relative = Path(entry["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"unsafe manifest path: {entry['path']}")
            candidate = state / relative
            if not candidate.is_file() or _sha256(candidate) != entry["sha256"]:
                raise ValueError(f"backup checksum mismatch: {entry['path']}")
            if entry.get("sqlite"):
                with sqlite3.connect(f"file:{candidate.resolve()}?mode=ro", uri=True) as conn:
                    result = conn.execute("PRAGMA quick_check").fetchone()
                if not result or result[0] != "ok":
                    raise ValueError(f"SQLite integrity check failed: {entry['path']}")
            checked += 1
    return {"ok": True, "archive": str(path), "entries_checked": checked}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create or verify a runtime-state backup")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--sqlite", type=Path, action="append", default=[])
    create.add_argument("--path", type=Path, action="append", default=[])
    verify = subparsers.add_parser("verify")
    verify.add_argument("archive", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = (
        create_archive(args.output, args.sqlite, args.path)
        if args.command == "create"
        else verify_archive(args.archive)
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
