import asyncio
import json
from pathlib import Path

import pytest

from mdv.collection import CollectionService
from mdv.connectors.bitmart import BitmartFutureConnector, BitmartSpotConnector
from mdv.connectors.registry import default_collection_connectors, market_trade_url, supported_venues
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
