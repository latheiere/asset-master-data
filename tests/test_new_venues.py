import asyncio
import json
from pathlib import Path

import pytest

from mdv.collection import CollectionService
from mdv.connectors.htx import htx_connectors
from mdv.connectors.coinbase import (
    CoinbaseCrossMarginConnector,
    CoinbasePerpetualConnector,
    CoinbaseSpotConnector,
)
from mdv.connectors.hyperliquid import (
    HyperliquidPerpConnector,
    HyperliquidSpotConnector,
)
from mdv.connectors.kucoin import KucoinFutureConnector, KucoinSpotConnector
from mdv.connectors.okx import okx_connectors
from mdv.connectors.registry import (
    default_collection_connectors,
    market_metadata,
    market_trade_url,
    supported_venues,
)
from mdv.connectors.whitebit import WhitebitConnector
from mdv.db import SQLiteStore


FIXTURES = Path(__file__).parent / "fixtures"
OBSERVED_AT = "2026-07-06T00:00:00+00:00"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_okx_recorded_spot_swap_and_expiry_fixtures_normalize_dimensions():
    payload = fixture("okx_success.json")
    spot_connector, swap_connector, futures_connector = okx_connectors()
    spot = spot_connector.parse(payload["spot"], observed_at=OBSERVED_AT)
    swap = swap_connector.parse(payload["swap"], observed_at=OBSERVED_AT)
    futures = futures_connector.parse(payload["futures"], observed_at=OBSERVED_AT)

    assert spot.markets[0].product == "SPOT"
    assert spot.markets[0].active is True
    assert swap.markets[0].product == "PERP"
    assert swap.markets[0].contract_direction == "LINEAR"
    assert swap.markets[0].max_market_order_size == "500"
    assert futures.markets[0].product == "DATED"
    assert futures.markets[0].contract_direction == "INVERSE"
    assert futures.markets[0].expiry_cycle == "Q"
    assert futures.markets[0].expires_at == "2026-09-25T08:00:00+00:00"


def test_okx_skips_official_preopen_listing_placeholders_without_symbols():
    payload = fixture("okx_success.json")["spot"]
    payload["data"].append(
        {
            "instId": "LISTING-SPOT-SLX-USD",
            "instType": "SPOT",
            "state": "preopen",
            "baseCcy": "",
            "quoteCcy": "",
        }
    )

    snapshot = okx_connectors()[0].parse(payload, observed_at=OBSERVED_AT)

    assert [market.raw_symbol for market in snapshot.markets] == ["BTC-USDT"]


def test_hyperliquid_recorded_spot_and_all_perp_dex_fixtures():
    payload = fixture("hyperliquid_success.json")
    spot = HyperliquidSpotConnector().parse(payload["spot"], observed_at=OBSERVED_AT)
    perps = HyperliquidPerpConnector().parse(
        payload["perps"], spot_payload=payload["spot"], observed_at=OBSERVED_AT
    )

    assert spot.markets[0].raw_symbol == "PURR/USDC"
    assert spot.markets[0].base_symbol == "PURR"
    assert perps.markets[0].raw_symbol == "BTC"
    assert perps.markets[0].settle_symbol == "USDC"
    assert perps.markets[0].venue_product == "PERP"
    assert perps.markets[1].raw_symbol == "flx:TSLA"
    assert perps.markets[1].base_symbol == "TSLA"
    assert perps.markets[1].settle_symbol == "USDT0"
    assert perps.markets[1].venue_product == "HIP-3:flx"
    assert perps.markets[1].active is False


def test_htx_recorded_spot_linear_inverse_perp_and_expiry_fixtures():
    payload = fixture("htx_success.json")
    spot_connector, linear_swap, linear_future, coin_swap, coin_future = htx_connectors()
    snapshots = [
        spot_connector.parse(payload["spot"], observed_at=OBSERVED_AT),
        linear_swap.parse(payload["linearSwap"], observed_at=OBSERVED_AT),
        linear_future.parse(payload["linearFuture"], observed_at=OBSERVED_AT),
        coin_swap.parse(payload["coinSwap"], observed_at=OBSERVED_AT),
        coin_future.parse(payload["coinFuture"], observed_at=OBSERVED_AT),
    ]

    assert snapshots[0].markets[0].active is True
    assert snapshots[1].markets[0].product == "PERP"
    assert snapshots[1].markets[0].contract_direction == "LINEAR"
    assert snapshots[2].markets[0].product == "DATED"
    assert snapshots[2].markets[0].expiry_cycle == "W"
    assert snapshots[3].markets[0].contract_direction == "INVERSE"
    assert snapshots[3].markets[0].status == "PAUSED"
    assert snapshots[4].markets[0].expiry_cycle == "Q"
    assert snapshots[4].markets[0].expires_at == "2026-09-25T08:00:00+00:00"


def test_kucoin_recorded_spot_perp_and_expiry_fixtures():
    payload = fixture("kucoin_success.json")
    spot = KucoinSpotConnector().parse(payload["spot"], observed_at=OBSERVED_AT)
    futures = KucoinFutureConnector().parse(payload["future"], observed_at=OBSERVED_AT)

    assert spot.markets[0].active is True
    assert spot.markets[0].raw["_metadata"]["ASSET_TAGS"][0]["tag"] == "ST"
    assert [market.product for market in futures.markets] == ["PERP", "DATED"]
    assert all(market.contract_direction == "LINEAR" for market in futures.markets)
    assert futures.markets[0].contract_multiplier == "0.001"
    assert futures.markets[0].max_market_order_size == "1000000"
    assert futures.markets[1].expires_at == "2026-09-25T08:00:00+00:00"


def test_whitebit_recorded_spot_crypto_and_tradfi_perp_fixtures():
    payload = fixture("whitebit_success.json")
    spot = WhitebitConnector(market_type="SPOT").parse(
        payload, observed_at=OBSERVED_AT
    )
    futures = WhitebitConnector(market_type="FUTURE").parse(
        payload, observed_at=OBSERVED_AT
    )

    assert [market.raw_symbol for market in spot.markets] == ["BTC_USDT"]
    assert [market.raw_symbol for market in futures.markets] == [
        "BTC_PERP",
        "TSLA_PERP",
    ]
    assert futures.markets[0].contract_direction == "LINEAR"
    assert futures.markets[1].active is False
    assert futures.markets[1].raw["_metadata"]["ASSET_TAGS"][0]["tag"] == "TRADFI"


def test_coinbase_recorded_spot_fixture_preserves_native_status_and_restrictions():
    snapshot = CoinbaseSpotConnector().parse(
        fixture("coinbase_success.json"), observed_at=OBSERVED_AT
    )

    assert [market.raw_symbol for market in snapshot.markets] == ["BTC-USD", "ETH-USD", "SOL-USD"]
    assert snapshot.markets[0].active is True
    assert snapshot.markets[1].status == "CLOSED"
    assert snapshot.markets[1].venue_status == "OFFLINE"
    assert snapshot.markets[2].active is False
    assert snapshot.markets[2].status == "PAUSED"
    assert snapshot.markets[2].venue_status == "ONLINE"


def test_coinbase_recorded_perpetual_and_margin_fixtures_normalize_dimensions():
    perpetuals = CoinbasePerpetualConnector().parse(
        fixture("coinbase_perpetuals_success.json"), observed_at=OBSERVED_AT
    )
    margin = CoinbaseCrossMarginConnector().parse(
        fixture("coinbase_success.json"), observed_at=OBSERVED_AT
    )

    assert [market.raw_symbol for market in perpetuals.markets] == [
        "BTC-PERP-INTX",
        "META-PERP-INTX",
    ]
    assert perpetuals.markets[0].active is True
    assert perpetuals.markets[0].contract_multiplier == "1"
    assert perpetuals.markets[0].contract_direction == "LINEAR"
    assert perpetuals.markets[1].status == "PAUSED"
    assert perpetuals.markets[1].venue_status == "STANDARD"
    assert perpetuals.markets[1].raw["future_product_details"]["perpetual_details"]["underlying_type"] == "EQUITY"
    assert "EQUITY" in market_metadata(
        {"market_type": "FUTURE", "base_symbol": "META"}, perpetuals.markets[1].raw
    ).classifications

    eligible = {record.raw_asset_symbol for record in margin.records if record.eligible}
    assert eligible == {"BTC", "USD"}
    assert margin.records[0].raw["evidence_granularity"] == "PAIR"


@pytest.mark.parametrize(
    ("parser", "payload"),
    [
        (lambda value: okx_connectors()[0].parse(value, observed_at=OBSERVED_AT), fixture("okx_malformed.json")),
        (lambda value: okx_connectors()[0].parse(value, observed_at=OBSERVED_AT), fixture("okx_partial.json")),
        (lambda value: HyperliquidSpotConnector().parse(value, observed_at=OBSERVED_AT), fixture("hyperliquid_malformed.json")),
        (lambda value: HyperliquidSpotConnector().parse(value, observed_at=OBSERVED_AT), fixture("hyperliquid_partial.json")),
        (lambda value: htx_connectors()[0].parse(value, observed_at=OBSERVED_AT), fixture("htx_malformed.json")),
        (lambda value: htx_connectors()[1].parse(value, observed_at=OBSERVED_AT), fixture("htx_partial.json")),
        (lambda value: KucoinSpotConnector().parse(value, observed_at=OBSERVED_AT), fixture("kucoin_malformed.json")),
        (lambda value: KucoinSpotConnector().parse(value, observed_at=OBSERVED_AT), fixture("kucoin_partial.json")),
        (lambda value: WhitebitConnector(market_type="SPOT").parse(value, observed_at=OBSERVED_AT), fixture("whitebit_malformed.json")),
        (lambda value: WhitebitConnector(market_type="FUTURE").parse(value, observed_at=OBSERVED_AT), fixture("whitebit_partial.json")),
        (lambda value: CoinbaseSpotConnector().parse(value, observed_at=OBSERVED_AT), fixture("coinbase_malformed.json")),
        (lambda value: CoinbaseSpotConnector().parse(value, observed_at=OBSERVED_AT), fixture("coinbase_partial.json")),
        (lambda value: CoinbasePerpetualConnector().parse(value, observed_at=OBSERVED_AT), fixture("coinbase_perpetuals_malformed.json")),
        (lambda value: CoinbasePerpetualConnector().parse(value, observed_at=OBSERVED_AT), fixture("coinbase_perpetuals_partial.json")),
        (lambda value: CoinbaseCrossMarginConnector().parse(value, observed_at=OBSERVED_AT), fixture("coinbase_partial.json")),
    ],
)
def test_new_venue_malformed_and_partial_fixtures_fail_complete_snapshots(parser, payload):
    with pytest.raises(ValueError):
        parser(payload)


class FailingConnector:
    def __init__(self, snapshot):
        self.source = snapshot.source
        self.venue = snapshot.venue
        self.market_type = snapshot.market_type
        self.product = snapshot.product

    async def fetch(self, _client):
        raise ValueError("partial upstream response")


@pytest.mark.parametrize("venue", ["OKX", "HYPERLIQUID", "HTX", "KUCOIN", "WHITEBIT", "COINBASE"])
def test_new_venue_failed_snapshot_preserves_last_active_market(tmp_path, venue):
    connector = next(
        connector
        for connector in default_collection_connectors()
        if connector.venue == venue and connector.market_type == "SPOT"
    )
    success_payloads = {
        "OKX": fixture("okx_success.json")["spot"],
        "HYPERLIQUID": fixture("hyperliquid_success.json")["spot"],
        "HTX": fixture("htx_success.json")["spot"],
        "KUCOIN": fixture("kucoin_success.json")["spot"],
        "WHITEBIT": fixture("whitebit_success.json"),
        "COINBASE": fixture("coinbase_success.json"),
    }
    snapshot = connector.parse(success_payloads[venue], observed_at=OBSERVED_AT)
    store = SQLiteStore(tmp_path / f"{venue}.sqlite3")
    store.apply_snapshot(snapshot)

    result = asyncio.run(
        CollectionService(store, connectors=[FailingConnector(snapshot)]).collect_all()
    )

    assert result[0].ok is False
    assert bool(store.list_markets({"VENUE": [venue]})[0]["active"]) is True


def test_new_venue_registry_extension_covers_sources_and_trade_links():
    expected = {
        "OKX": {"OKX_SPOT", "OKX_SWAP_FUTURE", "OKX_EXPIRY_FUTURE"},
        "HYPERLIQUID": {"HYPERLIQUID_SPOT", "HYPERLIQUID_PERP_FUTURE"},
        "HTX": {
            "HTX_SPOT",
            "HTX_USDT_SWAP_FUTURE",
            "HTX_USDT_FUTURE",
            "HTX_COIN_SWAP_FUTURE",
            "HTX_COIN_FUTURE",
        },
        "KUCOIN": {"KUCOIN_SPOT", "KUCOIN_FUTURE", "KUCOIN_CROSS_MARGIN"},
        "WHITEBIT": {"WHITEBIT_SPOT", "WHITEBIT_FUTURE"},
        "COINBASE": {
            "COINBASE_SPOT",
            "COINBASE_PERP_FUTURE",
            "COINBASE_CROSS_MARGIN",
        },
    }
    assert set(expected).issubset(supported_venues())
    connectors = default_collection_connectors()
    for venue, sources in expected.items():
        assert {connector.source for connector in connectors if connector.venue == venue} == sources

    assert market_trade_url(
        {
            "venue": "OKX",
            "market_type": "FUTURE",
            "venue_product": "SWAP",
            "raw_symbol": "BTC-USDT-SWAP",
        }
    ) == "https://www.okx.com/trade-swap/btc-usdt-swap"
    assert "PURR%2FUSDC" in market_trade_url(
        {"venue": "HYPERLIQUID", "market_type": "SPOT", "raw_symbol": "PURR/USDC"}
    )
    assert market_trade_url(
        {"venue": "KUCOIN", "market_type": "SPOT", "raw_symbol": "BTC-USDT"}
    ) == "https://www.kucoin.com/trade/BTC-USDT"
    assert market_trade_url(
        {"venue": "WHITEBIT", "market_type": "FUTURE", "raw_symbol": "BTC_PERP"}
    ) == "https://whitebit.com/trade/BTC_PERP"
    assert market_trade_url(
        {"venue": "COINBASE", "market_type": "SPOT", "raw_symbol": "BTC-USD"}
    ) == "https://www.coinbase.com/advanced-trade/spot/BTC-USD"
    assert market_trade_url(
        {"venue": "COINBASE", "market_type": "FUTURE", "raw_symbol": "BTC-PERP-INTX"}
    ) == "https://www.coinbase.com/advanced-trade/perpetuals/BTC-PERP-INTX"
