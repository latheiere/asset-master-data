#!/usr/bin/env python3
"""Create, verify, and restore runtime-state archives.

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
MAX_ARCHIVE_MEMBERS = 10_000
MAX_EXTRACTED_BYTES = 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MIN_FREE_BYTES_AFTER_EXTRACT = 128 * 1024 * 1024


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
    os.chmod(destination, 0o600)


def _reject_symlinks(source: Path) -> None:
    if source.is_symlink():
        raise ValueError(f"backup input must not be a symlink: {source}")
    if source.is_dir():
        for candidate in source.rglob("*"):
            if candidate.is_symlink():
                raise ValueError(
                    f"backup input contains a symlink: {candidate}"
                )
    elif not source.is_file():
        raise ValueError(f"backup input must be a regular file or directory: {source}")


def _copy_path(source: Path, destination: Path) -> None:
    _reject_symlinks(source)
    if source.is_dir():
        shutil.copytree(source, destination)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safety_link(source: Path, destination: Path) -> None:
    """Preserve a same-filesystem name without an unbudgeted data copy."""
    if os.path.lexists(destination):
        raise FileExistsError(f"restore safety path already exists: {destination}")
    os.link(source, destination)


def _files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def _portable_restore_path(source: Path, fallback: str) -> Path:
    expanded = source.expanduser()
    if expanded.is_absolute():
        # Manifest paths remain relative and safe. Restoring an archive created
        # from absolute paths with ``--target-root /`` reproduces those exact
        # configured locations.
        return Path(*expanded.parts[1:])
    if ".." in expanded.parts:
        return Path(fallback)
    return expanded


def create_archive(
    output: Path,
    sqlite_paths: list[Path],
    paths: list[Path],
    *,
    evidence_paths: list[Path] | None = None,
    metadata: dict[str, str] | None = None,
) -> dict:
    requested_output = output.expanduser().absolute()
    if requested_output.is_symlink():
        raise ValueError(f"backup output must not be a symlink: {requested_output}")
    output = requested_output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    expanded_sqlite = [source.expanduser() for source in sqlite_paths]
    expanded_paths = [source.expanduser() for source in paths]
    expanded_evidence = [
        source.expanduser() for source in (evidence_paths or [])
    ]
    all_sources = [*expanded_sqlite, *expanded_paths, *expanded_evidence]
    missing = [str(source) for source in all_sources if not source.exists()]
    if missing:
        raise ValueError(
            "missing required backup input(s): " + ", ".join(sorted(missing))
        )
    for source in all_sources:
        _reject_symlinks(source)
    resolved_sources: set[Path] = set()
    for source in all_sources:
        resolved = source.resolve()
        if resolved in resolved_sources:
            raise ValueError(f"duplicate backup input: {source}")
        resolved_sources.add(resolved)
        if output == resolved or (source.is_dir() and output.is_relative_to(resolved)):
            raise ValueError(f"backup output overlaps input: {source}")
    for source in expanded_sqlite:
        if not source.is_file():
            raise ValueError(f"SQLite backup input must be a file: {source}")
    manifest_metadata = dict(sorted((metadata or {}).items()))
    if not all(
        isinstance(key, str)
        and isinstance(value, str)
        and 0 < len(key) <= 128
        and len(value) <= 4096
        for key, value in manifest_metadata.items()
    ):
        raise ValueError("backup metadata must map bounded strings to strings")
    used: set[str] = {MANIFEST}
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    temporary_output = Path(temporary_name)
    try:
        with tempfile.TemporaryDirectory(prefix="runtime-backup-") as temporary:
            staging = Path(temporary) / "state"
            staging.mkdir()
            sqlite_names: set[str] = set()
            restore_paths: dict[str, str] = {}
            evidence_names: set[str] = set()

            for source in expanded_sqlite:
                name = _safe_name(source, used)
                _sqlite_backup(source, staging / name)
                sqlite_names.add(name)
                restore_paths[name] = _portable_restore_path(source, name).as_posix()

            for source in expanded_paths:
                name = _safe_name(source, used)
                destination = staging / name
                _copy_path(source, destination)
                logical_root = _portable_restore_path(source, name)
                if source.is_dir():
                    for copied in _files(destination):
                        restore_paths[copied.relative_to(staging).as_posix()] = (
                            logical_root / copied.relative_to(destination)
                        ).as_posix()
                else:
                    restore_paths[name] = logical_root.as_posix()

            for source in expanded_evidence:
                name = _safe_name(source, used)
                destination = staging / name
                _copy_path(source, destination)
                if source.is_dir():
                    evidence_names.update(
                        copied.relative_to(staging).as_posix()
                        for copied in _files(destination)
                    )
                else:
                    evidence_names.add(name)

            entries = []
            for file_path in _files(staging):
                relative = file_path.relative_to(staging).as_posix()
                evidence = relative in evidence_names
                entry = {
                    "path": relative,
                    "sha256": _sha256(file_path),
                    "size": file_path.stat().st_size,
                    "mode": file_path.stat().st_mode & 0o777,
                    "sqlite": relative in sqlite_names,
                    "evidence": evidence,
                }
                if not evidence:
                    entry["restore_path"] = restore_paths[relative]
                entries.append(entry)
            if not entries:
                raise ValueError("no backup inputs exist")
            manifest = {
                "format": 2,
                "created_at": datetime.now(UTC).isoformat(),
                "entries": entries,
                "missing_optional_inputs": [],
                "metadata": manifest_metadata,
            }
            (staging / MANIFEST).write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with tarfile.open(temporary_output, "w:gz") as archive:
                archive.add(staging, arcname="state")
            os.chmod(temporary_output, 0o600)
            _fsync_file(temporary_output)
        # Release the potentially large SQLite staging copy before extracting
        # the archive for self-verification on limited-disk hosts.
        verify_archive(temporary_output)
        temporary_output.replace(output)
        _fsync_directory(output.parent)
    finally:
        if temporary_output.exists():
            temporary_output.unlink()
    return {"ok": True, "archive": str(output), **manifest}


def _safe_members(
    archive: tarfile.TarFile,
) -> tuple[list[tarfile.TarInfo], int]:
    members: list[tarfile.TarInfo] = []
    names: set[str] = set()
    extracted_bytes = 0
    for member_count, member in enumerate(archive, start=1):
        if member_count > MAX_ARCHIVE_MEMBERS:
            raise ValueError(
                "backup archive exceeds member limit: "
                f"{member_count} > {MAX_ARCHIVE_MEMBERS}"
            )
        path = Path(member.name)
        normalized = path.as_posix()
        if (
            path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or path.parts[0] != "state"
            or not (member.isfile() or member.isdir())
        ):
            raise ValueError(f"unsafe archive member: {member.name}")
        if normalized in names:
            raise ValueError(f"duplicate archive member: {member.name}")
        names.add(normalized)
        if len(normalized) > 4096:
            raise ValueError(f"backup archive member name is too long: {member.name[:80]}")
        if member.isfile():
            if normalized == f"state/{MANIFEST}" and member.size > MAX_MANIFEST_BYTES:
                raise ValueError("backup manifest exceeds size limit")
            extracted_bytes += member.size
            if extracted_bytes > MAX_EXTRACTED_BYTES:
                raise ValueError(
                    "backup archive exceeds expanded byte limit: "
                    f"{extracted_bytes} > {MAX_EXTRACTED_BYTES}"
                )
        members.append(member)
    return members, extracted_bytes


def _extract_archive(path: Path, root: Path) -> tuple[Path, dict, int]:
    root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "r:gz") as archive:
        members, extracted_bytes = _safe_members(archive)
        free_bytes = shutil.disk_usage(root).free
        required_bytes = extracted_bytes + MIN_FREE_BYTES_AFTER_EXTRACT
        if free_bytes < required_bytes:
            raise ValueError(
                "insufficient free space to extract backup: "
                f"{free_bytes} available, {required_bytes} required"
            )
        archive.extractall(root, members=members, filter="data")
    state = root / "state"
    manifest, checked = _verify_extracted_state(state)
    return state, manifest, checked


def _verify_extracted_state(state: Path) -> tuple[dict, int]:
    manifest_path = state / MANIFEST
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("backup archive has no regular manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") not in {1, 2}:
        raise ValueError("unsupported backup manifest format")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("backup manifest has no entries")
    metadata = manifest.get("metadata", {})
    if not isinstance(metadata, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in metadata.items()
    ):
        raise ValueError("backup manifest metadata must map strings to strings")
    checked = 0
    expected_paths: set[str] = set()
    restore_paths: set[str] = set()
    actual_paths = {
        item.relative_to(state).as_posix()
        for item in _files(state)
        if item.relative_to(state).as_posix() != MANIFEST
    }
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("backup manifest entries must be mappings")
        relative = Path(entry.get("path", ""))
        if not str(relative) or relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe manifest path: {entry.get('path')}")
        if relative.as_posix() in expected_paths:
            raise ValueError(f"duplicate manifest path: {entry['path']}")
        expected_paths.add(relative.as_posix())
        evidence = bool(entry.get("evidence", False))
        if evidence and entry.get("sqlite"):
            raise ValueError(f"SQLite entry cannot be evidence-only: {entry['path']}")
        if manifest.get("format") == 2 and not evidence:
            restore_path = Path(entry.get("restore_path", ""))
            if (
                not str(restore_path)
                or restore_path.is_absolute()
                or ".." in restore_path.parts
            ):
                raise ValueError(f"unsafe restore path: {entry.get('restore_path')}")
            if restore_path.as_posix() in restore_paths:
                raise ValueError(f"duplicate restore path: {restore_path}")
            restore_paths.add(restore_path.as_posix())
        candidate = state / relative
        if (
            not candidate.is_file()
            or candidate.is_symlink()
            or candidate.stat().st_size != entry.get("size")
            or _sha256(candidate) != entry.get("sha256")
        ):
            raise ValueError(f"backup checksum mismatch: {entry.get('path')}")
        if entry.get("sqlite"):
            with sqlite3.connect(
                f"file:{candidate.resolve()}?mode=ro&immutable=1", uri=True
            ) as conn:
                result = conn.execute("PRAGMA quick_check").fetchone()
            if not result or result[0] != "ok":
                raise ValueError(f"SQLite integrity check failed: {entry['path']}")
        checked += 1
    if actual_paths != expected_paths:
        raise ValueError("backup archive contains unmanifested or missing files")
    return manifest, checked


def verify_archive(path: Path) -> dict:
    path = path.expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="runtime-restore-check-") as temporary:
        _, manifest, checked = _extract_archive(path, Path(temporary))
    return {
        "ok": True,
        "archive": str(path),
        "format": manifest["format"],
        "entries_checked": checked,
        "metadata": manifest.get("metadata", {}),
    }


def restore_archive(
    path: Path, *, target_root: Path, replace: bool = False
) -> dict:
    """Verify and atomically replace each logical runtime file under target_root."""
    path = path.expanduser().resolve()
    root = target_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="runtime-restore-") as temporary:
        extracted = Path(temporary) / "extracted"
        state, manifest, _ = _extract_archive(path, extracted)
        if manifest["format"] != 2:
            raise ValueError(
                "legacy format-1 backups can be verified but not auto-restored"
            )
        destinations: list[tuple[dict, Path, Path]] = []
        destination_paths: set[Path] = set()
        for entry in manifest["entries"]:
            if entry.get("evidence"):
                continue
            relative = Path(entry["restore_path"])
            requested_destination = root / relative
            destination = (
                requested_destination.parent.resolve()
                / requested_destination.name
            )
            if destination != root and root not in destination.parents:
                raise ValueError(f"restore target escapes target root: {relative}")
            if destination.is_symlink():
                raise ValueError(
                    f"restore target must not be a symlink: {destination}"
                )
            if destination in destination_paths:
                raise ValueError(f"duplicate restore destination: {destination}")
            destination_paths.add(destination)
            sqlite_sidecars = (
                [Path(f"{destination}{suffix}") for suffix in ("-wal", "-shm")]
                if entry.get("sqlite")
                else []
            )
            symlink_sidecars = [
                sidecar for sidecar in sqlite_sidecars if sidecar.is_symlink()
            ]
            if symlink_sidecars:
                raise ValueError(
                    "SQLite sidecar must not be a symlink: "
                    + ", ".join(str(sidecar) for sidecar in symlink_sidecars)
                )
            if destination.exists() and not replace:
                raise ValueError(
                    f"restore target exists: {destination}; pass --replace to overwrite"
                )
            stale_sidecars = [
                sidecar for sidecar in sqlite_sidecars if os.path.lexists(sidecar)
            ]
            if stale_sidecars and not replace:
                raise ValueError(
                    "restore target has SQLite sidecars: "
                    + ", ".join(str(sidecar) for sidecar in stale_sidecars)
                    + "; pass --replace to overwrite"
                )
            destinations.append((entry, state / entry["path"], destination))

        required_by_device: dict[int, int] = {}
        location_by_device: dict[int, Path] = {}
        for entry, _, destination in destinations:
            existing_parent = destination.parent
            while not existing_parent.exists():
                existing_parent = existing_parent.parent
            device = existing_parent.stat().st_dev
            required_by_device[device] = (
                required_by_device.get(device, 0) + int(entry["size"])
            )
            location_by_device[device] = existing_parent
        for device, required in required_by_device.items():
            available = shutil.disk_usage(location_by_device[device]).free
            required_with_reserve = required + MIN_FREE_BYTES_AFTER_EXTRACT
            if available < required_with_reserve:
                raise ValueError(
                    "insufficient free space to stage restore targets: "
                    f"{available} available, {required_with_reserve} required"
                )

        staged: list[tuple[dict, Path, Path]] = []
        safety_files: dict[Path, Path] = {}
        attempted: list[Path] = []
        removed_sidecars: list[Path] = []
        safety_suffix = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        cleanup_safety = False
        try:
            for entry, source, destination in destinations:
                destination.parent.mkdir(parents=True, exist_ok=True)
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{destination.name}.restore-", dir=destination.parent
                )
                os.close(descriptor)
                temporary_path = Path(temporary_name)
                # Track immediately so copy/chmod/fsync failures cannot strand
                # a DB-sized same-directory temporary file.
                staged.append((entry, temporary_path, destination))
                shutil.copy2(source, temporary_path)
                mode = 0o600 if entry.get("sqlite") else int(entry.get("mode", 0o600))
                os.chmod(temporary_path, mode)
                _fsync_file(temporary_path)

            # Preserve every original, including SQLite sidecars, before the
            # first target is promoted. These names are on the target's own
            # filesystem, so rollback never depends on a cross-device rename.
            originals = [
                destination
                for _, _, destination in staged
                if destination.exists()
            ]
            for entry, _, destination in staged:
                if entry.get("sqlite"):
                    originals.extend(
                        sidecar
                        for suffix in ("-wal", "-shm")
                        if os.path.lexists(
                            sidecar := Path(f"{destination}{suffix}")
                        )
                    )
            for position, original in enumerate(dict.fromkeys(originals)):
                safety = original.with_name(
                    f".{original.name}.pre-restore-{safety_suffix}-{position}"
                )
                _safety_link(original, safety)
                safety_files[original] = safety
                _fsync_file(safety)
            for directory in {
                path.parent for path in [*safety_files, *safety_files.values()]
            }:
                _fsync_directory(directory)

            for entry, temporary_path, destination in staged:
                attempted.append(destination)
                if entry.get("sqlite") and replace:
                    for suffix in ("-wal", "-shm"):
                        sidecar = Path(f"{destination}{suffix}")
                        if os.path.lexists(sidecar):
                            sidecar.unlink()
                            removed_sidecars.append(sidecar)
                temporary_path.replace(destination)
            for directory in {destination.parent for _, _, destination in staged}:
                _fsync_directory(directory)
            cleanup_safety = True
        except BaseException as restore_error:
            # Reverse every attempted promotion. A target that did not exist
            # before restore is removed; an existing target is recovered from
            # its same-filesystem safety name. Sidecars are restored only after
            # the original SQLite database is back in place.
            try:
                for destination in reversed(attempted):
                    safety = safety_files.get(destination)
                    if destination.exists():
                        destination.unlink()
                    if safety is not None and safety.exists():
                        safety.replace(destination)
                for sidecar in removed_sidecars:
                    safety = safety_files.get(sidecar)
                    if os.path.lexists(sidecar):
                        sidecar.unlink()
                    if safety is not None and safety.exists():
                        safety.replace(sidecar)
                for directory in {
                    path.parent for path in [*safety_files, *attempted]
                }:
                    _fsync_directory(directory)
            except BaseException as rollback_error:
                raise RuntimeError(
                    "restore failed and automatic rollback was incomplete; "
                    "preserved .pre-restore files require operator recovery"
                ) from rollback_error
            cleanup_safety = True
            raise restore_error
        finally:
            for _, temporary_path, _ in staged:
                if temporary_path.exists():
                    temporary_path.unlink()
            if cleanup_safety:
                for safety in safety_files.values():
                    if safety.exists():
                        safety.unlink()
            for directory in {
                path.parent
                for path in [
                    *[temporary_path for _, temporary_path, _ in staged],
                    *safety_files.values(),
                ]
            }:
                _fsync_directory(directory)
    return {
        "ok": True,
        "archive": str(path),
        "target_root": str(root),
        "restored": [
            entry["restore_path"]
            for entry in manifest["entries"]
            if not entry.get("evidence")
        ],
        "metadata": manifest.get("metadata", {}),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create, verify, or restore a runtime-state backup"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--sqlite", type=Path, action="append", default=[])
    create.add_argument("--path", type=Path, action="append", default=[])
    create.add_argument(
        "--evidence",
        type=Path,
        action="append",
        default=[],
        help="verified file retained in the archive but never auto-restored",
    )
    create.add_argument(
        "--metadata",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="recovery metadata recorded in the manifest",
    )
    verify = subparsers.add_parser("verify")
    verify.add_argument("archive", type=Path)
    restore = subparsers.add_parser("restore")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--target-root", type=Path, default=Path("."))
    restore.add_argument("--replace", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "create":
        metadata: dict[str, str] = {}
        for item in args.metadata:
            key, separator, value = item.partition("=")
            if not separator or not key or len(key) > 128 or len(value) > 4096:
                raise ValueError("--metadata must be KEY=VALUE within size limits")
            if key in metadata:
                raise ValueError(f"duplicate metadata key: {key}")
            metadata[key] = value
        result = create_archive(
            args.output,
            args.sqlite,
            args.path,
            evidence_paths=args.evidence,
            metadata=metadata,
        )
    elif args.command == "verify":
        result = verify_archive(args.archive)
    else:
        result = restore_archive(
            args.archive, target_root=args.target_root, replace=args.replace
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
