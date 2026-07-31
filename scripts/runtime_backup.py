#!/usr/bin/env python3
"""Create, verify, and restore signed runtime backups."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from statecrate import (
    Limits,
    SigningKey,
    Source,
    VerificationKey,
    backup,
    restore,
    verify,
)


LIMITS = Limits(
    max_members=10_000,
    max_expanded_bytes=1024 * 1024 * 1024,
    max_manifest_bytes=8 * 1024 * 1024,
    max_member_name_length=4096,
    min_free_bytes=128 * 1024 * 1024,
)
_APPLICATION = "asset-master-data"
_SIGNING_KEY_NAME = "statecrate-signing-key.pem"
_VERIFICATION_KEY_NAME = "statecrate-verification-key.pem"


def _safe_name(path: Path, used: set[str]) -> str:
    base = path.name or "state"
    candidate = base
    counter = 2
    while candidate in used:
        candidate = f"{base}-{counter}"
        counter += 1
    used.add(candidate)
    return candidate


def _reject_symlinks(source: Path) -> None:
    if source.is_symlink():
        raise ValueError(f"backup input must not be a symlink: {source}")
    if source.is_dir():
        for candidate in source.rglob("*"):
            if candidate.is_symlink():
                raise ValueError(f"backup input contains a symlink: {candidate}")
    elif not source.is_file():
        raise ValueError(
            f"backup input must be a regular file or directory: {source}"
        )


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
        return Path(*expanded.parts[1:])
    if ".." in expanded.parts:
        return Path(fallback)
    return expanded


def _runtime_metadata(metadata: dict[str, str] | None) -> dict[str, str]:
    values = dict(sorted((metadata or {}).items()))
    if not all(
        isinstance(key, str)
        and isinstance(value, str)
        and 0 < len(key) <= 128
        and len(value) <= 4096
        for key, value in values.items()
    ):
        raise ValueError("backup metadata must map bounded strings to strings")
    return values


def _backup_sources(
    output: Path,
    sqlite_paths: list[Path],
    paths: list[Path],
    evidence_paths: list[Path],
) -> tuple[list[Source], list[str], list[str], list[dict[str, Any]]]:
    expanded_sqlite = [source.expanduser() for source in sqlite_paths]
    expanded_paths = [source.expanduser() for source in paths]
    expanded_evidence = [source.expanduser() for source in evidence_paths]
    all_inputs = [*expanded_sqlite, *expanded_paths, *expanded_evidence]
    missing = [str(source) for source in all_inputs if not source.exists()]
    if missing:
        raise ValueError(
            "missing required backup input(s): " + ", ".join(sorted(missing))
        )
    resolved_sources: set[Path] = set()
    for source in all_inputs:
        _reject_symlinks(source)
        resolved = source.resolve()
        if resolved in resolved_sources:
            raise ValueError(f"duplicate backup input: {source}")
        resolved_sources.add(resolved)
        if output == resolved or (source.is_dir() and output.is_relative_to(resolved)):
            raise ValueError(f"backup output overlaps input: {source}")

    sources: list[Source] = []
    restore_files: list[str] = []
    sqlite_restore_paths: list[str] = []
    entries: list[dict[str, Any]] = []
    for source in expanded_sqlite:
        if not source.is_file():
            raise ValueError(f"SQLite backup input must be a file: {source}")
        restore_path = _portable_restore_path(source, source.name).as_posix()
        sources.append(
            Source.sqlite(source, at=f"restore/{restore_path}", mode=0o600)
        )
        restore_files.append(restore_path)
        sqlite_restore_paths.append(restore_path)
        entries.append(
            {"path": restore_path, "restore_path": restore_path, "sqlite": True}
        )

    for source in expanded_paths:
        logical_root = _portable_restore_path(source, source.name)
        source_files = _files(source) if source.is_dir() else [source]
        for file_path in source_files:
            relative = (
                logical_root / file_path.relative_to(source)
                if source.is_dir()
                else logical_root
            ).as_posix()
            sources.append(Source.file(file_path, at=f"restore/{relative}"))
            restore_files.append(relative)
            entries.append(
                {"path": relative, "restore_path": relative, "sqlite": False}
            )

    used_evidence_names: set[str] = set()
    for source in expanded_evidence:
        name = _safe_name(source, used_evidence_names)
        source_files = _files(source) if source.is_dir() else [source]
        for file_path in source_files:
            relative = (
                Path(name) / file_path.relative_to(source)
                if source.is_dir()
                else Path(name)
            ).as_posix()
            sources.append(Source.file(file_path, at=f"evidence/{relative}"))
            entries.append({"path": relative, "evidence": True, "sqlite": False})

    if not sources:
        raise ValueError("no backup inputs exist")
    if len(set(restore_files)) != len(restore_files):
        raise ValueError("duplicate restore path")
    return sources, restore_files, sqlite_restore_paths, entries


def create_archive(
    output: Path,
    sqlite_paths: list[Path],
    paths: list[Path],
    *,
    signing_key: SigningKey,
    evidence_paths: list[Path] | None = None,
    metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    requested_output = output.expanduser().absolute()
    if requested_output.is_symlink():
        raise ValueError(f"backup output must not be a symlink: {requested_output}")
    resolved_output = requested_output.parent.resolve() / requested_output.name
    sources, restore_files, sqlite_restore_paths, entries = _backup_sources(
        resolved_output,
        sqlite_paths,
        paths,
        evidence_paths or [],
    )
    runtime_metadata = _runtime_metadata(metadata)
    signed_metadata: dict[str, object] = {
        "application": _APPLICATION,
        "runtime_metadata": runtime_metadata,
        "restore_files": restore_files,
        "sqlite_restore_paths": sqlite_restore_paths,
        "file_count": len(entries),
    }
    with tempfile.TemporaryDirectory(prefix="runtime-backup-plan-") as temporary:
        plan = Path(temporary) / "restore-plan.json"
        plan.write_text(
            json.dumps(signed_metadata, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        sources.append(Source.file(plan, at="control/restore-plan.json", mode=0o600))
        report = backup(
            resolved_output,
            sources,
            signing_key=signing_key,
            limits=LIMITS,
            metadata=signed_metadata,
            overwrite=True,
        )
    return {
        "ok": True,
        "archive": str(report.archive),
        "format": "statecrate-1",
        "created_at": report.created_at,
        "entries": entries,
        "missing_optional_inputs": [],
        "metadata": runtime_metadata,
        "signer_key_id": report.signer_key_id,
    }


def _validated_archive_metadata(
    metadata: object,
) -> tuple[dict[str, str], list[str], set[str], int]:
    if not isinstance(metadata, dict) or metadata.get("application") != _APPLICATION:
        raise ValueError("backup metadata does not identify asset-master-data")
    runtime_metadata = metadata.get("runtime_metadata")
    restore_files = metadata.get("restore_files")
    sqlite_restore_paths = metadata.get("sqlite_restore_paths")
    file_count = metadata.get("file_count")
    if not isinstance(runtime_metadata, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in runtime_metadata.items()
    ):
        raise ValueError("backup runtime metadata must map strings to strings")
    if not isinstance(restore_files, list) or not all(
        isinstance(value, str) for value in restore_files
    ):
        raise ValueError("backup restore file list is invalid")
    if len(set(restore_files)) != len(restore_files):
        raise ValueError("backup restore file list contains duplicates")
    if not isinstance(sqlite_restore_paths, list) or not all(
        isinstance(value, str) for value in sqlite_restore_paths
    ):
        raise ValueError("backup SQLite restore file list is invalid")
    if not set(sqlite_restore_paths).issubset(restore_files):
        raise ValueError("backup SQLite restore paths are not restorable files")
    if type(file_count) is not int or file_count < len(restore_files):
        raise ValueError("backup file count is invalid")
    for value in restore_files:
        relative = Path(value)
        if not value or relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe restore path: {value}")
    return runtime_metadata, restore_files, set(sqlite_restore_paths), file_count


def verify_archive(path: Path, *, trusted_key: VerificationKey) -> dict[str, Any]:
    report = verify(path, trusted_keys=trusted_key, limits=LIMITS)
    runtime_metadata, _, _, file_count = _validated_archive_metadata(
        dict(report.metadata)
    )
    return {
        "ok": True,
        "archive": str(report.archive),
        "format": "statecrate-1",
        "entries_checked": file_count,
        "metadata": runtime_metadata,
        "signer_key_id": report.signer_key_id,
    }


def restore_archive(
    path: Path,
    *,
    target_root: Path,
    trusted_key: VerificationKey,
    replace: bool = False,
) -> dict[str, Any]:
    """Verify and atomically replace each logical runtime file under target_root."""
    archive_path = path.expanduser().resolve()
    root = target_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="runtime-restore-") as temporary:
        verified_root = Path(temporary) / "verified"
        report = restore(
            archive_path,
            verified_root,
            trusted_keys=trusted_key,
            limits=LIMITS,
        )
        plan_path = verified_root / "control" / "restore-plan.json"
        if not plan_path.is_file() or plan_path.is_symlink():
            raise ValueError("backup restore plan is not a regular file")
        runtime_metadata, restore_files, sqlite_paths, _ = _validated_archive_metadata(
            json.loads(plan_path.read_text(encoding="utf-8"))
        )
        destinations: list[tuple[str, bool, Path, Path]] = []
        destination_paths: set[Path] = set()
        for restore_path in restore_files:
            relative = Path(restore_path)
            source = verified_root / "restore" / relative
            if not source.is_file() or source.is_symlink():
                raise ValueError(f"restored source is not a regular file: {restore_path}")
            requested_destination = root / relative
            destination = requested_destination.parent.resolve() / requested_destination.name
            if destination != root and root not in destination.parents:
                raise ValueError(f"restore target escapes target root: {relative}")
            if destination.is_symlink():
                raise ValueError(f"restore target must not be a symlink: {destination}")
            if destination in destination_paths:
                raise ValueError(f"duplicate restore destination: {destination}")
            destination_paths.add(destination)
            sqlite_entry = restore_path in sqlite_paths
            sqlite_sidecars = (
                [Path(f"{destination}{suffix}") for suffix in ("-wal", "-shm")]
                if sqlite_entry
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
            destinations.append((restore_path, sqlite_entry, source, destination))

        required_by_device: dict[int, int] = {}
        location_by_device: dict[int, Path] = {}
        for _, _, source, destination in destinations:
            existing_parent = destination.parent
            while not existing_parent.exists():
                existing_parent = existing_parent.parent
            device = existing_parent.stat().st_dev
            required_by_device[device] = (
                required_by_device.get(device, 0) + source.stat().st_size
            )
            location_by_device[device] = existing_parent
        for device, required in required_by_device.items():
            available = shutil.disk_usage(location_by_device[device]).free
            required_with_reserve = required + LIMITS.min_free_bytes
            if available < required_with_reserve:
                raise ValueError(
                    "insufficient free space to stage restore targets: "
                    f"{available} available, {required_with_reserve} required"
                )

        staged: list[tuple[bool, Path, Path]] = []
        safety_files: dict[Path, Path] = {}
        attempted: list[Path] = []
        removed_sidecars: list[Path] = []
        safety_suffix = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        cleanup_safety = False
        try:
            for _, sqlite_entry, source, destination in destinations:
                destination.parent.mkdir(parents=True, exist_ok=True)
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{destination.name}.restore-", dir=destination.parent
                )
                os.close(descriptor)
                temporary_path = Path(temporary_name)
                staged.append((sqlite_entry, temporary_path, destination))
                shutil.copy2(source, temporary_path)
                mode = 0o600 if sqlite_entry else source.stat().st_mode & 0o777
                os.chmod(temporary_path, mode)
                _fsync_file(temporary_path)

            originals = [
                destination for _, _, destination in staged if destination.exists()
            ]
            for sqlite_entry, _, destination in staged:
                if sqlite_entry:
                    originals.extend(
                        sidecar
                        for suffix in ("-wal", "-shm")
                        if os.path.lexists(sidecar := Path(f"{destination}{suffix}"))
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

            for sqlite_entry, temporary_path, destination in staged:
                attempted.append(destination)
                if sqlite_entry and replace:
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
                item.parent
                for item in [
                    *[temporary_path for _, temporary_path, _ in staged],
                    *safety_files.values(),
                ]
            }:
                _fsync_directory(directory)
    return {
        "ok": True,
        "archive": str(archive_path),
        "target_root": str(root),
        "restored": restore_files,
        "metadata": runtime_metadata,
        "signer_key_id": report.signer_key_id,
    }


def _default_key_paths(archive: Path) -> tuple[Path, Path]:
    directory = archive.expanduser().absolute().parent
    return directory / _SIGNING_KEY_NAME, directory / _VERIFICATION_KEY_NAME


def _load_or_create_signing_key(
    archive: Path, requested: Path | None
) -> SigningKey:
    if requested is not None:
        return SigningKey.load(requested)
    signing_path, verification_path = _default_key_paths(archive)
    if not signing_path.exists() and not verification_path.exists():
        signing_path.parent.mkdir(parents=True, exist_ok=True)
        key = SigningKey.generate()
        key.save(signing_path)
        key.verification_key.save(verification_path)
        return key
    if not signing_path.exists() or not verification_path.exists():
        raise ValueError("backup signing and verification keys must both exist")
    key = SigningKey.load(signing_path)
    if key.key_id != VerificationKey.load(verification_path).key_id:
        raise ValueError("backup signing and verification keys do not match")
    return key


def _load_verification_key(
    archive: Path, requested: Path | None
) -> VerificationKey:
    path = requested or _default_key_paths(archive)[1]
    if not path.exists():
        raise ValueError("backup verification key does not exist")
    return VerificationKey.load(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create, verify, or restore a signed runtime backup"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--sqlite", type=Path, action="append", default=[])
    create.add_argument("--path", type=Path, action="append", default=[])
    create.add_argument("--signing-key", type=Path)
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
        help="recovery metadata recorded in the signed manifest",
    )
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("archive", type=Path)
    verify_parser.add_argument("--verification-key", type=Path)
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("archive", type=Path)
    restore_parser.add_argument("--verification-key", type=Path)
    restore_parser.add_argument("--target-root", type=Path, default=Path("."))
    restore_parser.add_argument("--replace", action="store_true")
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
            signing_key=_load_or_create_signing_key(args.output, args.signing_key),
            evidence_paths=args.evidence,
            metadata=metadata,
        )
    elif args.command == "verify":
        result = verify_archive(
            args.archive,
            trusted_key=_load_verification_key(args.archive, args.verification_key),
        )
    else:
        result = restore_archive(
            args.archive,
            target_root=args.target_root,
            trusted_key=_load_verification_key(args.archive, args.verification_key),
            replace=args.replace,
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
