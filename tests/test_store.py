import sqlite3
from importlib import resources

from mdv.db import SQLiteStore, market_trade_url
from mdv.models import MarketRecord, MarketSnapshot


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

    log = store.list_collection_runs(limit=10)
    by_id = {run["collection_run_id"]: run for run in log["runs"]}
    assert by_id[first_parent]["venues"][0]["changes"][0]["message"] == "WIF listed"
    assert by_id[second_parent]["change_count"] == 1
    assert by_id[second_parent]["venues"][0]["changes"][0]["message"] == (
        "WIF added Monitoring tag"
    )


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


def test_mexc_stock_suffix_maps_to_underlying_without_changing_raw_truth(tmp_path):
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
    assert "STOCK_SUFFIX_METADATA" in mapping_method
    assert revision_count == 1
    assert store.list_assets({"stock": "1"})["count"] == 1
    assert store.list_assets({"stock": "0"})["count"] == 0


def test_unverified_stock_suffix_remains_a_separate_candidate(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    apply_market(store, market(source="BINANCE_FAKE", venue="BINANCE", market_type="FUTURE", raw_symbol="FAKEUSDT", base_symbol="FAKE"))
    apply_market(store, market(source="MEXC_FAKE", venue="MEXC", market_type="FUTURE", raw_symbol="FAKESTOCK_USDT", base_symbol="FAKESTOCK"))

    both = store.list_assets({"contract": "PERP", "futures": ["BINANCE", "MEXC"]})
    with store.readonly() as conn:
        candidate = conn.execute(
            """
            SELECT decision, score FROM asset_match_candidates
            WHERE source_market_id = 'MEXC_FAKE:FAKESTOCK_USDT'
            """
        ).fetchone()

    assert both["count"] == 0
    assert tuple(candidate) == ("PROPOSED", 0.0)


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
