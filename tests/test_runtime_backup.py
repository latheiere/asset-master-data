import json
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "runtime_backup.py"


def test_runtime_backup_round_trip(tmp_path):
    database = tmp_path / "live.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        conn.execute("INSERT INTO sample VALUES ('durable')")
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

    assert json.loads(created.stdout)["entries"]
    assert json.loads(verified.stdout)["entries_checked"] == 2


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
    assert "no backup inputs exist" in result.stderr
