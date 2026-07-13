import json
import importlib.util
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "runtime_backup.py"
SPEC = importlib.util.spec_from_file_location("runtime_backup", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
RUNTIME_BACKUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNTIME_BACKUP)


def test_runtime_backup_round_trip(tmp_path):
    database = tmp_path / "live.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        conn.execute("INSERT INTO sample VALUES ('durable')")
    database.chmod(0o666)
    state = tmp_path / "settings.json"
    state.write_text('{"enabled": true}\n', encoding="utf-8")
    archive = tmp_path / "backup.tar.gz"

    created = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "create",
            "--output",
            str(archive),
            "--sqlite",
            str(database),
            "--path",
            str(state),
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

    created_payload = json.loads(created.stdout)
    assert created_payload["entries"]
    assert next(
        entry["mode"] for entry in created_payload["entries"] if entry["sqlite"]
    ) == 0o600
    assert archive.stat().st_mode & 0o777 == 0o600
    assert json.loads(verified.stdout)["entries_checked"] == 2

    with sqlite3.connect(database) as conn:
        conn.execute("DELETE FROM sample")
        conn.execute("INSERT INTO sample VALUES ('damaged')")
    state.write_text('{"enabled": false}\n', encoding="utf-8")
    refused = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "restore",
            str(archive),
            "--target-root",
            "/",
        ],
        capture_output=True,
        text=True,
    )
    restored = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "restore",
            str(archive),
            "--target-root",
            "/",
            "--replace",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert refused.returncode != 0
    assert "restore target exists" in refused.stderr
    assert json.loads(restored.stdout)["ok"] is True
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT value FROM sample").fetchone()[0] == "durable"
    assert database.stat().st_mode & 0o777 == 0o600
    assert state.read_text(encoding="utf-8") == '{"enabled": true}\n'


def test_runtime_backup_rejects_unsafe_archive(tmp_path):
    archive = tmp_path / "unsafe.tar.gz"
    payload = tmp_path / "payload"
    payload.write_text("unsafe", encoding="utf-8")
    with tarfile.open(archive, "w:gz") as output:
        output.add(payload, arcname="../payload")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "verify", str(archive)],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "unsafe archive member" in result.stderr


def test_runtime_backup_rejects_members_outside_state_and_duplicates(tmp_path):
    payload = tmp_path / "payload"
    payload.write_text("unsafe", encoding="utf-8")
    outside = tmp_path / "outside.tar.gz"
    with tarfile.open(outside, "w:gz") as output:
        output.add(payload, arcname="other/payload")
    with pytest.raises(ValueError, match="unsafe archive member"):
        RUNTIME_BACKUP.verify_archive(outside)

    duplicate = tmp_path / "duplicate.tar.gz"
    with tarfile.open(duplicate, "w:gz") as output:
        output.add(payload, arcname="state/manifest.json")
        output.add(payload, arcname="state/manifest.json")
    with pytest.raises(ValueError, match="duplicate archive member"):
        RUNTIME_BACKUP.verify_archive(duplicate)


def test_runtime_backup_rejects_archive_expansion_over_quota(monkeypatch, tmp_path):
    state = tmp_path / "settings.json"
    state.write_text("{}\n", encoding="utf-8")
    archive = tmp_path / "backup.tar.gz"
    RUNTIME_BACKUP.create_archive(archive, [], [state])
    monkeypatch.setattr(RUNTIME_BACKUP, "MAX_EXTRACTED_BYTES", 1)

    with pytest.raises(ValueError, match="expanded byte limit"):
        RUNTIME_BACKUP.verify_archive(archive)


def test_runtime_backup_enforces_member_limit_without_getmembers(monkeypatch, tmp_path):
    state = tmp_path / "settings.json"
    state.write_text("{}\n", encoding="utf-8")
    archive = tmp_path / "backup.tar.gz"
    RUNTIME_BACKUP.create_archive(archive, [], [state])

    def forbidden_getmembers(*_args, **_kwargs):
        raise AssertionError("unbounded getmembers must not be called")

    monkeypatch.setattr(tarfile.TarFile, "getmembers", forbidden_getmembers)
    assert RUNTIME_BACKUP.verify_archive(archive)["entries_checked"] == 1
    monkeypatch.setattr(RUNTIME_BACKUP, "MAX_ARCHIVE_MEMBERS", 1)
    with pytest.raises(ValueError, match="member limit"):
        RUNTIME_BACKUP.verify_archive(archive)


def test_runtime_backup_rejects_extraction_without_free_space(monkeypatch, tmp_path):
    state = tmp_path / "settings.json"
    state.write_text("{}\n", encoding="utf-8")
    archive = tmp_path / "backup.tar.gz"
    RUNTIME_BACKUP.create_archive(archive, [], [state])
    monkeypatch.setattr(
        RUNTIME_BACKUP.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=10, used=9, free=1),
    )

    with pytest.raises(ValueError, match="insufficient free space"):
        RUNTIME_BACKUP.verify_archive(archive)


def test_runtime_backup_rejects_output_symlink_and_source_overlap(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "settings.json").write_text("{}\n", encoding="utf-8")
    linked_output = tmp_path / "linked.tar.gz"
    linked_output.symlink_to(tmp_path / "elsewhere.tar.gz")

    with pytest.raises(ValueError, match="must not be a symlink"):
        RUNTIME_BACKUP.create_archive(linked_output, [], [source / "settings.json"])
    with pytest.raises(ValueError, match="overlaps input"):
        RUNTIME_BACKUP.create_archive(source / "backup.tar.gz", [], [source])


def test_runtime_backup_rejects_nested_input_symlink(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    (external / ".env").write_text("SECRET=do-not-copy\n", encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    (source / "linked").symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="contains a symlink"):
        RUNTIME_BACKUP.create_archive(tmp_path / "backup.tar.gz", [], [source])


def test_runtime_backup_reserves_manifest_name(tmp_path):
    source = tmp_path / "manifest.json"
    source.write_text('{"user": true}\n', encoding="utf-8")
    archive = tmp_path / "backup.tar.gz"

    created = RUNTIME_BACKUP.create_archive(archive, [], [source])

    assert created["entries"][0]["path"] == "manifest.json-2"
    assert RUNTIME_BACKUP.verify_archive(archive)["entries_checked"] == 1


def test_runtime_restore_rejects_destination_symlink(tmp_path):
    source = tmp_path / "settings.json"
    source.write_text("archived\n", encoding="utf-8")
    archive = tmp_path / "backup.tar.gz"
    RUNTIME_BACKUP.create_archive(archive, [], [source])
    target_root = tmp_path / "restore-root"
    relative = Path(*source.parts[1:])
    destination = target_root / relative
    destination.parent.mkdir(parents=True)
    victim = tmp_path / "victim.json"
    victim.write_text("keep\n", encoding="utf-8")
    destination.symlink_to(victim)

    with pytest.raises(ValueError, match="must not be a symlink"):
        RUNTIME_BACKUP.restore_archive(
            archive, target_root=target_root, replace=True
        )

    assert destination.is_symlink()
    assert victim.read_text(encoding="utf-8") == "keep\n"


def test_runtime_restore_handles_sidecars_when_database_is_absent(tmp_path):
    database = tmp_path / "live.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        conn.execute("INSERT INTO sample VALUES ('archived')")
    archive = tmp_path / "backup.tar.gz"
    RUNTIME_BACKUP.create_archive(archive, [database], [])
    target_root = tmp_path / "restore-root"
    destination = target_root / Path(*database.parts[1:])
    destination.parent.mkdir(parents=True)
    stale_wal = Path(f"{destination}-wal")
    stale_wal.write_bytes(b"stale")

    with pytest.raises(ValueError, match="SQLite sidecars"):
        RUNTIME_BACKUP.restore_archive(archive, target_root=target_root)

    RUNTIME_BACKUP.restore_archive(
        archive, target_root=target_root, replace=True
    )
    assert not stale_wal.exists()
    with sqlite3.connect(destination) as conn:
        assert conn.execute("SELECT value FROM sample").fetchone()[0] == "archived"


def test_runtime_restore_rejects_dangling_sidecar_symlink(tmp_path):
    database = tmp_path / "live.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE sample (value TEXT NOT NULL)")
    archive = tmp_path / "backup.tar.gz"
    RUNTIME_BACKUP.create_archive(archive, [database], [])
    target_root = tmp_path / "restore-root"
    destination = target_root / Path(*database.parts[1:])
    destination.parent.mkdir(parents=True)
    stale_wal = Path(f"{destination}-wal")
    stale_wal.symlink_to(tmp_path / "missing-wal-target")

    with pytest.raises(ValueError, match="sidecar must not be a symlink"):
        RUNTIME_BACKUP.restore_archive(
            archive, target_root=target_root, replace=True
        )

    assert stale_wal.is_symlink()
    assert not destination.exists()


def test_runtime_restore_fails_safely_when_hardlink_safety_is_unavailable(
    tmp_path, monkeypatch
):
    state = tmp_path / "settings.json"
    state.write_text("archived\n", encoding="utf-8")
    archive = tmp_path / "backup.tar.gz"
    RUNTIME_BACKUP.create_archive(archive, [], [state])
    state.write_text("current\n", encoding="utf-8")

    def deny_hardlink(_source, _destination):
        raise OSError("hardlinks unavailable")

    monkeypatch.setattr(RUNTIME_BACKUP.os, "link", deny_hardlink)
    with pytest.raises(OSError, match="hardlinks unavailable"):
        RUNTIME_BACKUP.restore_archive(
            archive, target_root=Path("/"), replace=True
        )

    assert state.read_text(encoding="utf-8") == "current\n"
    assert not list(tmp_path.glob(".*.pre-restore-*"))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("manifest_path", "duplicate manifest path"),
        ("restore_path", "duplicate restore path"),
    ],
)
def test_runtime_backup_rejects_duplicate_manifest_destinations(
    tmp_path, mutation, message
):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text("{}\n", encoding="utf-8")
    second.write_text("{}\n", encoding="utf-8")
    archive = tmp_path / "valid.tar.gz"
    RUNTIME_BACKUP.create_archive(archive, [], [first, second])

    with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
        root = Path(temporary)
        with tarfile.open(archive, "r:gz") as source_archive:
            source_archive.extractall(root, filter="data")
        manifest_path = root / "state" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if mutation == "manifest_path":
            manifest["entries"].append(dict(manifest["entries"][0]))
        else:
            manifest["entries"][1]["restore_path"] = manifest["entries"][0][
                "restore_path"
            ]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        tampered = tmp_path / f"{mutation}.tar.gz"
        with tarfile.open(tampered, "w:gz") as output:
            output.add(root / "state", arcname="state")

    with pytest.raises(ValueError, match=message):
        RUNTIME_BACKUP.verify_archive(tampered)


def test_runtime_backup_rejects_empty_input_set(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "create",
            "--output",
            str(tmp_path / "empty.tar.gz"),
            "--path",
            str(tmp_path / "missing"),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "missing required backup input" in result.stderr


def test_evidence_and_metadata_are_verified_but_not_restored(tmp_path):
    state = tmp_path / "state.json"
    state.write_text("archived\n", encoding="utf-8")
    evidence = tmp_path / "active-config.yaml"
    evidence.write_text("release: old\n", encoding="utf-8")
    archive = tmp_path / "backup.tar.gz"
    RUNTIME_BACKUP.create_archive(
        archive,
        [],
        [state],
        evidence_paths=[evidence],
        metadata={"runtime_revision": "a" * 40},
    )

    verified = RUNTIME_BACKUP.verify_archive(archive)
    target = tmp_path / "restore-root"
    restored = RUNTIME_BACKUP.restore_archive(archive, target_root=target)

    assert verified["metadata"] == {"runtime_revision": "a" * 40}
    assert restored["metadata"] == verified["metadata"]
    assert restored["restored"] == [Path(*state.parts[1:]).as_posix()]
    assert (target / Path(*state.parts[1:])).read_text(encoding="utf-8") == "archived\n"
    assert not (target / Path(*evidence.parts[1:])).exists()


def test_restore_extracts_and_verifies_archive_only_once(monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    state.write_text("archived\n", encoding="utf-8")
    archive = tmp_path / "backup.tar.gz"
    RUNTIME_BACKUP.create_archive(archive, [], [state])
    original_extract = RUNTIME_BACKUP._extract_archive
    calls = 0

    def count_extract(path, root):
        nonlocal calls
        calls += 1
        if calls > 1:
            path.write_bytes(b"replaced after verification")
        return original_extract(path, root)

    monkeypatch.setattr(RUNTIME_BACKUP, "_extract_archive", count_extract)
    target = tmp_path / "restore-root"
    RUNTIME_BACKUP.restore_archive(archive, target_root=target)

    assert calls == 1
    assert (target / Path(*state.parts[1:])).read_text(encoding="utf-8") == "archived\n"


def test_restore_rolls_back_database_config_and_sidecars_on_mid_promotion_failure(
    tmp_path, monkeypatch
):
    database = tmp_path / "live.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        conn.execute("INSERT INTO sample VALUES ('archived')")
    config = tmp_path / "settings.json"
    config.write_text('{"state": "archived"}\n', encoding="utf-8")
    archive = tmp_path / "backup.tar.gz"
    RUNTIME_BACKUP.create_archive(archive, [database], [config])

    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE sample SET value = 'pre-restore'")
    config.write_text('{"state": "pre-restore"}\n', encoding="utf-8")
    wal = Path(f"{database}-wal")
    wal.write_bytes(b"preserve-this-sidecar")

    original_replace = Path.replace
    promotions = 0

    def fail_second_promotion(path, target):
        nonlocal promotions
        if ".restore-" in path.name and ".pre-restore-" not in path.name:
            promotions += 1
            if promotions == 2:
                raise OSError("injected promotion failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_second_promotion)
    with pytest.raises(OSError, match="injected promotion failure"):
        RUNTIME_BACKUP.restore_archive(archive, target_root=Path("/"), replace=True)

    assert config.read_text(encoding="utf-8") == '{"state": "pre-restore"}\n'
    assert wal.read_bytes() == b"preserve-this-sidecar"
    wal.unlink()
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT value FROM sample").fetchone()[0] == "pre-restore"
    assert not list(tmp_path.glob(".*.pre-restore-*"))


def test_restore_cleans_staged_file_when_copy_fails(tmp_path, monkeypatch):
    state = tmp_path / "settings.json"
    state.write_text("archived\n", encoding="utf-8")
    archive = tmp_path / "backup.tar.gz"
    RUNTIME_BACKUP.create_archive(archive, [], [state])
    original_copy = RUNTIME_BACKUP.shutil.copy2

    def fail_restore_copy(source, destination, *args, **kwargs):
        if ".restore-" in Path(destination).name:
            Path(destination).write_bytes(b"partial")
            raise OSError("injected copy failure")
        return original_copy(source, destination, *args, **kwargs)

    monkeypatch.setattr(RUNTIME_BACKUP.shutil, "copy2", fail_restore_copy)
    with pytest.raises(OSError, match="injected copy failure"):
        RUNTIME_BACKUP.restore_archive(
            archive, target_root=Path("/"), replace=True
        )

    assert state.read_text(encoding="utf-8") == "archived\n"
    assert not list(tmp_path.glob(".*.restore-*"))
