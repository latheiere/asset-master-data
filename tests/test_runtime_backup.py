import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from statecrate import SignatureError, SigningKey


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "runtime_backup.py"
SPEC = importlib.util.spec_from_file_location("runtime_backup", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
RUNTIME_BACKUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNTIME_BACKUP)


def test_runtime_backup_round_trip(tmp_path: Path) -> None:
    database = tmp_path / "live.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample VALUES ('durable')")
    database.chmod(0o666)
    state = tmp_path / "settings.json"
    state.write_text('{"enabled": true}\n', encoding="utf-8")
    archive = tmp_path / "backup.tar.gz"
    key = SigningKey.generate()

    created = RUNTIME_BACKUP.create_archive(
        archive,
        [database],
        [state],
        signing_key=key,
    )
    verified = RUNTIME_BACKUP.verify_archive(
        archive, trusted_key=key.verification_key
    )
    target_root = tmp_path / "restore-root"
    restored = RUNTIME_BACKUP.restore_archive(
        archive,
        target_root=target_root,
        trusted_key=key.verification_key,
    )

    assert created["entries"]
    assert verified["entries_checked"] == 2
    assert restored["ok"] is True
    restored_database = target_root / Path(*database.parts[1:])
    with sqlite3.connect(restored_database) as connection:
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "durable"
    assert restored_database.stat().st_mode & 0o777 == 0o600
    restored_state = target_root / Path(*state.parts[1:])
    assert restored_state.read_text(encoding="utf-8") == '{"enabled": true}\n'


def test_cli_creates_persistent_keys_and_uses_them_for_verification(
    tmp_path: Path,
) -> None:
    state = tmp_path / "settings.json"
    state.write_text("{}\n", encoding="utf-8")
    archive = tmp_path / "backup.tar.gz"

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "create",
            "--output",
            str(archive),
            "--path",
            str(state),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "verify", str(archive)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout)["ok"] is True
    assert (tmp_path / "statecrate-signing-key.pem").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "statecrate-verification-key.pem").is_file()


def test_runtime_backup_rejects_an_untrusted_signer(tmp_path: Path) -> None:
    state = tmp_path / "settings.json"
    state.write_text("{}\n", encoding="utf-8")
    archive = tmp_path / "backup.tar.gz"
    untrusted = SigningKey.generate()
    RUNTIME_BACKUP.create_archive(
        archive, [], [state], signing_key=untrusted
    )

    with pytest.raises(SignatureError):
        RUNTIME_BACKUP.verify_archive(
            archive, trusted_key=SigningKey.generate().verification_key
        )


def test_runtime_backup_rejects_output_overlap_and_nested_symlinks(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "settings.json").write_text("{}\n", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    (source / "linked").symlink_to(external, target_is_directory=True)
    key = SigningKey.generate()

    with pytest.raises(ValueError, match="contains a symlink"):
        RUNTIME_BACKUP.create_archive(
            tmp_path / "backup.tar.gz", [], [source], signing_key=key
        )
    (source / "linked").unlink()
    with pytest.raises(ValueError, match="overlaps input"):
        RUNTIME_BACKUP.create_archive(
            source / "backup.tar.gz", [], [source], signing_key=key
        )


def test_evidence_and_metadata_are_verified_but_not_restored(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text("archived\n", encoding="utf-8")
    evidence = tmp_path / "active-config.yaml"
    evidence.write_text("release: prior\n", encoding="utf-8")
    archive = tmp_path / "backup.tar.gz"
    key = SigningKey.generate()
    RUNTIME_BACKUP.create_archive(
        archive,
        [],
        [state],
        signing_key=key,
        evidence_paths=[evidence],
        metadata={"runtime_revision": "a" * 40},
    )

    verified = RUNTIME_BACKUP.verify_archive(
        archive, trusted_key=key.verification_key
    )
    target = tmp_path / "restore-root"
    restored = RUNTIME_BACKUP.restore_archive(
        archive,
        target_root=target,
        trusted_key=key.verification_key,
    )

    assert verified["metadata"] == {"runtime_revision": "a" * 40}
    assert restored["metadata"] == verified["metadata"]
    assert restored["restored"] == [Path(*state.parts[1:]).as_posix()]
    assert (target / Path(*state.parts[1:])).read_text() == "archived\n"
    assert not (target / Path(*evidence.parts[1:])).exists()


def test_runtime_restore_rejects_destination_symlink(tmp_path: Path) -> None:
    source = tmp_path / "settings.json"
    source.write_text("archived\n", encoding="utf-8")
    archive = tmp_path / "backup.tar.gz"
    key = SigningKey.generate()
    RUNTIME_BACKUP.create_archive(archive, [], [source], signing_key=key)
    target_root = tmp_path / "restore-root"
    destination = target_root / Path(*source.parts[1:])
    destination.parent.mkdir(parents=True)
    victim = tmp_path / "victim.json"
    victim.write_text("keep\n", encoding="utf-8")
    destination.symlink_to(victim)

    with pytest.raises(ValueError, match="must not be a symlink"):
        RUNTIME_BACKUP.restore_archive(
            archive,
            target_root=target_root,
            trusted_key=key.verification_key,
            replace=True,
        )

    assert victim.read_text(encoding="utf-8") == "keep\n"


def test_runtime_restore_handles_sqlite_sidecars(tmp_path: Path) -> None:
    database = tmp_path / "live.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample VALUES ('archived')")
    archive = tmp_path / "backup.tar.gz"
    key = SigningKey.generate()
    RUNTIME_BACKUP.create_archive(archive, [database], [], signing_key=key)
    target_root = tmp_path / "restore-root"
    destination = target_root / Path(*database.parts[1:])
    destination.parent.mkdir(parents=True)
    stale_wal = Path(f"{destination}-wal")
    stale_wal.write_bytes(b"stale")

    with pytest.raises(ValueError, match="SQLite sidecars"):
        RUNTIME_BACKUP.restore_archive(
            archive,
            target_root=target_root,
            trusted_key=key.verification_key,
        )
    RUNTIME_BACKUP.restore_archive(
        archive,
        target_root=target_root,
        trusted_key=key.verification_key,
        replace=True,
    )

    assert not stale_wal.exists()
    with sqlite3.connect(destination) as connection:
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "archived"


def test_restore_rolls_back_all_targets_on_mid_promotion_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "live.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample VALUES ('archived')")
    config = tmp_path / "settings.json"
    config.write_text('{"state": "archived"}\n', encoding="utf-8")
    archive = tmp_path / "backup.tar.gz"
    key = SigningKey.generate()
    RUNTIME_BACKUP.create_archive(
        archive, [database], [config], signing_key=key
    )
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE sample SET value = 'current'")
    config.write_text('{"state": "current"}\n', encoding="utf-8")
    wal = Path(f"{database}-wal")
    wal.write_bytes(b"preserve-sidecar")
    original_replace = Path.replace
    promotions = 0

    def fail_second_promotion(path: Path, target: Path) -> Path:
        nonlocal promotions
        if ".restore-" in path.name and ".pre-restore-" not in path.name:
            promotions += 1
            if promotions == 2:
                raise OSError("injected promotion failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_second_promotion)
    with pytest.raises(OSError, match="injected promotion failure"):
        RUNTIME_BACKUP.restore_archive(
            archive,
            target_root=Path("/"),
            trusted_key=key.verification_key,
            replace=True,
        )

    assert config.read_text(encoding="utf-8") == '{"state": "current"}\n'
    assert wal.read_bytes() == b"preserve-sidecar"
    wal.unlink()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "current"
    assert not list(tmp_path.glob(".*.pre-restore-*"))


def test_restore_verifies_and_extracts_the_archive_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state.json"
    state.write_text("archived\n", encoding="utf-8")
    archive = tmp_path / "backup.tar.gz"
    key = SigningKey.generate()
    RUNTIME_BACKUP.create_archive(archive, [], [state], signing_key=key)
    original_restore = RUNTIME_BACKUP.restore
    calls = 0

    def count_restore(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_restore(*args, **kwargs)

    monkeypatch.setattr(RUNTIME_BACKUP, "restore", count_restore)
    RUNTIME_BACKUP.restore_archive(
        archive,
        target_root=tmp_path / "restore-root",
        trusted_key=key.verification_key,
    )

    assert calls == 1


def test_restore_cleans_staged_file_when_copy_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "settings.json"
    state.write_text("archived\n", encoding="utf-8")
    archive = tmp_path / "backup.tar.gz"
    key = SigningKey.generate()
    RUNTIME_BACKUP.create_archive(archive, [], [state], signing_key=key)
    original_copy = RUNTIME_BACKUP.shutil.copy2

    def fail_restore_copy(source, destination, *args, **kwargs):
        if ".restore-" in Path(destination).name:
            Path(destination).write_bytes(b"partial")
            raise OSError("injected copy failure")
        return original_copy(source, destination, *args, **kwargs)

    monkeypatch.setattr(RUNTIME_BACKUP.shutil, "copy2", fail_restore_copy)
    with pytest.raises(OSError, match="injected copy failure"):
        RUNTIME_BACKUP.restore_archive(
            archive,
            target_root=Path("/"),
            trusted_key=key.verification_key,
            replace=True,
        )

    assert state.read_text(encoding="utf-8") == "archived\n"
    assert not list(tmp_path.glob(".*.restore-*"))


def test_runtime_backup_rejects_empty_input_set(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no backup inputs"):
        RUNTIME_BACKUP.create_archive(
            tmp_path / "empty.tar.gz",
            [],
            [],
            signing_key=SigningKey.generate(),
        )
