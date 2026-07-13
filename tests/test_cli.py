from __future__ import annotations

import json

import pytest
import yaml

from mdv.cli import build_parser, main
from mdv.db import SQLiteStore
from mdv.models import MarketRecord, MarketSnapshot


def test_init_config_entitlement_and_doctor_work_outside_checkout(tmp_path, capsys):
    config_path = tmp_path / "xdg" / "config.yaml"

    assert main(["--config", str(config_path), "init-config"]) == 0
    initialized = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert initialized["collection"]["max_concurrent_fetches"] == 2
    assert initialized["audit"]["unchanged_observation_retention_days"] == 30
    assert initialized["audit"]["changed_payload_retention_days"] == 7
    assert initialized["audit"]["max_retained_observations_per_table"] == 100000
    assert initialized["auth"]["failed_attempt_limit"] == 10

    database = tmp_path / "state" / "mdv.sqlite3"
    entitlements = tmp_path / "xdg" / "entitlements.yaml"
    initialized["database"]["path"] = str(database)
    initialized["auth"]["entitlements_path"] = str(entitlements)
    initialized["collection"]["readiness_max_age_seconds"] = 0
    config_path.write_text(yaml.safe_dump(initialized), encoding="utf-8")
    password_file = tmp_path / "password"
    password_file.write_text("secret\n", encoding="utf-8")

    assert main([
        "--config", str(config_path), "entitlement", "reader",
        "--password-file", str(password_file), "--role", "reader",
    ]) == 0
    assert entitlements.stat().st_mode & 0o777 == 0o600
    saved_user = yaml.safe_load(entitlements.read_text(encoding="utf-8"))["users"]["reader"]
    assert saved_user["role"] == "reader"

    store = SQLiteStore(database)
    record = MarketRecord(
        source="BINANCE_SPOT", venue="BINANCE", market_type="SPOT",
        product="SPOT", raw_symbol="BTCUSDT", base_symbol="BTC",
        quote_symbol="USDT", settle_symbol=None, contract_type="SPOT",
        status="TRADING", active=True, contract_multiplier=None,
        raw={"symbol": "BTCUSDT"},
    )
    store.apply_snapshot(MarketSnapshot(
        source=record.source, venue=record.venue, market_type=record.market_type,
        product=record.product, observed_at="2026-07-14T00:00:00+00:00",
        markets=(record,),
    ))

    capsys.readouterr()
    assert main(["--config", str(config_path), "doctor", "--require-ready"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["entitlements_private"] is True
    assert report["readiness"]["active_markets"] == 1


def test_serve_has_no_collection_switch():
    with pytest.raises(SystemExit, match="2"):
        build_parser().parse_args(["serve", "--refresh"])


def test_init_config_uses_random_atomic_output_and_rejects_symlinks(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep\n", encoding="utf-8")
    config = tmp_path / "config.yaml"
    planted_temporary = tmp_path / "config.yaml.tmp"
    planted_temporary.symlink_to(victim)

    assert main(["--config", str(config), "init-config"]) == 0
    assert victim.read_text(encoding="utf-8") == "keep\n"
    assert planted_temporary.is_symlink()
    assert config.stat().st_mode & 0o777 == 0o600

    linked_config = tmp_path / "linked.yaml"
    linked_config.symlink_to(victim)
    with pytest.raises(ValueError, match="must not be a symlink"):
        main(["--config", str(linked_config), "init-config", "--force"])
    assert victim.read_text(encoding="utf-8") == "keep\n"


def test_force_init_detaches_existing_hardlink(tmp_path):
    original = tmp_path / "original.yaml"
    original.write_text("keep\n", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.hardlink_to(original)

    assert main(["--config", str(config), "init-config", "--force"]) == 0

    assert original.read_text(encoding="utf-8") == "keep\n"
    assert config.read_text(encoding="utf-8") != "keep\n"
    assert config.stat().st_ino != original.stat().st_ino
