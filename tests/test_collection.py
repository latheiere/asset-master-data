import asyncio

import pytest

from mdv.cli import build_parser
from mdv.collection import CollectionService
from mdv.db import SQLiteStore
from mdv.models import FinancingRecord, FinancingSnapshot, MarketRecord, MarketSnapshot


class FakeConnector:
    def __init__(self, *, source: str, venue: str, fail: bool = False):
        self.source = source
        self.venue = venue
        self.market_type = "SPOT"
        self.product = "SPOT"
        self.fail = fail

    async def fetch(self, _client):
        if self.fail:
            raise RuntimeError(f"{self.source} unavailable")
        market = MarketRecord(
            source=self.source,
            venue=self.venue,
            market_type="SPOT",
            product="SPOT",
            raw_symbol=f"{self.venue}COINUSDT",
            base_symbol=f"{self.venue}COIN",
            quote_symbol="USDT",
            settle_symbol=None,
            contract_type="SPOT",
            status="TRADING",
            active=True,
            contract_multiplier=None,
            raw={"symbol": f"{self.venue}COINUSDT"},
        )
        return MarketSnapshot(
            source=self.source,
            venue=self.venue,
            market_type="SPOT",
            product="SPOT",
            observed_at="2026-07-03T00:00:00+00:00",
            markets=(market,),
        )


class FakeFinancingConnector:
    source = "BYBIT_CROSS_MARGIN"
    venue = "BYBIT"
    market_type = "FINANCING"
    product = "CROSS_MARGIN"

    def __init__(self, *, fail: bool = False):
        self.fail = fail

    async def fetch(self, _client):
        if self.fail:
            raise RuntimeError("margin endpoint unavailable")
        record = FinancingRecord(
            source=self.source, venue=self.venue, product=self.product,
            asset_role="BORROWABLE", raw_asset_symbol="BTC", eligible=True,
            status="ENABLED", regular_user_tier="No VIP", rates=(), terms=(),
            limits={}, pair_symbols=(), raw={"currency": "BTC"},
        )
        return FinancingSnapshot(
            source=self.source, venue=self.venue, product=self.product,
            observed_at="2026-07-05T00:00:00+00:00", records=(record,),
        )


def test_collection_service_saves_parent_run_and_supports_venue_scope(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    service = CollectionService(
        store,
        connectors=[
            FakeConnector(source="BINANCE_SPOT", venue="BINANCE"),
            FakeConnector(source="MEXC_SPOT", venue="MEXC", fail=True),
        ],
    )

    results = asyncio.run(service.collect_venue("binance"))

    assert len(results) == 1
    assert results[0].ok is True
    collection_log = store.list_collection_runs()
    saved = collection_log["runs"][0]
    assert saved["collection_run_id"] == results[0].collection_run_id
    assert saved["scope"] == "BINANCE"
    assert saved["status"] == "SUCCEEDED"
    assert [venue["venue"] for venue in saved["venues"]] == ["BINANCE"]
    assert saved["venues"][0]["changes"][0]["message"] == "BINANCECOIN listed"


def test_collection_service_records_partial_all_run_and_rejects_unknown_venue(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    service = CollectionService(
        store,
        connectors=[
            FakeConnector(source="BINANCE_SPOT", venue="BINANCE"),
            FakeConnector(source="MEXC_SPOT", venue="MEXC", fail=True),
        ],
    )

    results = asyncio.run(service.collect_all())

    assert [result.ok for result in results] == [True, False]
    saved = store.list_collection_runs()["runs"][0]
    assert saved["scope"] == "ALL"
    assert saved["status"] == "PARTIAL"
    assert {venue["status"] for venue in saved["venues"]} == {"FAILED", "SUCCEEDED"}
    with pytest.raises(ValueError, match="VENUE must be one of"):
        asyncio.run(service.collect_venue("UNKNOWN"))


def test_collect_cli_accepts_venue_scope():
    args = build_parser().parse_args(["collect", "--venue", "MEXC"])
    assert args.command == "collect"
    assert args.venue == "MEXC"


def test_collection_service_supports_generic_venue_exclusion(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    service = CollectionService(
        store,
        connectors=[
            FakeConnector(source="BINANCE_SPOT", venue="BINANCE"),
            FakeConnector(source="XT_SPOT", venue="XT"),
        ],
    )

    results = asyncio.run(service.collect(exclude_venues=["xt"]))

    assert [result.source for result in results] == ["BINANCE_SPOT"]
    assert store.list_collection_runs()["runs"][0]["scope"] == "ALL_EXCEPT_XT"


def test_collect_cli_accepts_repeatable_excluded_venues_and_bundle_commands():
    collect = build_parser().parse_args(
        ["collect", "--exclude-venue", "XT", "--exclude-venue", "MEXC"]
    )
    export = build_parser().parse_args(
        ["bundle-export", "--venue", "XT", "--output", "-"]
    )
    imported = build_parser().parse_args(["bundle-import", "bundle.json"])

    assert collect.exclude_venue == ["XT", "MEXC"]
    assert export.output == "-"
    assert imported.path == "bundle.json"


def test_failed_financing_collection_preserves_last_complete_snapshot(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    success = CollectionService(store, connectors=[FakeFinancingConnector()])
    failure = CollectionService(store, connectors=[FakeFinancingConnector(fail=True)])

    first = asyncio.run(success.collect_all())
    second = asyncio.run(failure.collect_all())

    assert first[0].ok is True
    assert second[0].ok is False
    assert store.list_financing({})["count"] == 1
    assert store.list_financing({})["financing"][0]["raw_asset_symbol"] == "BTC"
