import sqlite3
from importlib import resources

import pytest

from mdv.db import (
    CollectionBusyError,
    OutOfOrderSnapshotError,
    SQLiteStore,
    market_trade_url,
)
from mdv.models import FinancingRecord, FinancingSnapshot, MarketRecord, MarketSnapshot


def snapshot(*, active: bool = True, status: str = "TRADING") -> MarketSnapshot:
    row = MarketRecord(
        source="BINANCE_USDM_FUTURE",
        venue="BINANCE",
        market_type="FUTURE",
        product="USD-M",
        raw_symbol="BTCUSDT",
        base_symbol="BTC",
        quote_symbol="USDT",
        settle_symbol="USDT",
        contract_type="PERP",
        status=status,
        active=active,
        contract_multiplier=None,
        raw={"symbol": "BTCUSDT", "status": status},
    )
    return MarketSnapshot(
        source=row.source,
        venue=row.venue,
        market_type=row.market_type,
        product=row.product,
        observed_at="2026-07-03T00:00:00+00:00",
        markets=(row,),
    )


def apply_market(store, market: MarketRecord) -> None:
    store.apply_snapshot(
        MarketSnapshot(
            source=market.source,
            venue=market.venue,
            market_type=market.market_type,
            product=market.product,
            observed_at="2026-07-03T00:00:00+00:00",
            markets=(market,),
        )
    )


def market(
    *,
    source: str,
    venue: str,
    market_type: str,
    raw_symbol: str,
    base_symbol: str = "BTC",
    quote_symbol: str = "USDT",
    active: bool = True,
    product: str | None = None,
    contract_type: str | None = None,
    raw: dict | None = None,
) -> MarketRecord:
    return MarketRecord(
        source=source,
        venue=venue,
        market_type=market_type,
        product=product or ("SPOT" if market_type == "SPOT" else "PERP"),
        raw_symbol=raw_symbol,
        base_symbol=base_symbol,
        quote_symbol=quote_symbol,
        settle_symbol=None if market_type == "SPOT" else quote_symbol,
        contract_type=contract_type or ("SPOT" if market_type == "SPOT" else "PERP"),
        status="TRADING" if active else "BREAK",
        active=active,
        contract_multiplier=None,
        raw=raw or {"symbol": raw_symbol, "base_symbol": base_symbol},
    )
def test_store_applies_snapshot_matches_asset_and_filters(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    store.apply_snapshot(snapshot())
    rows = store.list_markets({"type": "future", "symbol": "BT*", "limit": 10})
    assert len(rows) == 1
    assert rows[0]["canonical_symbol"] == "BTC"
    assert rows[0]["market_type"] == "FUTURE"
    assert rows[0]["product"] == "PERP"
    assert rows[0]["venue_product"] == "USD-M"
    assert rows[0]["contract_direction"] == "LINEAR"
    assert rows[0]["active"] == 1


def test_filter_metadata_describes_filters_and_current_values(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    store.apply_snapshot(snapshot())

    metadata = store.filter_metadata()

    assert metadata["filters"]["TYPE"]["values"] == ["FUTURE"]
    assert metadata["filters"]["CONTRACT"]["values"] == ["PERP"]
    assert metadata["filters"]["CONTRACT"]["deprecated_alias_for"] == "PRODUCT"
    assert metadata["filters"]["FUTURES"]["values"] == ["BINANCE"]
    assert "FUTURES!" not in metadata["filters"]
    assert metadata["filters"]["PRODUCT"]["values"] == ["PERP"]
    assert metadata["filters"]["DIRECTION"]["values"] == ["LINEAR"]
    assert metadata["filters"]["SETTLE"]["values"] == ["USDT"]
    assert metadata["filters"]["SYMBOL"]["wildcard"] == "*"
    assert all(
        definition["operators"] == ["=", "!="]
        for name, definition in metadata["filters"].items()
        if name not in {"LIMIT", "OFFSET"}
    )
    assert metadata["filters"]["LIMIT"]["maximum"] == 5000


def test_store_records_status_transition(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    store.apply_snapshot(snapshot())
    store.apply_snapshot(snapshot(active=False, status="SETTLING"))
    with store.readonly() as conn:
        event_types = [row[0] for row in conn.execute(
            "SELECT event_type FROM market_lifecycle_events ORDER BY rowid"
        )]
    assert event_types == ["DISCOVERED", "DEACTIVATED", "STATUS_CHANGED"]


def test_collection_log_groups_market_and_tag_changes_by_venue(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    untagged = market(
        source="BINANCE_WIF_SPOT",
        venue="BINANCE",
        market_type="SPOT",
        raw_symbol="WIFUSDT",
        base_symbol="WIF",
        raw={
            "symbol": "WIFUSDT",
            "_metadata": {"BINANCE_PRODUCT": {"s": "WIFUSDT", "b": "WIF", "tags": []}},
        },
    )
    first_parent = store.start_collection_run(scope="BINANCE", venues=["BINANCE"])
    store.apply_snapshot(
        MarketSnapshot(
            source=untagged.source,
            venue=untagged.venue,
            market_type=untagged.market_type,
            product=untagged.product,
            observed_at="2026-07-03T00:00:00+00:00",
            markets=(untagged,),
        ),
        collection_run_id=first_parent,
    )
    store.finish_collection_run(first_parent)

    second_parent = store.start_collection_run(scope="BINANCE", venues=["BINANCE"])
    tagged = market(
        source="BINANCE_WIF_SPOT",
        venue="BINANCE",
        market_type="SPOT",
        raw_symbol="WIFUSDT",
        base_symbol="WIF",
        raw={
            "symbol": "WIFUSDT",
            "_metadata": {
                "BINANCE_PRODUCT": {"s": "WIFUSDT", "b": "WIF", "tags": ["Monitoring"]}
            },
        },
    )
    store.apply_snapshot(
        MarketSnapshot(
            source=tagged.source,
            venue=tagged.venue,
            market_type=tagged.market_type,
            product=tagged.product,
            observed_at="2026-07-03T01:00:00+00:00",
            markets=(tagged,),
        ),
        collection_run_id=second_parent,
    )
    store.finish_collection_run(second_parent)

    third_parent = store.start_collection_run(scope="BINANCE", venues=["BINANCE"])
    store.apply_snapshot(
        MarketSnapshot(
            source=untagged.source,
            venue=untagged.venue,
            market_type=untagged.market_type,
            product=untagged.product,
            observed_at="2026-07-04T01:00:00+00:00",
            markets=(untagged,),
        ),
        collection_run_id=third_parent,
    )
    store.finish_collection_run(third_parent)

    replacement = market(
        source="BINANCE_WIF_SPOT",
        venue="BINANCE",
        market_type="SPOT",
        raw_symbol="BONKUSDT",
        base_symbol="BONK",
    )
    fourth_parent = store.start_collection_run(scope="BINANCE", venues=["BINANCE"])
    store.apply_snapshot(
        MarketSnapshot(
            source=replacement.source,
            venue=replacement.venue,
            market_type=replacement.market_type,
            product=replacement.product,
            observed_at="2026-07-05T01:00:00+00:00",
            markets=(replacement,),
        ),
        collection_run_id=fourth_parent,
    )
    store.finish_collection_run(fourth_parent)

    log = store.list_collection_runs(limit=10)
    by_id = {run["collection_run_id"]: run for run in log["runs"]}
    assert by_id[first_parent]["venues"][0]["changes"][0]["message"] == "WIF listed"
    assert by_id[second_parent]["change_count"] == 1
    assert by_id[second_parent]["venues"][0]["changes"][0]["message"] == (
        "WIF added Monitoring tag"
    )
    assert log["filter_options"]["tags"] == ["BINANCE:MONITORING"]

    tag_added = store.list_collection_runs(
        action="TAG_ADDED", tag="binance:monitoring"
    )
    assert tag_added["count"] == 1
    assert tag_added["runs"][0]["collection_run_id"] == second_parent
    assert [
        change["kind"]
        for venue in tag_added["runs"][0]["venues"]
        for change in venue["changes"]
    ] == ["TAG_ADDED"]

    tag_removed = store.list_collection_runs(action="TAG_REMOVED")
    assert tag_removed["count"] == 1
    assert tag_removed["runs"][0]["collection_run_id"] == third_parent
    assert tag_removed["runs"][0]["venues"][0]["changes"][0]["kind"] == "TAG_REMOVED"

    removals = store.list_collection_runs(action="REMOVAL")
    assert removals["count"] == 1
    assert removals["runs"][0]["collection_run_id"] == fourth_parent
    assert removals["runs"][0]["venues"][0]["changes"][0]["message"] == "WIF removed"

    dated = store.list_collection_runs(
        date_from="2026-07-04", date_to="2026-07-04"
    )
    assert dated["count"] == 1
    assert dated["runs"][0]["collection_run_id"] == third_parent
    with pytest.raises(ValueError, match="TAG cannot filter LISTING"):
        store.list_collection_runs(action="LISTING", tag="BINANCE:MONITORING")

    mixed_parent = store.start_collection_run(scope="ALL", venues=["BINANCE", "MEXC"])
    store.apply_snapshot(
        MarketSnapshot(
            source=replacement.source,
            venue=replacement.venue,
            market_type=replacement.market_type,
            product=replacement.product,
            observed_at="2026-07-06T01:00:00+00:00",
            markets=(replacement,),
        ),
        collection_run_id=mixed_parent,
    )
    mexc_market = market(
        source="MEXC_SPOT",
        venue="MEXC",
        market_type="SPOT",
        raw_symbol="ETHUSDT",
        base_symbol="ETH",
    )
    store.apply_snapshot(
        MarketSnapshot(
            source=mexc_market.source,
            venue=mexc_market.venue,
            market_type=mexc_market.market_type,
            product=mexc_market.product,
            observed_at="2026-07-06T01:00:00+00:00",
            markets=(mexc_market,),
        ),
        collection_run_id=mixed_parent,
    )
    store.finish_collection_run(mixed_parent)
    filtered_mixed = store.list_collection_runs(action="LISTING")
    mixed_run = next(
        run for run in filtered_mixed["runs"]
        if run["collection_run_id"] == mixed_parent
    )
    assert [venue["venue"] for venue in mixed_run["venues"]] == ["MEXC"]
    symbol_listing = store.list_collection_runs(action="LISTING", symbol="BONK*")
    assert symbol_listing["count"] == 1
    assert symbol_listing["runs"][0]["collection_run_id"] == fourth_parent
    venue_symbol_listing = store.list_collection_runs(
        action="LISTING", venue="MEXC", symbol="ETH*", product="SPOT"
    )
    assert venue_symbol_listing["count"] == 1
    assert venue_symbol_listing["runs"][0]["collection_run_id"] == mixed_parent
    assert venue_symbol_listing["runs"][0]["venues"][0]["changes"][0]["product"] == "SPOT"
    assert filtered_mixed["filter_options"]["venues"] == ["BINANCE", "MEXC"]

    sorted_parent = store.start_collection_run(scope="BINANCE", venues=["BINANCE"])
    sorted_markets = (
        market(
            source="SORT_PERP",
            venue="BINANCE",
            market_type="FUTURE",
            raw_symbol="AAAUSDT",
            base_symbol="AAA",
        ),
        market(
            source="SORT_DATED",
            venue="BINANCE",
            market_type="FUTURE",
            raw_symbol="BBBUSDT",
            base_symbol="BBB",
            contract_type="DATED",
        ),
        market(
            source="SORT_SPOT",
            venue="BINANCE",
            market_type="SPOT",
            raw_symbol="CCCUSDT",
            base_symbol="CCC",
        ),
    )
    for sorted_market in sorted_markets:
        store.apply_snapshot(
            MarketSnapshot(
                source=sorted_market.source,
                venue=sorted_market.venue,
                market_type=sorted_market.market_type,
                product=sorted_market.product,
                observed_at="2026-07-07T01:00:00+00:00",
                markets=(sorted_market,),
            ),
            collection_run_id=sorted_parent,
        )
    store.finish_collection_run(sorted_parent)
    sorted_run = next(
        run for run in store.list_collection_runs(action="LISTING")["runs"]
        if run["collection_run_id"] == sorted_parent
    )
    assert [
        change["product"] for change in sorted_run["venues"][0]["changes"]
    ] == ["PERP", "DATED", "SPOT"]

    with pytest.raises(ValueError, match="ACTION must be"):
        store.list_collection_runs(action="UNKNOWN")
    with pytest.raises(ValueError, match="SYMBOL cannot filter TAG_ADDED"):
        store.list_collection_runs(action="TAG_ADDED", symbol="WIF")
    with pytest.raises(ValueError, match="PRODUCT must be"):
        store.list_collection_runs(action="LISTING", product="FUTURE")
    with pytest.raises(ValueError, match="DATE_FROM must be on or before DATE_TO"):
        store.list_collection_runs(date_from="2026-07-05", date_to="2026-07-04")


def test_collection_log_records_explicit_no_change_run(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    store.apply_snapshot(snapshot())
    parent = store.start_collection_run(scope="ALL", venues=["BINANCE"])
    store.apply_snapshot(snapshot(), collection_run_id=parent)
    store.finish_collection_run(parent)

    saved = {
        run["collection_run_id"]: run for run in store.list_collection_runs()["runs"]
    }[parent]
    assert saved["change_count"] == 0
    assert saved["venues"][0]["changes"] == []
    assert saved["status"] == "SUCCEEDED"


def test_collection_run_migration_backfills_previous_ingest_runs(tmp_path):
    path = tmp_path / "mdv.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            filename TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    migration_dir = resources.files("mdv.migrations")
    for version in range(1, 5):
        entry = next(item for item in migration_dir.iterdir() if item.name.startswith(f"{version:03d}_"))
        conn.executescript(entry.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_migrations(version, filename, applied_at) VALUES (?, ?, ?)",
            (version, entry.name, "2026-07-03T00:00:00+00:00"),
        )
    conn.executemany(
        """
        INSERT INTO ingest_runs(
            run_id, source, venue, market_type, product, started_at,
            completed_at, status, complete, record_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'SUCCEEDED', 1, ?)
        """,
        [
            ("legacy-mexc-future", "MEXC_FUTURE", "MEXC", "FUTURE", "PERP", "2026-07-02T00:00:00.100000+00:00", "2026-07-02T00:00:01+00:00", 1),
            ("legacy-mexc-spot", "MEXC_SPOT", "MEXC", "SPOT", "SPOT", "2026-07-02T00:00:00.200000+00:00", "2026-07-02T00:00:01+00:00", 2),
            ("legacy-binance-usdm", "BINANCE_USDM_FUTURE", "BINANCE", "FUTURE", "USD-M", "2026-07-02T00:00:00.300000+00:00", "2026-07-02T00:00:01+00:00", 3),
            ("legacy-binance-coinm", "BINANCE_COINM_FUTURE", "BINANCE", "FUTURE", "COIN-M", "2026-07-02T00:00:00.400000+00:00", "2026-07-02T00:00:01+00:00", 4),
            ("legacy-binance-spot", "BINANCE_SPOT", "BINANCE", "SPOT", "SPOT", "2026-07-02T00:00:00.500000+00:00", "2026-07-02T00:00:01+00:00", 5),
        ],
    )
    conn.commit()
    conn.close()

    store = SQLiteStore(path)
    store.migrate()

    with store.readonly() as migrated:
        parents = migrated.execute(
            "SELECT collection_run_id, scope, requested_venues_json, status, universe_count, record_count FROM collection_runs"
        ).fetchall()
        child_parents = {
            row[0] for row in migrated.execute("SELECT collection_run_id FROM ingest_runs")
        }
        market_columns = {
            row[1] for row in migrated.execute("PRAGMA table_info(markets)")
        }
    assert len(parents) == 1
    assert tuple(parents[0][1:]) == (
        "ALL",
        '["BINANCE","MEXC"]',
        "SUCCEEDED",
        5,
        15,
    )
    assert child_parents == {parents[0][0]}
    assert "max_market_order_size" in market_columns
    assert {
        "venue_product", "venue_status", "contract_direction", "expiry_cycle"
    }.issubset(market_columns)


def test_financing_migration_upgrades_schema_12_without_rewriting_market_data(tmp_path):
    path = tmp_path / "schema12.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, filename TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    migration_dir = resources.files("mdv.migrations")
    for version in range(1, 13):
        entry = next(
            item for item in migration_dir.iterdir()
            if item.name.startswith(f"{version:03d}_")
        )
        conn.executescript(entry.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_migrations(version, filename, applied_at) VALUES (?, ?, ?)",
            (version, entry.name, "2026-07-03T00:00:00+00:00"),
        )
    conn.execute("INSERT INTO venues(venue, display_name) VALUES ('BYBIT', 'Bybit')")
    conn.commit()
    conn.close()

    store = SQLiteStore(path)
    store.migrate()

    with store.readonly() as migrated:
        versions = [row[0] for row in migrated.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )]
        tables = {row[0] for row in migrated.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        venue = migrated.execute(
            "SELECT display_name FROM venues WHERE venue = 'BYBIT'"
        ).fetchone()[0]
        asset_columns = {
            row[1] for row in migrated.execute("PRAGMA table_info(assets)")
        }
        indexes = {
            row[0]
            for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
    assert versions[-1] == 17
    assert {
        "financing_products", "financing_observations",
        "financing_lifecycle_events", "financing_asset_mappings",
        "financing_asset_mapping_revisions",
        "manual_asset_actions", "manual_asset_action_tombstones",
    }.issubset(tables)
    assert "is_stock" in asset_columns
    assert {
        "idx_collection_runs_status_started",
        "idx_market_observations_retention",
        "idx_financing_observations_retention",
        "idx_market_observations_evidence_retention",
        "idx_financing_observations_evidence_retention",
    }.issubset(indexes)
    assert "audit_compaction_stats" in tables
    assert venue == "Bybit"


def test_observations_retain_raw_payloads_only_for_lifecycle_changes(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    first = snapshot()
    repeated = MarketSnapshot(
        source=first.source,
        venue=first.venue,
        market_type=first.market_type,
        product=first.product,
        observed_at="2026-07-04T00:00:00+00:00",
        markets=first.markets,
    )
    changed = snapshot(active=False, status="BREAK")
    changed = MarketSnapshot(
        source=changed.source,
        venue=changed.venue,
        market_type=changed.market_type,
        product=changed.product,
        observed_at="2026-07-05T00:00:00+00:00",
        markets=changed.markets,
    )
    store.apply_snapshot(first)
    store.apply_snapshot(repeated)
    store.apply_snapshot(changed)

    record = FinancingRecord(
        source="BYBIT_CRYPTO_LOAN", venue="BYBIT", product="CRYPTO_LOAN",
        asset_role="BORROWABLE", raw_asset_symbol="BTC", eligible=True,
        status="ENABLED", regular_user_tier="VIP0", rates=(), terms=(),
        limits={}, pair_symbols=(), raw={"coin": "BTC"},
    )
    finance_first = FinancingSnapshot(
        source=record.source, venue=record.venue, product=record.product,
        observed_at="2026-07-03T00:00:00+00:00", records=(record,),
    )
    finance_repeat = FinancingSnapshot(
        source=record.source, venue=record.venue, product=record.product,
        observed_at="2026-07-04T00:00:00+00:00", records=(record,),
    )
    store.apply_financing_snapshot(finance_first)
    store.apply_financing_snapshot(finance_repeat)

    with store.readonly() as conn:
        market_payloads = [tuple(row) for row in conn.execute(
            "SELECT raw_retained, raw_json FROM market_observations ORDER BY observed_at"
        )]
        financing_payloads = [tuple(row) for row in conn.execute(
            "SELECT raw_retained, rates_json, raw_json FROM financing_observations ORDER BY observed_at"
        )]
    assert market_payloads[0][0] == 1
    assert market_payloads[1] == (0, "{}")
    assert market_payloads[2][0] == 1
    assert financing_payloads[0][0] == 1
    assert financing_payloads[1] == (0, "[]", "{}")

    deleted = store.compact_audit_history(
        unchanged_retention_days=1, batch_size=1
    )
    with store.readonly() as conn:
        retained_market = conn.execute(
            "SELECT COUNT(*) FROM market_observations WHERE raw_retained = 1"
        ).fetchone()[0]
        retained_financing = conn.execute(
            "SELECT COUNT(*) FROM financing_observations WHERE raw_retained = 1"
        ).fetchone()[0]
        lifecycle = conn.execute(
            "SELECT COUNT(*) FROM market_lifecycle_events"
        ).fetchone()[0]
    assert deleted["market_observations"] == 1
    assert deleted["financing_observations"] == 1
    assert retained_market == 2
    assert retained_financing == 1
    assert lifecycle >= 2


def test_content_only_observation_change_retains_raw_evidence(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    first = snapshot()
    changed_record = MarketRecord(
        **{**first.markets[0].__dict__, "raw": {"symbol": "BTCUSDT", "tickSize": "0.01"}}
    )
    changed = MarketSnapshot(
        source=first.source,
        venue=first.venue,
        market_type=first.market_type,
        product=first.product,
        observed_at="2026-07-04T00:00:00+00:00",
        markets=(changed_record,),
    )

    store.apply_snapshot(first)
    store.apply_snapshot(changed)

    with store.readonly() as conn:
        retained = [
            row[0]
            for row in conn.execute(
                "SELECT raw_retained FROM market_observations ORDER BY observed_at"
            )
        ]
    assert retained == [1, 1]


def test_audit_compaction_bounds_payloads_and_evidence_rows(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    base = snapshot()
    for day in range(3, 7):
        record = MarketRecord(
            **{
                **base.markets[0].__dict__,
                "raw": {"symbol": "BTCUSDT", "revision": day},
            }
        )
        store.apply_snapshot(
            MarketSnapshot(
                source=base.source,
                venue=base.venue,
                market_type=base.market_type,
                product=base.product,
                observed_at=f"2026-07-{day:02d}T00:00:00+00:00",
                markets=(record,),
            )
        )

    result = store.compact_audit_history(
        unchanged_retention_days=30,
        changed_payload_retention_days=1,
        max_retained_observations_per_table=2,
        batch_size=1,
    )

    with store.readonly() as conn:
        observations = conn.execute(
            """
            SELECT raw_json, raw_retained, payload_compacted
            FROM market_observations ORDER BY observed_at
            """
        ).fetchall()
        current_raw = conn.execute(
            "SELECT raw_json FROM markets WHERE market_id = ?",
            (base.markets[0].market_id,),
        ).fetchone()[0]
        lifecycle = conn.execute(
            "SELECT COUNT(*) FROM market_lifecycle_events"
        ).fetchone()[0]
    readiness = store.readiness()

    assert result["market_payloads_compacted"] == 4
    assert result["market_evidence_rows_pruned"] == 2
    assert [tuple(row) for row in observations] == [
        ("{}", 1, 1),
        ("{}", 1, 1),
    ]
    assert '"revision":6' in current_raw
    assert lifecycle == 1
    assert readiness["retained_observations"]["market_observations"] == 2
    stats = readiness["audit_compaction"]["market_observations"]
    assert stats["payloads_compacted"] == 4
    assert stats["evidence_rows_pruned"] == 2
    assert stats["updated_at"] is not None


def test_collection_writer_lease_is_nonblocking_across_store_instances(tmp_path):
    path = tmp_path / "mdv.sqlite3"
    first = SQLiteStore(path)
    second = SQLiteStore(path)

    with first.collection_writer_lease():
        with pytest.raises(CollectionBusyError, match="already running"):
            with second.collection_writer_lease():
                raise AssertionError("second writer unexpectedly acquired the lease")


def test_out_of_order_snapshot_cannot_regress_current_catalog(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    current = snapshot()
    older = MarketSnapshot(
        source=current.source,
        venue=current.venue,
        market_type=current.market_type,
        product=current.product,
        observed_at="2026-07-02T00:00:00+00:00",
        markets=(MarketRecord(
            **{**current.markets[0].__dict__, "active": False, "status": "BREAK"}
        ),),
    )
    store.apply_snapshot(current)

    with pytest.raises(OutOfOrderSnapshotError, match="older than applied"):
        store.apply_snapshot(older)

    assert store.list_markets({})[0]["status"] == "TRADING"
    assert store.list_collection_runs()["count"] == 1


def test_snapshot_order_uses_instants_not_timestamp_text(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    current = snapshot()

    def observed_at(value: str, *, status: str) -> MarketSnapshot:
        record = MarketRecord(
            **{
                **current.markets[0].__dict__,
                "status": status,
                "raw": {"symbol": "BTCUSDT", "status": status},
            }
        )
        return MarketSnapshot(
            source=current.source,
            venue=current.venue,
            market_type=current.market_type,
            product=current.product,
            observed_at=value,
            markets=(record,),
        )

    # Lexically, 08:00+07 sorts after 02:00+00 even though it is an hour older.
    store.apply_snapshot(observed_at("2026-07-03T08:00:00+07:00", status="OPEN"))
    store.apply_snapshot(observed_at("2026-07-03T02:00:00+00:00", status="TRADING"))

    with pytest.raises(OutOfOrderSnapshotError, match="older than applied"):
        store.apply_snapshot(
            observed_at("2026-07-03T01:30:00+00:00", status="BREAK")
        )

    assert store.list_markets({})[0]["status"] == "TRADING"


def test_stale_collection_runs_are_reconciled_and_exposed_by_readiness(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    collection_run_id = store.start_collection_run(scope="BINANCE", venues=["BINANCE"])
    with store.transaction() as conn:
        conn.execute(
            "UPDATE collection_runs SET started_at = ? WHERE collection_run_id = ?",
            ("2000-01-01T00:00:00+00:00", collection_run_id),
        )
        conn.execute(
            """
            INSERT INTO ingest_runs(
                run_id, source, venue, market_type, product, started_at,
                status, complete, collection_run_id
            ) VALUES ('stale-child', 'BINANCE_SPOT', 'BINANCE', 'SPOT',
                      'SPOT', '2000-01-01T00:00:00+00:00', 'RUNNING', 0, ?)
            """,
            (collection_run_id,),
        )

    assert store.reconcile_stale_collection_runs(stale_after_seconds=1) == 1
    readiness = store.readiness(max_collection_age_seconds=60)
    with store.readonly() as conn:
        parent = conn.execute(
            "SELECT status, error FROM collection_runs WHERE collection_run_id = ?",
            (collection_run_id,),
        ).fetchone()
        child = conn.execute(
            "SELECT status, error FROM ingest_runs WHERE run_id = 'stale-child'"
        ).fetchone()
    assert parent[0] == child[0] == "FAILED"
    assert "collector exited" in parent[1]
    assert "collector exited" in child[1]
    assert readiness["running_collections"] == 0
    assert readiness["ready"] is False


def test_reconciled_stale_run_does_not_eclipse_newer_collection(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    stale_run_id = store.start_collection_run(scope="BINANCE", venues=["BINANCE"])
    with store.transaction() as conn:
        conn.execute(
            "UPDATE collection_runs SET started_at = ? WHERE collection_run_id = ?",
            ("2000-01-01T00:00:00+00:00", stale_run_id),
        )

    successful_run_id = store.start_collection_run(
        scope="BINANCE",
        venues=["BINANCE"],
    )
    store.apply_snapshot(snapshot(), collection_run_id=successful_run_id)
    store.finish_collection_run(successful_run_id)

    assert store.reconcile_stale_collection_runs(stale_after_seconds=1) == 1

    readiness = store.readiness(max_collection_age_seconds=60)
    assert readiness["latest_collection"]["collection_run_id"] == successful_run_id
    assert readiness["latest_collection"]["status"] == "SUCCEEDED"
    assert readiness["ready"] is True


def test_asset_pagination_does_not_reparse_raw_market_catalog(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    apply_market(store, market(
        source="BINANCE_BTC", venue="BINANCE", market_type="SPOT",
        raw_symbol="BTCUSDT", base_symbol="BTC",
    ))
    apply_market(store, market(
        source="BINANCE_ETH", venue="BINANCE", market_type="SPOT",
        raw_symbol="ETHUSDT", base_symbol="ETH",
    ))

    def fail_if_reparsed(*_args, **_kwargs):
        raise AssertionError("list_assets reparsed raw catalog metadata")

    monkeypatch.setattr("mdv.db.market_metadata", fail_if_reparsed)
    page = store.list_assets({"limit": 1, "offset": 1})

    assert page["count"] == 2
    assert [asset["canonical_symbol"] for asset in page["assets"]] == ["ETH"]


def test_delivery_manual_mapping_applies_and_local_actions_are_crud(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    apply_market(
        store,
        market(
            source="MEXC_TSEM", venue="MEXC", market_type="FUTURE",
            raw_symbol="TSEMSTOCK_USDT", base_symbol="TSEMSTOCK",
        ),
    )

    assets = store.list_assets({"symbol": "TSEM"})
    assert assets["assets"][0]["canonical_symbol"] == "TSEM"
    with store.readonly() as conn:
        method = conn.execute(
            "SELECT method FROM market_asset_mappings WHERE market_id = 'MEXC_TSEM:TSEMSTOCK_USDT'"
        ).fetchone()[0]
    assert method.startswith("MANUAL_MAP_SYMBOL")

    created = store.create_manual_asset_action({
        "action_type": "MAP_SYMBOL", "venue": "BITGET",
        "source_symbol": "LOCALOLD", "target_symbol": "LOCALNEW",
        "note": "reviewed", "enabled": True,
    })
    assert created["origin"] == "LOCAL"
    updated = store.update_manual_asset_action(created["action_id"], {
        "action_type": "OTHER", "venue": "", "source_symbol": "",
        "target_symbol": "", "note": "retired", "enabled": False,
    })
    assert updated["action_type"] == "OTHER"
    store.delete_manual_asset_action(created["action_id"])
    assert all(row["action_id"] != created["action_id"] for row in store.list_manual_asset_actions())


def test_collection_log_marks_follow_on_same_asset_market_as_listed(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    apply_market(store, market(
        source="OKX_EXPIRY_FUTURE", venue="OKX", market_type="FUTURE",
        raw_symbol="INJ-USD_UM_XPERP-300627", base_symbol="INJ",
        product="DATED", contract_type="DATED",
    ))
    store.apply_snapshot(MarketSnapshot(
        source="OKX_EXPIRY_FUTURE", venue="OKX", market_type="FUTURE",
        product="DATED", observed_at="2026-07-04T00:00:00+00:00",
        markets=(market(
            source="OKX_EXPIRY_FUTURE", venue="OKX", market_type="FUTURE",
            raw_symbol="INJ-USD_UM_XPERP-310711", base_symbol="INJ",
            product="DATED", contract_type="DATED",
        ),),
    ))

    log = store.list_collection_runs(action="LISTING", venue="OKX")
    assert any(
        change["kind"] == "MARKET_LISTED" and change["market"] == "INJ-USD_UM_XPERP-310711"
        for run in log["runs"] for venue in run["venues"] for change in venue["changes"]
    )


def test_asset_view_groups_active_markets_and_reports_cross_venue_futures(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    apply_market(store, market(source="BINANCE_SPOT", venue="BINANCE", market_type="SPOT", raw_symbol="BTCUSDT"))
    apply_market(store, market(source="BINANCE_FUTURE", venue="BINANCE", market_type="FUTURE", raw_symbol="BTCUSDT", product="USD-M"))
    apply_market(store, market(source="MEXC_FUTURE", venue="MEXC", market_type="FUTURE", raw_symbol="BTC_USDT"))
    apply_market(store, market(source="MEXC_SPOT", venue="MEXC", market_type="SPOT", raw_symbol="BTCUSDT", active=False))

    view = store.list_assets({"type": "FUTURE"})

    assert view["count"] == 1
    asset = view["assets"][0]
    assert asset["canonical_symbol"] == "BTC"
    assert asset["future_coverage"] == "BOTH · 2/2"
    assert [item["venue"] for item in asset["future_venues"]] == ["BINANCE", "MEXC"]
    assert asset["perp_venues"] == [
        {"venue": "BINANCE", "count": 1},
        {"venue": "MEXC", "count": 1},
    ]
    assert asset["dated_venues"] == []
    assert asset["margin_venues"] == []
    assert asset["loan_venues"] == []
    assert asset["spot_venues"] == [{"venue": "BINANCE", "count": 1}]
    assert asset["active_market_count"] == 3
    assert all(row["status"] != "BREAK" for row in asset["markets"])


def test_asset_view_separates_venue_symbol_from_underlying_unit(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    apply_market(
        store,
        market(
            source="BINANCE_SPOT",
            venue="BINANCE",
            market_type="SPOT",
            raw_symbol="BONKUSDT",
            base_symbol="BONK",
        ),
    )
    apply_market(
        store,
        market(
            source="BINANCE_FUTURE",
            venue="BINANCE",
            market_type="FUTURE",
            raw_symbol="1000BONKUSDT",
            base_symbol="1000BONK",
            product="USD-M",
        ),
    )

    asset = store.list_assets({"symbol": "BONK"})["assets"][0]
    future = asset["venues"][0]["futures"][0]
    assert asset["canonical_symbol"] == "BONK"
    assert asset["venue_symbols"] == [{"venue": "BINANCE", "symbols": ["1000BONK", "BONK"]}]
    assert future["underlying_unit"] == "1000 BONK"


def test_raw_market_api_defaults_to_active_only(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    apply_market(store, market(source="BINANCE_SPOT", venue="BINANCE", market_type="SPOT", raw_symbol="BTCUSDT", active=False))

    assert store.list_markets({}) == []
    assert len(store.list_markets({"active": "false"})) == 1


def test_short_future_filters_support_required_and_excluded_venues(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    apply_market(store, market(source="BINANCE_BTC", venue="BINANCE", market_type="FUTURE", raw_symbol="BTCUSDT", base_symbol="BTC"))
    apply_market(store, market(source="MEXC_BTC", venue="MEXC", market_type="FUTURE", raw_symbol="BTC_USDT", base_symbol="BTC"))
    apply_market(store, market(source="BINANCE_ETH", venue="BINANCE", market_type="FUTURE", raw_symbol="ETHUSDT", base_symbol="ETH"))
    apply_market(store, market(source="MEXC_SOL", venue="MEXC", market_type="FUTURE", raw_symbol="SOL_USDT", base_symbol="SOL"))
    apply_market(
        store,
        market(
            source="BINANCE_BNB_DELIVERY",
            venue="BINANCE",
            market_type="FUTURE",
            raw_symbol="BNBUSDT_260925",
            base_symbol="BNB",
            contract_type="CQ",
        ),
    )

    both = store.list_assets({"contract": "PERP", "futures": ["BINANCE", "MEXC"]})
    binance_optional = store.list_assets({"contract": "PERP", "futures": ["BINANCE"]})
    binance_only = store.list_assets(
        {"contract": "PERP", "futures": ["BINANCE"], "futures_not": ["MEXC"]}
    )
    current_quarter = store.list_assets({"contract": "CQ"})

    assert [asset["canonical_symbol"] for asset in both["assets"]] == ["BTC"]
    assert [asset["canonical_symbol"] for asset in binance_optional["assets"]] == ["BTC", "ETH"]
    assert [asset["canonical_symbol"] for asset in binance_only["assets"]] == ["ETH"]
    assert all(asset["canonical_symbol"] != "BNB" for asset in binance_optional["assets"])
    assert [asset["canonical_symbol"] for asset in current_quarter["assets"]] == ["BNB"]
    metadata = store.filter_metadata()["filters"]
    assert metadata["CONTRACT"]["values"] == ["DATED", "PERP"]
    assert metadata["EXPIRY"]["values"] == ["Q"]


def test_normalized_dimensions_support_equal_and_not_equal_filters(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    perpetual = market(
        source="BINANCE_BTC",
        venue="BINANCE",
        market_type="FUTURE",
        raw_symbol="BTCUSDT",
        base_symbol="BTC",
        product="USD-M",
    )
    dated = market(
        source="GATE_ETH_DELIVERY",
        venue="GATE",
        market_type="FUTURE",
        raw_symbol="ETH_USDC_20260710",
        base_symbol="ETH",
        quote_symbol="USDC",
        product="USDC-DELIVERY",
        contract_type="DATED",
    )
    dated = MarketRecord(
        **{
            **dated.__dict__,
            "settle_symbol": "USDC",
            "expiry_cycle": "W",
            "venue_product": "USDC-DELIVERY",
        }
    )
    apply_market(store, perpetual)
    apply_market(store, dated)

    assert [row["raw_symbol"] for row in store.list_markets({"product": "PERP"})] == [
        "BTCUSDT"
    ]
    assert [row["raw_symbol"] for row in store.list_markets({"product_not": "PERP"})] == [
        "ETH_USDC_20260710"
    ]
    assert store.list_markets({"expiry": "W"})[0]["contract_type"] == "DATED"
    assert store.list_markets({"quote": "USDC"})[0]["settle_symbol"] == "USDC"
    assert store.list_markets({"settle_not": "USDT"})[0]["raw_symbol"] == "ETH_USDC_20260710"
    assert store.list_markets({"direction": "LINEAR"})[0]["status"] == "TRADING"

    assert [asset["canonical_symbol"] for asset in store.list_assets({"venue_not": "GATE"})["assets"]] == ["BTC"]
    assert [asset["canonical_symbol"] for asset in store.list_assets({"product_not": "PERP"})["assets"]] == ["ETH"]
    assert [asset["canonical_symbol"] for asset in store.list_assets({"expiry": "W"})["assets"]] == ["ETH"]
    assert [asset["canonical_symbol"] for asset in store.list_assets({"quote_not": "USDT"})["assets"]] == ["ETH"]
    assert [asset["canonical_symbol"] for asset in store.list_assets({"symbol_not": "BTC*"})["assets"]] == ["ETH"]


def test_unit_prefixed_spot_and_future_symbols_share_one_asset(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    apply_market(store, market(source="BINANCE_SATS_SPOT", venue="BINANCE", market_type="SPOT", raw_symbol="1000SATSUSDT", base_symbol="1000SATS"))
    apply_market(store, market(source="BINANCE_SATS_FUTURE", venue="BINANCE", market_type="FUTURE", raw_symbol="1000SATSUSDT", base_symbol="1000SATS"))
    apply_market(store, market(source="MEXC_SATS_SPOT", venue="MEXC", market_type="SPOT", raw_symbol="SATSUSDT", base_symbol="SATS"))
    apply_market(store, market(source="MEXC_SATS_FUTURE", venue="MEXC", market_type="FUTURE", raw_symbol="SATS_USDT", base_symbol="SATS"))

    view = store.list_assets({"symbol": "*SATS"})

    assert view["count"] == 1
    assert view["assets"][0]["canonical_symbol"] == "SATS"
    assert view["assets"][0]["future_coverage"] == "BOTH · 2/2"


def test_stock_suffix_policy_maps_to_underlying_without_changing_raw_truth(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    apply_market(
        store,
        market(
            source="BINANCE_AMAT",
            venue="BINANCE",
            market_type="FUTURE",
            raw_symbol="AMATUSDT",
            base_symbol="AMAT",
            raw={
                "symbol": "AMATUSDT",
                "baseAsset": "AMAT",
                "contractType": "TRADIFI_PERPETUAL",
                "underlyingType": "EQUITY",
            },
        ),
    )
    apply_market(
        store,
        market(
            source="MEXC_AMAT",
            venue="MEXC",
            market_type="FUTURE",
            raw_symbol="AMATSTOCK_USDT",
            base_symbol="AMATSTOCK",
            raw={
                "symbol": "AMATSTOCK_USDT",
                "baseCoin": "AMATSTOCK",
                "displayNameEn": "AMAT_USDT PERPETUAL",
                "conceptPlate": ["mc-trade-zone-Stock", "mc-trade-zone-tradfi"],
                "indexOrigin": ["BINANCE_FUTURE", "BINANCETICKER"],
            },
        ),
    )

    view = store.list_assets({"contract": "PERP", "futures": ["BINANCE", "MEXC"]})

    assert view["count"] == 1
    asset = view["assets"][0]
    assert asset["canonical_symbol"] == "AMAT"
    assert asset["venue_symbols"] == [
        {"venue": "BINANCE", "symbols": ["AMAT"]},
        {"venue": "MEXC", "symbols": ["AMATSTOCK"]},
    ]
    with store.readonly() as conn:
        raw_market = conn.execute(
            "SELECT base_symbol, raw_symbol FROM markets WHERE market_id = 'MEXC_AMAT:AMATSTOCK_USDT'"
        ).fetchone()
        mapping_method = conn.execute(
            "SELECT method FROM market_asset_mappings WHERE market_id = 'MEXC_AMAT:AMATSTOCK_USDT'"
        ).fetchone()[0]
        revision_count = conn.execute(
            "SELECT COUNT(*) FROM market_asset_mapping_revisions WHERE market_id = 'MEXC_AMAT:AMATSTOCK_USDT'"
        ).fetchone()[0]
    assert tuple(raw_market) == ("AMATSTOCK", "AMATSTOCK_USDT")
    assert mapping_method == "STOCK_SUFFIX_POLICY+CROSS_VENUE_SYMBOL"
    assert revision_count == 1
    assert store.list_assets({"stock": "1"})["count"] == 1
    assert store.list_assets({"stock": "0"})["count"] == 0


def test_stock_suffix_policy_maps_all_market_and_financing_types(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    apply_market(store, market(source="MEXC_FAKE", venue="MEXC", market_type="FUTURE", raw_symbol="FAKESTOCK_USDT", base_symbol="FAKESTOCK"))
    apply_market(store, market(source="HTX_FAKE", venue="HTX", market_type="FUTURE", raw_symbol="FAKESTOCK-USDT", base_symbol="FAKESTOCK"))
    apply_market(store, market(source="HTX_FAKE_SPOT", venue="HTX", market_type="SPOT", raw_symbol="FAKESTOCKUSDC", base_symbol="FAKESTOCK", quote_symbol="USDC"))
    financing_record = FinancingRecord(
        source="HTX_CROSS_MARGIN", venue="HTX", product="CROSS_MARGIN",
        asset_role="BORROWABLE", raw_asset_symbol="FAKE", eligible=True,
        status="ENABLED", regular_user_tier=None, rates=(), terms=(), limits={},
        pair_symbols=(), raw={"asset": "FAKE"},
    )
    store.apply_financing_snapshot(FinancingSnapshot(
        source=financing_record.source, venue=financing_record.venue,
        product=financing_record.product, observed_at="2026-07-04T00:00:00+00:00",
        records=(financing_record,),
    ))

    asset_view = store.list_assets({"symbol": "FAKE"})
    with store.readonly() as conn:
        candidates = conn.execute(
            """
            SELECT source_market_id, decision, score, rule FROM asset_match_candidates
            WHERE rule = 'STOCK_SUFFIX_POLICY'
            ORDER BY source_market_id
            """
        ).fetchall()
        mappings = conn.execute(
            """
            SELECT normalized_symbol, method, confidence FROM market_asset_mappings
            WHERE market_id IN (
                'MEXC_FAKE:FAKESTOCK_USDT',
                'HTX_FAKE:FAKESTOCK-USDT',
                'HTX_FAKE_SPOT:FAKESTOCKUSDC'
            )
            ORDER BY market_id
            """
        ).fetchall()
        financing_mapping = conn.execute(
            """
            SELECT normalized_symbol, method, confidence FROM financing_asset_mappings
            WHERE financing_id = 'HTX_CROSS_MARGIN:CROSS_MARGIN:BORROWABLE:FAKE'
            """
        ).fetchone()

    assert asset_view["count"] == 1
    assert asset_view["assets"][0]["canonical_symbol"] == "FAKE"
    assert [tuple(candidate) for candidate in candidates] == [
        ("HTX_FAKE:FAKESTOCK-USDT", "ACCEPTED", 1.0, "STOCK_SUFFIX_POLICY"),
        ("HTX_FAKE_SPOT:FAKESTOCKUSDC", "ACCEPTED", 1.0, "STOCK_SUFFIX_POLICY"),
        ("MEXC_FAKE:FAKESTOCK_USDT", "ACCEPTED", 1.0, "STOCK_SUFFIX_POLICY"),
    ]
    assert [tuple(mapping) for mapping in mappings] == [
        ("FAKE", "STOCK_SUFFIX_POLICY+SAME_VENUE_SPOT_FUTURE_SYMBOL", 1.0),
        ("FAKE", "STOCK_SUFFIX_POLICY+SAME_VENUE_SPOT_FUTURE_SYMBOL", 1.0),
        ("FAKE", "STOCK_SUFFIX_POLICY+SAME_VENUE_SPOT_FUTURE_SYMBOL", 1.0),
    ]
    assert tuple(financing_mapping) == (
        "FAKE", "SAME_VENUE_MARKET_SYMBOL+STOCK_SUFFIX_POLICY", 1.0
    )


def test_stock_suffix_alias_can_use_any_declared_reference_venue(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    apply_market(
        store,
        market(
            source="BYBIT_AMAT",
            venue="BYBIT",
            market_type="FUTURE",
            raw_symbol="AMATUSDT",
            base_symbol="AMAT",
            raw={"symbol": "AMATUSDT", "symbolType": "stock"},
        ),
    )
    apply_market(
        store,
        market(
            source="MEXC_AMAT",
            venue="MEXC",
            market_type="FUTURE",
            raw_symbol="AMATSTOCK_USDT",
            base_symbol="AMATSTOCK",
            raw={
                "symbol": "AMATSTOCK_USDT",
                "displayNameEn": "AMAT_USDT PERPETUAL",
                "conceptPlate": ["Stock"],
                "indexOrigin": ["BYBIT_FUTURE"],
            },
        ),
    )

    view = store.list_assets({"futures": ["BYBIT", "MEXC"]})

    assert view["count"] == 1
    assert view["assets"][0]["canonical_symbol"] == "AMAT"


def test_market_projection_has_exact_venue_trade_links(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    apply_market(store, market(source="BINANCE_LINK", venue="BINANCE", market_type="FUTURE", raw_symbol="WIFUSDT", base_symbol="WIF", product="USD-M"))
    apply_market(store, market(source="MEXC_LINK", venue="MEXC", market_type="FUTURE", raw_symbol="WIF_USDT", base_symbol="WIF", product="PERP"))
    apply_market(store, market(source="BINANCE_LINK_SPOT", venue="BINANCE", market_type="SPOT", raw_symbol="WIFUSDC", base_symbol="WIF", quote_symbol="USDC", product="SPOT", contract_type="SPOT"))
    apply_market(store, market(source="MEXC_LINK_SPOT", venue="MEXC", market_type="SPOT", raw_symbol="WIFUSDT", base_symbol="WIF", product="SPOT", contract_type="SPOT"))
    apply_market(store, market(source="BYBIT_LINK", venue="BYBIT", market_type="FUTURE", raw_symbol="WIFUSDT", base_symbol="WIF", product="LINEAR"))
    apply_market(store, market(source="BYBIT_LINK_SPOT", venue="BYBIT", market_type="SPOT", raw_symbol="WIFUSDT", base_symbol="WIF", product="SPOT", contract_type="SPOT"))

    asset = store.list_assets({"symbol": "WIF"})["assets"][0]
    urls = {
        (market_row["venue"], market_row["market_type"]): market_row["trade_url"]
        for venue in asset["venues"]
        for market_row in [*venue["spot"], *venue["futures"]]
    }

    assert urls == {
        ("BINANCE", "FUTURE"): "https://www.binance.com/en/futures/WIFUSDT",
        ("BINANCE", "SPOT"): "https://www.binance.com/en/trade/WIF_USDC?type=spot",
        ("BYBIT", "FUTURE"): "https://www.bybit.com/trade/usdt/WIFUSDT",
        ("BYBIT", "SPOT"): "https://www.bybit.com/en/trade/spot/WIF/USDT",
        ("MEXC", "FUTURE"): "https://www.mexc.com/futures/WIF_USDT",
        ("MEXC", "SPOT"): "https://www.mexc.com/exchange/WIF_USDT",
    }


def test_gate_and_bitget_trade_links_use_exact_product_routes():
    assert market_trade_url(
        {
            "venue": "GATE",
            "market_type": "FUTURE",
            "product": "DATED",
            "venue_product": "USDT-DELIVERY",
            "raw_symbol": "SOL_USDT_20260710",
            "base_symbol": "SOL",
            "quote_symbol": "USDT",
            "settle_symbol": "USDT",
        }
    ) == "https://www.gate.com/en/futures-delivery/usdt/SOL_USDT_20260710"
    assert market_trade_url(
        {
            "venue": "GATE",
            "market_type": "FUTURE",
            "product": "PERP",
            "venue_product": "USDT-PERP",
            "raw_symbol": "BTC_USDT",
            "settle_symbol": "USDT",
        }
    ) == "https://www.gate.com/futures/USDT/BTC_USDT"
    assert market_trade_url(
        {
            "venue": "BITGET",
            "market_type": "FUTURE",
            "product": "PERP",
            "venue_product": "COIN-M",
            "raw_symbol": "BTCUSD",
        }
    ) == "https://www.bitget.com/futures/coin/BTCUSD"
    assert market_trade_url(
        {
            "venue": "BYBIT",
            "market_type": "FUTURE",
            "product": "PERP",
            "venue_product": "INVERSE",
            "raw_symbol": "BTCUSD",
            "settle_symbol": "BTC",
        }
    ) == "https://www.bybit.com/trade/inverse/BTCUSD"
    assert market_trade_url(
        {
            "venue": "BITGET",
            "market_type": "SPOT",
            "raw_symbol": "BTCUSDT",
        }
    ) == "https://www.bitget.com/spot/BTCUSDT"


def test_dated_future_expiration_is_migrated_stored_and_filterable(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    dated = market(
        source="BYBIT_LINEAR_FUTURE",
        venue="BYBIT",
        market_type="FUTURE",
        raw_symbol="BTCUSDT-25SEP26",
        product="LINEAR",
        contract_type="DATED",
    )
    dated = MarketRecord(
        **{**dated.__dict__, "expires_at": "2026-09-25T00:00:00+00:00"}
    )
    apply_market(store, dated)

    raw = store.list_markets({"type": "FUTURE"})[0]
    assets = store.list_assets({"contract": "DATED"})

    assert raw["expires_at"] == "2026-09-25T00:00:00+00:00"
    assert assets["assets"][0]["markets"][0]["expires_at"] == raw["expires_at"]
    assert store.filter_metadata()["filters"]["CONTRACT"]["values"] == ["DATED"]


def test_max_market_order_size_migrates_and_is_projected(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    future = market(
        source="BINANCE_USDM_FUTURE",
        venue="BINANCE",
        market_type="FUTURE",
        raw_symbol="BTCUSDT",
        product="USD-M",
    )
    future = MarketRecord(
        **{**future.__dict__, "max_market_order_size": "250.000"}
    )
    apply_market(store, future)

    raw = store.list_markets({"type": "FUTURE"})[0]
    asset_market = store.list_assets({"type": "FUTURE"})["assets"][0]["markets"][0]

    assert raw["max_market_order_size"] == "250.000"
    assert asset_market["max_market_order_size"] == raw["max_market_order_size"]


def test_bybit_stock_symbol_type_classifies_canonical_asset(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    apply_market(
        store,
        market(
            source="BYBIT_LINEAR_FUTURE",
            venue="BYBIT",
            market_type="FUTURE",
            raw_symbol="AAPLUSDT",
            base_symbol="AAPL",
            product="LINEAR",
            raw={"symbol": "AAPLUSDT", "symbolType": "stock"},
        ),
    )

    assert store.list_assets({"stock": "1"})["assets"][0]["canonical_symbol"] == "AAPL"


def test_binance_tags_belong_to_canonical_asset_and_are_versioned(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    tagged = market(
        source="BINANCE_WIF_SPOT",
        venue="BINANCE",
        market_type="SPOT",
        raw_symbol="WIFUSDT",
        base_symbol="WIF",
        raw={
            "symbol": "WIFUSDT",
            "baseAsset": "WIF",
            "_metadata": {
                "BINANCE_PRODUCT": {
                    "s": "WIFUSDT",
                    "b": "WIF",
                    "tags": ["Monitoring", "Seed", "Solana", "Meme"],
                }
            },
        },
    )
    apply_market(store, tagged)

    view = store.list_assets({"tags": ["BINANCE:MONITORING", "BINANCE:SEED"]})

    assert view["count"] == 1
    assert view["assets"][0]["canonical_symbol"] == "WIF"
    assert [tag["key"] for tag in view["assets"][0]["tags"]] == [
        "BINANCE:MEME",
        "BINANCE:MONITORING",
        "BINANCE:SEED",
        "BINANCE:SOLANA",
    ]
    assert store.list_markets({"tags": ["BINANCE:MONITORING"]})[0]["canonical_symbol"] == "WIF"

    apply_market(
        store,
        market(
            source="BINANCE_WIF_SPOT",
            venue="BINANCE",
            market_type="SPOT",
            raw_symbol="WIFUSDT",
            base_symbol="WIF",
            raw={
                "symbol": "WIFUSDT",
                "baseAsset": "WIF",
                "_metadata": {"BINANCE_PRODUCT": {"s": "WIFUSDT", "b": "WIF", "tags": []}},
            },
        ),
    )
    assert store.list_assets({"tags": ["BINANCE:MONITORING"]})["count"] == 0
    with store.readonly() as conn:
        events = [tuple(row) for row in conn.execute(
            "SELECT tag, event_type FROM asset_tag_events ORDER BY tag, event_type"
        )]
    assert events == [
        ("MEME", "ADDED"), ("MEME", "REMOVED"),
        ("MONITORING", "ADDED"), ("MONITORING", "REMOVED"),
        ("SEED", "ADDED"), ("SEED", "REMOVED"),
        ("SOLANA", "ADDED"), ("SOLANA", "REMOVED"),
    ]


def test_generic_provider_tags_are_projected_and_remain_provider_scoped(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    provider_markets = [
        market(
            source="GATE_WIF_SPOT",
            venue="GATE",
            market_type="SPOT",
            raw_symbol="WIF_USDT",
            base_symbol="WIF",
            raw={
                "id": "WIF_USDT",
                "_metadata": {
                    "ASSET_TAGS": [
                        {"provider": "GATE", "tag": "ST", "raw_tag": "ST", "source": "GATE_SPOT_CURRENCY_PAIR"}
                    ]
                },
            },
        ),
        market(
            source="BITGET_WIF_SPOT",
            venue="BITGET",
            market_type="SPOT",
            raw_symbol="WIFUSDT",
            base_symbol="WIF",
            raw={
                "symbol": "WIFUSDT",
                "_metadata": {
                    "ASSET_TAGS": [
                        {"provider": "BITGET", "tag": "AREA", "raw_tag": "Area", "source": "BITGET_SPOT_SYMBOL"}
                    ]
                },
            },
        ),
        market(
            source="BITGET_WIF_FUTURE",
            venue="BITGET",
            market_type="FUTURE",
            raw_symbol="WIFUSDT",
            base_symbol="WIF",
            product="USDT-M",
            raw={
                "symbol": "WIFUSDT",
                "_metadata": {
                    "ASSET_TAGS": [
                        {"provider": "BITGET", "tag": "RWA", "raw_tag": "RWA", "source": "BITGET_FUTURE_CONTRACT"}
                    ]
                },
            },
        ),
    ]
    for provider_market in provider_markets:
        apply_market(store, provider_market)

    view = store.list_assets({"tags": ["BITGET:AREA", "BITGET:RWA", "GATE:ST"]})

    assert view["count"] == 1
    assert [tag["key"] for tag in view["assets"][0]["tags"]] == [
        "BITGET:AREA",
        "BITGET:RWA",
        "GATE:ST",
    ]


def test_financing_snapshots_map_by_venue_and_project_separate_products(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    apply_market(store, market(
        source="BYBIT_BTC_SPOT", venue="BYBIT", market_type="SPOT",
        raw_symbol="BTCUSDT", base_symbol="BTC",
    ))
    records = (
        FinancingRecord(
            source="BYBIT_CROSS_MARGIN", venue="BYBIT", product="CROSS_MARGIN",
            asset_role="BORROWABLE", raw_asset_symbol="BTC", eligible=True,
            status="ENABLED", regular_user_tier="No VIP",
            rates=({"tier": "No VIP", "regular_user": True, "rate_type": "VARIABLE", "rate_unit": "HOURLY", "value": "0.000001"},),
            terms=(), limits={}, pair_symbols=(), raw={"currency": "BTC"},
        ),
    )
    store.apply_financing_snapshot(FinancingSnapshot(
        source="BYBIT_CROSS_MARGIN", venue="BYBIT", product="CROSS_MARGIN",
        observed_at="2026-07-05T00:00:00+00:00", records=records,
    ))
    loan_record = FinancingRecord(
        source="BYBIT_CRYPTO_LOAN", venue="BYBIT", product="CRYPTO_LOAN",
        asset_role="BORROWABLE", raw_asset_symbol="BTC", eligible=True,
        status="ENABLED", regular_user_tier="VIP0",
        rates=({"tier": "VIP0", "regular_user": True, "rate_type": "FLEXIBLE", "rate_unit": "APR", "value": "0.04"},),
        terms=({"type": "FLEXIBLE", "enabled": True},),
        limits={"platform_max": "10"}, pair_symbols=(), raw={"currency": "BTC"},
    )
    store.apply_financing_snapshot(FinancingSnapshot(
        source="BYBIT_CRYPTO_LOAN", venue="BYBIT", product="CRYPTO_LOAN",
        observed_at="2026-07-05T00:01:00+00:00", records=(loan_record,),
    ))

    financing = store.list_financing({})
    asset = store.list_assets({"symbol": "BTC"})["assets"][0]

    assert financing["count"] == 2
    assert {row["product"] for row in financing["financing"]} == {
        "CROSS_MARGIN", "CRYPTO_LOAN"
    }
    assert all(row["canonical_symbol"] == "BTC" for row in financing["financing"])
    assert {row["product"] for row in asset["borrow_eligibility"]} == {
        "CROSS_MARGIN", "CRYPTO_LOAN"
    }
    assert asset["borrow_eligibility"][1]["regular_rate"]["value"] == "0.04"
    assert "raw" not in asset["borrow_eligibility"][0]
    assert store.list_assets({"financing": ["BYBIT:MARGIN"]})["count"] == 1
    assert store.list_assets({"financing": ["BYBIT:LOAN"]})["count"] == 1
    assert store.list_assets(
        {"financing": ["BYBIT:MARGIN", "BYBIT:LOAN"]}
    )["count"] == 1
    assert store.list_assets({"financing_not": ["BYBIT:LOAN"]})["count"] == 0
    assert store.filter_metadata()["filters"]["FINANCING"]["values"] == [
        "BYBIT:LOAN", "BYBIT:MARGIN"
    ]
    with pytest.raises(ValueError, match="FINANCING must use"):
        store.list_assets({"financing": ["MARGIN"]})


def test_financing_complete_snapshot_marks_absent_records_inactive(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    record = FinancingRecord(
        source="GATE_CROSS_MARGIN", venue="GATE", product="CROSS_MARGIN",
        asset_role="BORROWABLE", raw_asset_symbol="BTC", eligible=True,
        status="ENABLED", regular_user_tier=None, rates=(), terms=(), limits={},
        pair_symbols=(), raw={"name": "BTC"},
    )
    store.apply_financing_snapshot(FinancingSnapshot(
        source=record.source, venue=record.venue, product=record.product,
        observed_at="2026-07-05T00:00:00+00:00", records=(record,),
    ))
    replacement = FinancingRecord(
        source=record.source, venue=record.venue, product=record.product,
        asset_role="BORROWABLE", raw_asset_symbol="ETH", eligible=True,
        status="ENABLED", regular_user_tier=None, rates=(), terms=(), limits={},
        pair_symbols=(), raw={"name": "ETH"},
    )
    store.apply_financing_snapshot(FinancingSnapshot(
        source=record.source, venue=record.venue, product=record.product,
        observed_at="2026-07-05T01:00:00+00:00", records=(replacement,),
    ))

    assert [row["raw_asset_symbol"] for row in store.list_financing({})["financing"]] == ["ETH"]
    with store.readonly() as conn:
        event = conn.execute(
            "SELECT event_type FROM financing_lifecycle_events WHERE financing_id = ? ORDER BY observed_at DESC LIMIT 1",
            (record.financing_id,),
        ).fetchone()
    assert event[0] == "MISSING"
