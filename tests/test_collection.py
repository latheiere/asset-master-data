import asyncio

import pytest

from mdv.cli import build_parser
from mdv.collection import CollectionService
from mdv.db import SQLiteStore
from mdv.models import MarketRecord, MarketSnapshot


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
