import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mdv.collection import CollectionService
from mdv.connectors.bitmart import BitmartFutureConnector, BitmartSpotConnector
from mdv.connectors.registry import (
    collection_lifecycle,
    default_collection_connectors,
    lifecycle_snapshot,
    market_trade_url,
    source_is_collectable,
    supported_venues,
)
from mdv.db import SQLiteStore


FIXTURES = Path(__file__).parent / "fixtures"
OBSERVED_AT = "2026-07-11T00:00:00+00:00"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_bitmart_recorded_spot_and_futures_fixtures_normalize_dimensions():
    payload = fixture("bitmart_success.json")
    spot = BitmartSpotConnector().parse(payload["spot"], observed_at=OBSERVED_AT)
    futures = BitmartFutureConnector().parse(payload["future"], observed_at=OBSERVED_AT)

    assert [market.active for market in spot.markets] == [True, False]
    assert spot.markets[1].venue_status == "PRE-TRADE"
    assert [market.product for market in futures.markets] == ["PERP", "PERP", "DATED"]
    assert futures.markets[0].venue_product == "USDT-M"
    assert futures.markets[0].settle_symbol == "USDT"
    assert futures.markets[0].contract_direction == "LINEAR"
    assert futures.markets[1].venue_product == "COIN-M"
    assert futures.markets[1].settle_symbol == "BTC"
    assert futures.markets[1].contract_direction == "INVERSE"
    assert futures.markets[1].status == "CLOSED"
    assert futures.markets[2].expires_at == "2026-09-25T08:00:00+00:00"
    assert futures.markets[2].max_market_order_size == "10000"
    assert futures.markets[2].raw["_metadata"]["ASSET_TAGS"][0]["tag"] == "TRADFI"
    assert futures.markets[2].trading_schedule is not None
    assert futures.markets[2].trading_schedule.market_group == "US_MARKET"


def test_bitmart_announced_lifecycle_restricts_then_stops_collection(tmp_path):
    payload = fixture("bitmart_success.json")
    futures = BitmartFutureConnector().parse(payload["future"], observed_at=OBSERVED_AT)
    restricted_at = datetime(2026, 7, 29, tzinfo=timezone.utc)
    closed_at = datetime(2026, 8, 26, 1, tzinfo=timezone.utc)

    lifecycle = collection_lifecycle("BITMART_FUTURE", at=restricted_at)
    assert lifecycle is not None
    assert lifecycle[1].collect is True
    assert lifecycle[1].status == "CLOSE_ONLY"
    restricted = lifecycle_snapshot(
        replace(futures, observed_at=restricted_at.isoformat())
    )
    assert {market.status for market in restricted.markets} == {"CLOSE_ONLY"}
    assert not any(market.active for market in restricted.markets)
    store = SQLiteStore(tmp_path / "restricted.sqlite3")
    store.apply_snapshot(restricted)
    assert not any(row["active"] for row in store.list_markets({}))
    assert source_is_collectable("BITMART_SPOT", at=closed_at) is False
    assert source_is_collectable("BITMART_FUTURE", at=closed_at) is False


@pytest.mark.parametrize(
    ("connector", "payload"),
    [
        (BitmartSpotConnector(), fixture("bitmart_malformed.json")["spot"]),
        (BitmartFutureConnector(), fixture("bitmart_malformed.json")["future"]),
        (BitmartSpotConnector(), fixture("bitmart_partial.json")["spot"]),
        (BitmartFutureConnector(), fixture("bitmart_partial.json")["future"]),
    ],
)
def test_bitmart_malformed_and_partial_fixtures_fail_complete_snapshots(connector, payload):
    with pytest.raises(ValueError):
        connector.parse(payload, observed_at=OBSERVED_AT)


class FailingConnector:
    def __init__(self, snapshot):
        self.source = snapshot.source
        self.venue = snapshot.venue
        self.market_type = snapshot.market_type
        self.product = snapshot.product

    async def fetch(self, _client):
        raise ValueError("partial upstream response")


class UnreachableConnector:
    source = "BITMART_SPOT"
    venue = "BITMART"
    market_type = "SPOT"
    product = "SPOT"

    async def fetch(self, _client):
        raise AssertionError("a closed source must not be queried")


def test_bitmart_failed_snapshot_preserves_last_active_market(tmp_path):
    snapshot = BitmartSpotConnector().parse(
        fixture("bitmart_success.json")["spot"], observed_at=OBSERVED_AT
    )
    store = SQLiteStore(tmp_path / "bitmart.sqlite3")
    store.apply_snapshot(snapshot)

    result = asyncio.run(
        CollectionService(store, connectors=[FailingConnector(snapshot)]).collect_all()
    )

    assert result[0].ok is False
    assert bool(store.list_markets({"VENUE": ["BITMART"]})[0]["active"]) is True


def test_bitmart_terminal_lifecycle_retires_history_without_fetching(tmp_path):
    snapshot = BitmartSpotConnector().parse(
        fixture("bitmart_success.json")["spot"], observed_at=OBSERVED_AT
    )
    store = SQLiteStore(tmp_path / "bitmart.sqlite3")
    store.apply_snapshot(snapshot)

    result = asyncio.run(
        CollectionService(
            store,
            connectors=[UnreachableConnector()],
            lifecycle_at=datetime(2026, 8, 26, 1, tzinfo=timezone.utc),
        ).collect_all()
    )

    assert result[0].ok is True
    assert result[0].records == len(snapshot.markets)
    with store.readonly() as conn:
        saved = conn.execute(
            "SELECT active, status, last_seen_at FROM markets ORDER BY raw_symbol"
        ).fetchall()
        events = [tuple(row) for row in conn.execute(
            "SELECT event_type, new_value FROM market_lifecycle_events "
            "WHERE run_id = ? ORDER BY event_type",
            (result[0].run_id,),
        )]
    assert all(row["active"] == 0 for row in saved)
    assert all(row["status"] == "CLOSED" for row in saved)
    assert {row["last_seen_at"] for row in saved} == {OBSERVED_AT}
    assert events == [
        ("DEACTIVATED", "False"),
        ("STATUS_CHANGED", "CLOSED"),
        ("STATUS_CHANGED", "CLOSED"),
    ]


def test_bitmart_registry_exposes_sources_and_trade_links():
    assert "BITMART" in supported_venues()
    assert {
        connector.source
        for connector in default_collection_connectors()
        if connector.venue == "BITMART"
    } == {"BITMART_SPOT", "BITMART_FUTURE"}
    assert market_trade_url(
        {"venue": "BITMART", "market_type": "SPOT", "raw_symbol": "BASE_QUOTE"}
    ) == "https://www.bitmart.com/en-US/trade/BASE_QUOTE?type=spot"
    assert market_trade_url(
        {"venue": "BITMART", "market_type": "FUTURE", "raw_symbol": "BTCUSDT"}
    ) == "https://www.bitmart.com/en-US/futures/BTCUSDT"
