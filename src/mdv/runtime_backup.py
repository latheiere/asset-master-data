"""Back up and replace Asset Master runtime state through StateCrate."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Mapping

from statecrate import (
    Limits,
    SigningKey,
    Source,
    VerificationKey,
    backup,
    restore,
    verify,
)


LIMITS = Limits(max_expanded_bytes=1024 * 1024 * 1024)
APPLICATION = "asset-master-data"
SIGNING_KEY_NAME = "statecrate-signing-key.pem"
VERIFICATION_KEY_NAME = "statecrate-verification-key.pem"
IDENTITY_KEYS = frozenset(
    {"release", "revision", "version", "configuration_sha256"}
)


def configuration_sha256(path: Path) -> str:
    configuration = path.expanduser().resolve()
    if not configuration.is_file():
        raise ValueError(f"configuration does not exist: {configuration}")
    return hashlib.sha256(configuration.read_bytes()).hexdigest()


def runtime_identity(
    *, release: str, revision: str, version: str, configuration: Path
) -> dict[str, str]:
    identity = {
        "release": release,
        "revision": revision.lower(),
        "version": version,
        "configuration_sha256": configuration_sha256(configuration),
    }
    return _validated_identity(identity)


def _validated_identity(metadata: Mapping[str, object]) -> dict[str, str]:
    if set(metadata) != IDENTITY_KEYS or not all(
        isinstance(value, str) for value in metadata.values()
    ):
        raise ValueError(
            "runtime identity must contain release, revision, version, and "
            "configuration_sha256 strings"
        )
    identity = {key: str(metadata[key]) for key in sorted(IDENTITY_KEYS)}
    if (
        not identity["release"]
        or len(identity["release"]) > 255
        or "/" in identity["release"]
        or identity["release"] in {".", ".."}
    ):
        raise ValueError("runtime release identity is invalid")
    if not re.fullmatch(r"[0-9a-f]{40}", identity["revision"]):
        raise ValueError("runtime revision must be a full lowercase Git SHA")
    if not re.fullmatch(
        r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)",
        identity["version"],
    ):
        raise ValueError("runtime version must be a final Semantic Version")
    if not re.fullmatch(r"[0-9a-f]{64}", identity["configuration_sha256"]):
        raise ValueError("configuration identity must be a lowercase SHA-256 digest")
    return identity


def _manifest_metadata(identity: Mapping[str, object]) -> dict[str, str]:
    return {"application": APPLICATION, **_validated_identity(identity)}


def _identity_from_manifest(metadata: Mapping[str, object]) -> dict[str, str]:
    if (
        set(metadata) != {*IDENTITY_KEYS, "application"}
        or metadata.get("application") != APPLICATION
    ):
        raise ValueError("backup metadata does not identify asset-master-data")
    return _validated_identity(
        {key: metadata.get(key) for key in IDENTITY_KEYS}
    )


def create_archive(
    output: Path,
    state_dir: Path,
    database_name: str,
    *,
    signing_key: SigningKey,
    identity: Mapping[str, object],
) -> dict[str, object]:
    state = state_dir.expanduser().resolve()
    archive = output.expanduser().absolute()
    if not database_name or Path(database_name).name != database_name:
        raise ValueError("database name must be one path component")
    database = state / database_name
    if archive == state or archive.is_relative_to(state):
        raise ValueError("backup archive must be outside the replaceable state directory")
    report = backup(
        archive,
        [Source.sqlite(database, at=database_name, mode=0o600)],
        signing_key=signing_key,
        limits=LIMITS,
        overwrite=True,
        metadata=_manifest_metadata(identity),
    )
    return {
        "ok": True,
        "archive": str(report.archive),
        "created_at": report.created_at,
        "entries": report.entries,
        "expanded_bytes": report.expanded_bytes,
        "identity": _validated_identity(identity),
        "signer_key_id": report.signer_key_id,
    }


def verify_archive(
    path: Path, *, trusted_key: VerificationKey
) -> dict[str, object]:
    report = verify(path, trusted_keys=trusted_key, limits=LIMITS)
    identity = _identity_from_manifest(dict(report.metadata))
    return {
        "ok": True,
        "archive": str(report.archive),
        "created_at": report.created_at,
        "entries_checked": report.entries_checked,
        "expanded_bytes": report.expanded_bytes,
        "identity": identity,
        "signer_key_id": report.signer_key_id,
    }


def restore_archive(
    path: Path,
    target: Path,
    *,
    trusted_key: VerificationKey,
    expected_identity: Mapping[str, object],
    replace: bool = False,
) -> dict[str, object]:
    verified = verify_archive(path, trusted_key=trusted_key)
    expected = _validated_identity(expected_identity)
    if verified["identity"] != expected:
        raise ValueError("backup identity does not match the selected immutable release")
    report = restore(
        path,
        target,
        trusted_keys=trusted_key,
        limits=LIMITS,
        replace=replace,
    )
    return {
        "ok": True,
        "archive": str(report.archive),
        "restored_to": str(report.destination),
        "entries_restored": report.restored_entries,
        "expanded_bytes": report.expanded_bytes,
        "identity": expected,
        "signer_key_id": report.signer_key_id,
    }


def _default_key_paths(archive: Path) -> tuple[Path, Path]:
    root = archive.expanduser().absolute().parent
    return root / SIGNING_KEY_NAME, root / VERIFICATION_KEY_NAME


def _load_or_create_signing_key(
    archive: Path, requested: Path | None
) -> SigningKey:
    if requested is not None:
        return SigningKey.load(requested)
    signing_path, verification_path = _default_key_paths(archive)
    if not signing_path.exists() and not verification_path.exists():
        signing_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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


def _add_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--release", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--configuration", type=Path, required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create, verify, or restore signed Asset Master runtime state"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--state-dir", type=Path, required=True)
    create.add_argument("--database-name", required=True)
    create.add_argument("--signing-key", type=Path)
    _add_identity_arguments(create)
    check = commands.add_parser("verify")
    check.add_argument("archive", type=Path)
    check.add_argument("--verification-key", type=Path)
    restore_command = commands.add_parser("restore")
    restore_command.add_argument("archive", type=Path)
    restore_command.add_argument("--target", type=Path, required=True)
    restore_command.add_argument("--verification-key", type=Path)
    restore_command.add_argument("--replace", action="store_true")
    _add_identity_arguments(restore_command)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "verify":
        result = verify_archive(
            args.archive,
            trusted_key=_load_verification_key(args.archive, args.verification_key),
        )
    else:
        identity = runtime_identity(
            release=args.release,
            revision=args.revision,
            version=args.version,
            configuration=args.configuration,
        )
        if args.command == "create":
            result = create_archive(
                args.output,
                args.state_dir,
                args.database_name,
                signing_key=_load_or_create_signing_key(
                    args.output, args.signing_key
                ),
                identity=identity,
            )
        else:
            result = restore_archive(
                args.archive,
                args.target,
                trusted_key=_load_verification_key(
                    args.archive, args.verification_key
                ),
                expected_identity=identity,
                replace=args.replace,
            )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
