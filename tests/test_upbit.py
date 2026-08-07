import asyncio
import json
from pathlib import Path

import pytest

from mdv.collection import CollectionService
from mdv.connectors.registry import (
    default_collection_connectors,
    market_trade_url,
    supported_venues,
)
from mdv.connectors.upbit import UpbitSpotConnector
from mdv.db import SQLiteStore


FIXTURES = Path(__file__).parent / "fixtures"
OBSERVED_AT = "2026-08-07T00:00:00+00:00"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_upbit_recorded_spot_fixture_normalizes_each_quote_market():
    snapshot = UpbitSpotConnector().parse(
        fixture("upbit_success.json"), observed_at=OBSERVED_AT
    )

    assert snapshot.market_type == "SPOT"
    assert [(market.base_symbol, market.quote_symbol) for market in snapshot.markets] == [
        ("BTC", "KRW"),
        ("ETH", "BTC"),
        ("XRP", "USDT"),
    ]
    assert all(market.product == "SPOT" for market in snapshot.markets)
    assert all(market.status == "TRADING" for market in snapshot.markets)
    assert all(market.active for market in snapshot.markets)
    assert snapshot.markets[2].raw["market_event"]["warning"] is True


@pytest.mark.parametrize(
    "payload",
    [fixture("upbit_malformed.json"), fixture("upbit_partial.json")],
)
def test_upbit_malformed_and_partial_fixtures_fail_complete_snapshots(payload):
    with pytest.raises(ValueError):
        UpbitSpotConnector().parse(payload, observed_at=OBSERVED_AT)


class FailingConnector:
    source = "UPBIT_SPOT"
    venue = "UPBIT"
    market_type = "SPOT"
    product = "SPOT"

    async def fetch(self, _client):
        raise ValueError("partial upstream response")


def test_upbit_failed_snapshot_preserves_last_active_market(tmp_path):
    snapshot = UpbitSpotConnector().parse(
        fixture("upbit_success.json"), observed_at=OBSERVED_AT
    )
    store = SQLiteStore(tmp_path / "upbit.sqlite3")
    store.apply_snapshot(snapshot)

    result = asyncio.run(
        CollectionService(store, connectors=[FailingConnector()]).collect_all()
    )

    assert result[0].ok is False
    assert all(
        bool(market["active"])
        for market in store.list_markets({"VENUE": ["UPBIT"]})
    )


def test_upbit_registry_exposes_spot_source_and_trade_link():
    assert "UPBIT" in supported_venues()
    assert {
        connector.source
        for connector in default_collection_connectors()
        if connector.venue == "UPBIT"
    } == {"UPBIT_SPOT"}
    assert market_trade_url(
        {"venue": "UPBIT", "market_type": "SPOT", "raw_symbol": "KRW-BTC"}
    ) == "https://upbit.com/exchange?code=CRIX.UPBIT.KRW-BTC"
    assert market_trade_url(
        {"venue": "UPBIT", "market_type": "FUTURE", "raw_symbol": "BTC-USDT"}
    ) is None
