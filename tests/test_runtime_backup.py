import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import statecrate.api as statecrate_api
from statecrate import SignatureError, SigningKey
from mdv import runtime_backup as RUNTIME_BACKUP


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "runtime_backup.py"
MODULE = Path(__file__).resolve().parents[1] / "src" / "mdv" / "runtime_backup.py"


def _database(path: Path, value: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample VALUES (?)", (value,))


def _configuration(path: Path, port: int = 8090) -> None:
    path.write_text(f"server:\n  port: {port}\n", encoding="utf-8")


def _identity(configuration: Path) -> dict[str, str]:
    return RUNTIME_BACKUP.runtime_identity(
        release="v1.2.3-aaaaaaaaaaaa",
        revision="a" * 40,
        version="1.2.3",
        configuration=configuration,
    )


def test_runtime_backup_replaces_the_complete_state_directory(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = state / "mdv.sqlite3"
    _database(database, "archived")
    configuration = tmp_path / "config.yaml"
    _configuration(configuration)
    archive = tmp_path / "backups" / "asset-master-data-runtime.tar.gz"
    archive.parent.mkdir()
    key = SigningKey.generate()
    identity = _identity(configuration)

    created = RUNTIME_BACKUP.create_archive(
        archive,
        state,
        database.name,
        signing_key=key,
        identity=identity,
    )
    database.unlink()
    _database(database, "candidate")
    (state / "mdv.sqlite3-wal").write_text("stale", encoding="utf-8")
    (state / "unrelated").write_text("remove", encoding="utf-8")

    restored = RUNTIME_BACKUP.restore_archive(
        archive,
        state,
        trusted_key=key.verification_key,
        expected_identity=identity,
        replace=True,
    )

    assert created["entries"] == 1
    assert restored["entries_restored"] == 1
    assert sorted(path.name for path in state.iterdir()) == ["mdv.sqlite3"]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "archived"
    assert database.stat().st_mode & 0o777 == 0o600


def test_statecrate_rolls_back_a_failed_directory_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_state = tmp_path / "source-state"
    source_state.mkdir()
    _database(source_state / "mdv.sqlite3", "archived")
    configuration = tmp_path / "config.yaml"
    _configuration(configuration)
    archive = tmp_path / "backup.tar.gz"
    key = SigningKey.generate()
    identity = _identity(configuration)
    RUNTIME_BACKUP.create_archive(
        archive,
        source_state,
        "mdv.sqlite3",
        signing_key=key,
        identity=identity,
    )
    target = tmp_path / "state"
    target.mkdir()
    current = target / "current"
    current.write_text("preserved", encoding="utf-8")
    real_replace = os.replace

    def fail_payload_install(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
        if Path(source).name == "payload" and Path(destination) == target:
            raise OSError("simulated StateCrate promotion failure")
        real_replace(source, destination)

    monkeypatch.setattr(statecrate_api.os, "replace", fail_payload_install)
    with pytest.raises(OSError, match="simulated StateCrate promotion failure"):
        RUNTIME_BACKUP.restore_archive(
            archive,
            target,
            trusted_key=key.verification_key,
            expected_identity=identity,
            replace=True,
        )

    assert current.read_text(encoding="utf-8") == "preserved"


def test_configuration_is_identity_metadata_not_archive_payload(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _database(state / "mdv.sqlite3", "durable")
    configuration = tmp_path / "config.yaml"
    _configuration(configuration)
    archive = tmp_path / "backup.tar.gz"
    key = SigningKey.generate()
    identity = _identity(configuration)

    RUNTIME_BACKUP.create_archive(
        archive,
        state,
        "mdv.sqlite3",
        signing_key=key,
        identity=identity,
    )
    verified = RUNTIME_BACKUP.verify_archive(
        archive, trusted_key=key.verification_key
    )
    restored = tmp_path / "restored"
    RUNTIME_BACKUP.restore_archive(
        archive,
        restored,
        trusted_key=key.verification_key,
        expected_identity=identity,
    )

    assert verified["identity"] == identity
    assert verified["entries_checked"] == 1
    assert sorted(path.name for path in restored.iterdir()) == ["mdv.sqlite3"]
    assert not (restored / configuration.name).exists()


def test_restore_rejects_a_different_release_before_replacing_state(
    tmp_path: Path,
) -> None:
    source_state = tmp_path / "source-state"
    source_state.mkdir()
    _database(source_state / "mdv.sqlite3", "archived")
    configuration = tmp_path / "config.yaml"
    _configuration(configuration)
    archive = tmp_path / "backup.tar.gz"
    key = SigningKey.generate()
    identity = _identity(configuration)
    RUNTIME_BACKUP.create_archive(
        archive,
        source_state,
        "mdv.sqlite3",
        signing_key=key,
        identity=identity,
    )
    target = tmp_path / "state"
    target.mkdir()
    current = target / "current"
    current.write_text("preserved", encoding="utf-8")
    incompatible = dict(identity, revision="b" * 40)

    with pytest.raises(ValueError, match="selected immutable release"):
        RUNTIME_BACKUP.restore_archive(
            archive,
            target,
            trusted_key=key.verification_key,
            expected_identity=incompatible,
            replace=True,
        )

    assert current.read_text(encoding="utf-8") == "preserved"


def test_runtime_backup_rejects_an_untrusted_signer(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _database(state / "mdv.sqlite3", "durable")
    configuration = tmp_path / "config.yaml"
    _configuration(configuration)
    archive = tmp_path / "backup.tar.gz"
    RUNTIME_BACKUP.create_archive(
        archive,
        state,
        "mdv.sqlite3",
        signing_key=SigningKey.generate(),
        identity=_identity(configuration),
    )

    with pytest.raises(SignatureError):
        RUNTIME_BACKUP.verify_archive(
            archive, trusted_key=SigningKey.generate().verification_key
        )


def test_cli_uses_persistent_keys_and_stable_identity_arguments(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _database(state / "mdv.sqlite3", "durable")
    configuration = tmp_path / "config.yaml"
    _configuration(configuration)
    archive = tmp_path / "backups" / "asset-master-data-runtime.tar.gz"
    archive.parent.mkdir()
    identity_arguments = [
        "--release",
        "v1.2.3-aaaaaaaaaaaa",
        "--revision",
        "a" * 40,
        "--version",
        "1.2.3",
        "--configuration",
        str(configuration),
    ]

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "create",
            "--output",
            str(archive),
            "--state-dir",
            str(state),
            "--database-name",
            "mdv.sqlite3",
            *identity_arguments,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    verified = subprocess.run(
        [sys.executable, str(SCRIPT), "verify", str(archive)],
        check=True,
        capture_output=True,
        text=True,
    )
    restored = tmp_path / "restored"
    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "restore",
            str(archive),
            "--target",
            str(restored),
            *identity_arguments,
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(verified.stdout)["identity"]["revision"] == "a" * 40
    assert (archive.parent / "statecrate-signing-key.pem").stat().st_mode & 0o777 == 0o600
    assert (archive.parent / "statecrate-verification-key.pem").is_file()
    assert (restored / "mdv.sqlite3").is_file()


def test_archive_must_remain_outside_the_replaceable_state_tree(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _database(state / "mdv.sqlite3", "durable")
    configuration = tmp_path / "config.yaml"
    _configuration(configuration)

    with pytest.raises(ValueError, match="outside the replaceable state"):
        RUNTIME_BACKUP.create_archive(
            state / "backup.tar.gz",
            state,
            "mdv.sqlite3",
            signing_key=SigningKey.generate(),
            identity=_identity(configuration),
        )


def test_wrapper_has_no_multi_destination_restore_layer() -> None:
    source = MODULE.read_text(encoding="utf-8")

    for removed_mechanism in (
        "restore-plan",
        "target_root",
        "safety_link",
        "pre-restore",
        "sqlite_sidecar",
        "disk_usage",
        "shutil.copy2",
        "evidence_paths",
    ):
        assert removed_mechanism not in source
