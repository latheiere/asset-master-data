import asyncio
import json
from pathlib import Path

import httpx
import pytest

from mdv.connectors.binance import BinanceConnector
from mdv.connectors.bitget import BitgetFutureConnector, BitgetSpotConnector, bitget_connectors
from mdv.connectors.bitfinex import BitfinexCrossMarginConnector
from mdv.connectors.bybit import BybitConnector, bybit_connectors
from mdv.connectors.gate import GateFutureConnector, GateSpotConnector, gate_connectors
from mdv.connectors.financing import (
    BitgetCrossMarginConnector,
    BitgetCryptoLoanConnector,
    BinanceCrossMarginPublicConnector,
    BybitCrossMarginConnector,
    BybitCryptoLoanConnector,
    GateCryptoLoanConnector,
    GateCrossMarginConnector,
    KucoinCrossMarginConnector,
    financing_connectors,
)
from mdv.connectors.mexc import MexcFutureConnector, MexcSpotConnector
from mdv.connectors.kucoin import KucoinFutureConnector
from mdv.connectors.registry import default_collection_connectors


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_binance_future_parser_preserves_market_fields():
    connector = BinanceConnector(
        source="BINANCE_USDM_FUTURE",
        market_type="FUTURE",
        product="USD-M",
        url="https://example.test",
    )
    snapshot = connector.parse(
        {
            "symbols": [
                {
                    "symbol": "1000PEPEUSDT",
                    "baseAsset": "1000PEPE",
                    "quoteAsset": "USDT",
                    "marginAsset": "USDT",
                    "contractType": "PERPETUAL",
                    "status": "TRADING",
                    "filters": [
                        {"filterType": "LOT_SIZE", "maxQty": "1000"},
                        {"filterType": "MARKET_LOT_SIZE", "maxQty": "250.000"},
                    ],
                }
            ]
        },
        observed_at="2026-07-03T00:00:00+00:00",
    )
    market = snapshot.markets[0]
    assert market.market_id == "BINANCE_USDM_FUTURE:1000PEPEUSDT"
    assert market.active is True
    assert market.contract_type == "PERP"
    assert market.product == "PERP"
    assert market.venue_product == "USD-M"
    assert market.contract_direction == "LINEAR"
    assert market.max_market_order_size == "250.000"


def test_kucoin_perpetual_delisting_timestamp_is_not_a_contract_expiry():
    snapshot = KucoinFutureConnector().parse(
        {
            "code": "200000",
            "data": [{
                "symbol": "NFPUSDTM", "type": "FFWCSX",
                "expireDate": 1783494000000, "baseCurrency": "NFP",
                "quoteCurrency": "USDT", "settleCurrency": "USDT",
                "marketMaxOrderQty": 1000000, "multiplier": 1, "status": "Open",
            }],
        },
        observed_at="2026-07-08T06:00:00+00:00",
    )

    market = snapshot.markets[0]
    assert market.contract_type == "PERP"
    assert market.product == "PERP"
    assert market.expires_at is None


def test_binance_spot_parser_preserves_product_metadata_tags():
    connector = BinanceConnector(
        source="BINANCE_SPOT",
        market_type="SPOT",
        product="SPOT",
        url="https://example.test/exchange-info",
        metadata_url="https://example.test/products",
    )
    snapshot = connector.parse(
        {
            "symbols": [
                {
                    "symbol": "WIFUSDT",
                    "baseAsset": "WIF",
                    "quoteAsset": "USDT",
                    "status": "TRADING",
                }
            ]
        },
        metadata_payload={
            "data": [
                {"s": "WIFUSDT", "b": "WIF", "tags": ["Monitoring", "Seed", "Solana", "Meme"]}
            ]
        },
        observed_at="2026-07-03T00:00:00+00:00",
    )
    metadata = snapshot.markets[0].raw["_metadata"]["BINANCE_PRODUCT"]
    assert metadata["tags"] == ["Monitoring", "Seed", "Solana", "Meme"]


def test_binance_coinm_parser_uses_contract_status():
    connector = BinanceConnector(
        source="BINANCE_COINM_FUTURE",
        market_type="FUTURE",
        product="COIN-M",
        url="https://example.test",
    )
    snapshot = connector.parse(
        {
            "symbols": [
                {
                    "symbol": "BTCUSD_PERP",
                    "baseAsset": "BTC",
                    "quoteAsset": "USD",
                    "marginAsset": "BTC",
                    "contractType": "PERPETUAL",
                    "contractStatus": "TRADING",
                }
            ]
        },
        observed_at="2026-07-03T00:00:00+00:00",
    )
    assert snapshot.markets[0].status == "TRADING"
    assert snapshot.markets[0].active is True


def test_binance_delivery_contracts_use_dated_product_and_expiry_cycle():
    connector = BinanceConnector(
        source="BINANCE_COINM_FUTURE",
        market_type="FUTURE",
        product="COIN-M",
        url="https://example.test",
    )
    snapshot = connector.parse(
        {
            "symbols": [
                {
                    "symbol": "BTCUSD_260925",
                    "baseAsset": "BTC",
                    "quoteAsset": "USD",
                    "marginAsset": "BTC",
                    "contractType": "CURRENT_QUARTER",
                    "contractStatus": "TRADING",
                    "deliveryDate": 1790323200000,
                },
                {
                    "symbol": "BTCUSD_261225",
                    "baseAsset": "BTC",
                    "quoteAsset": "USD",
                    "marginAsset": "BTC",
                    "contractType": "NEXT_QUARTER",
                    "contractStatus": "TRADING",
                    "deliveryDate": 1798185600000,
                },
            ]
        },
        observed_at="2026-07-03T00:00:00+00:00",
    )

    assert [market.product for market in snapshot.markets] == ["DATED", "DATED"]
    assert [market.contract_type for market in snapshot.markets] == ["DATED", "DATED"]
    assert [market.expiry_cycle for market in snapshot.markets] == ["Q", "BQ"]
    assert all(market.contract_direction == "INVERSE" for market in snapshot.markets)
    assert snapshot.markets[0].expires_at == "2026-09-25T08:00:00+00:00"


def test_binance_tradifi_perpetual_uses_short_contract_code():
    connector = BinanceConnector(
        source="BINANCE_USDM_FUTURE",
        market_type="FUTURE",
        product="USD-M",
        url="https://example.test",
    )
    snapshot = connector.parse(
        {
            "symbols": [
                {
                    "symbol": "AMATUSDT",
                    "baseAsset": "AMAT",
                    "quoteAsset": "USDT",
                    "marginAsset": "USDT",
                    "contractType": "TRADIFI_PERPETUAL",
                    "status": "TRADING_HALT",
                    "underlyingType": "EQUITY",
                }
            ]
        },
        observed_at="2026-07-03T00:00:00+00:00",
    )
    assert snapshot.markets[0].contract_type == "PERP"
    assert snapshot.markets[0].status == "PAUSED"
    assert snapshot.markets[0].active is True
    assert snapshot.markets[0].trading_schedule.session_status == "CLOSED"


def test_session_based_provider_markets_remain_listed_while_closed():
    bitget = BitgetSpotConnector().parse(
        {"code": "00000", "data": [{
            "symbol": "RTZAUSDT", "baseCoin": "rTZA", "quoteCoin": "USDT",
            "status": "halt", "areaSymbol": "yes",
        }]},
        observed_at="2026-07-15T00:00:00+00:00",
    ).markets[0]
    gate = GateFutureConnector(settle="USDT").parse(
        [{
            "name": "SOXS_USDT", "status": "suspend", "in_delisting": False,
            "contract_type": "stocks", "quanto_multiplier": "1",
        }],
        observed_at="2026-07-15T00:00:00+00:00",
    ).markets[0]
    mexc = MexcFutureConnector().parse(
        {"success": True, "data": [{
            "symbol": "AUD_USDT", "baseCoin": "AUD", "quoteCoin": "USDT",
            "settleCoin": "USDT", "state": 4,
            "conceptPlate": ["mc-trade-zone-tradfi", "mc-trade-zone-Forex"],
        }]},
        observed_at="2026-07-15T00:00:00+00:00",
    ).markets[0]
    bybit = BybitConnector(
        source="BYBIT_LINEAR_FUTURE", category="linear",
        market_type="FUTURE", product="LINEAR",
    ).parse(
        {"retCode": 0, "result": {"category": "linear", "list": [{
            "symbol": "AAPLUSDT", "baseCoin": "AAPL", "quoteCoin": "USDT",
            "settleCoin": "USDT", "contractType": "LinearPerpetual",
            "symbolType": "stock", "status": "Paused",
        }]}},
        observed_at="2026-07-15T00:00:00+00:00",
    ).markets[0]

    assert [market.active for market in (bitget, gate, mexc, bybit)] == [True] * 4
    assert [market.status for market in (bitget, gate, mexc, bybit)] == ["PAUSED"] * 4
    assert all(market.trading_schedule is not None for market in (bitget, gate, mexc, bybit))


def test_mexc_parsers_accept_official_shapes():
    spot = MexcSpotConnector().parse(
        {"symbols": [{"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "ENABLED"}]},
        observed_at="2026-07-03T00:00:00+00:00",
    )
    future = MexcFutureConnector().parse(
        {
            "success": True,
            "data": [
                {
                    "symbol": "BTC_USDT",
                    "baseCoin": "BTC",
                    "quoteCoin": "USDT",
                    "settleCoin": "USDT",
                    "contractSize": 0.0001,
                    "maxVol": 5000000,
                    "state": 0,
                }
            ],
        },
        observed_at="2026-07-03T00:00:00+00:00",
    )
    assert spot.markets[0].active is True
    assert future.markets[0].active is True
    assert future.markets[0].contract_multiplier == "0.0001"
    assert future.markets[0].max_market_order_size == "5000000"
    assert future.markets[0].contract_type == "PERP"
    assert future.product == "PERP"


def test_mexc_spot_normalizes_numeric_enabled_status():
    snapshot = MexcSpotConnector().parse(
        {"symbols": [{"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "1"}]},
        observed_at="2026-07-03T00:00:00+00:00",
    )
    assert snapshot.markets[0].status == "TRADING"
    assert snapshot.markets[0].venue_status == "ENABLED"
    assert snapshot.markets[0].active is True


def test_bybit_parsers_accept_recorded_spot_linear_and_inverse_shapes():
    spot_connector, linear_connector, inverse_connector = bybit_connectors()

    spot = spot_connector.parse(
        fixture("bybit_spot.json"), observed_at="2026-07-03T00:00:00+00:00"
    )
    linear = linear_connector.parse(
        fixture("bybit_linear.json"), observed_at="2026-07-03T00:00:00+00:00"
    )
    inverse = inverse_connector.parse(
        fixture("bybit_inverse.json"), observed_at="2026-07-03T00:00:00+00:00"
    )

    assert spot.markets[0].market_id == "BYBIT_SPOT:BTCUSDT"
    assert spot.markets[0].contract_type == "SPOT"
    assert [market.contract_type for market in linear.markets] == ["PERP", "DATED"]
    assert [market.max_market_order_size for market in linear.markets] == [
        "500.000",
        "250.000",
    ]
    assert linear.markets[1].expires_at == "2026-09-25T08:00:00+00:00"
    assert inverse.markets[0].settle_symbol == "BTC"
    assert inverse.markets[1].contract_type == "DATED"
    assert inverse.markets[1].expires_at == "2026-12-25T00:00:00+00:00"
    assert inverse.markets[1].raw["contractType"] == "InverseFutures"


def test_bybit_derivative_fetch_follows_pagination_cursor():
    connector = BybitConnector(
        source="BYBIT_LINEAR_FUTURE",
        category="linear",
        market_type="FUTURE",
        product="LINEAR",
    )
    requested_cursors = []

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor", "")
        requested_cursors.append(cursor)
        payload = fixture("bybit_linear.json")
        payload["result"]["list"] = [payload["result"]["list"][0 if not cursor else 1]]
        payload["result"]["nextPageCursor"] = "page-2" if not cursor else ""
        return httpx.Response(200, json=payload)

    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await connector.fetch(client)

    snapshot = asyncio.run(fetch())

    assert requested_cursors == ["", "page-2"]
    assert [market.raw_symbol for market in snapshot.markets] == [
        "BTCUSDT",
        "BTCUSDT-25SEP26",
    ]


def test_gate_parsers_accept_spot_perpetual_and_delivery_shapes():
    spot = GateSpotConnector().parse(
        [
            {
                "id": "GM_USDT",
                "base": "GM",
                "quote": "USDT",
                "trade_status": "tradable",
                "type": "normal",
                "st_tag": True,
            }
        ],
        observed_at="2026-07-03T00:00:00+00:00",
    )
    perpetual = GateFutureConnector(settle="USDT").parse(
        [
            {
                "name": "BTC_USDT",
                "status": "trading",
                "in_delisting": False,
                "quanto_multiplier": "0.0001",
                "market_order_size_max": "250000",
            }
        ],
        observed_at="2026-07-03T00:00:00+00:00",
    )
    delivery = GateFutureConnector(settle="USDT", delivery=True).parse(
        [
            {
                "name": "SOL_USDT_20260710",
                "underlying": "SOL_USDT",
                "cycle": "WEEKLY",
                "expire_time": 1783670400,
                "in_delisting": False,
                "quanto_multiplier": "1",
            },
            {
                "name": "SOL_USDT_20260717",
                "underlying": "SOL_USDT",
                "cycle": "BI-WEEKLY",
                "expire_time": 1784275200,
                "in_delisting": False,
                "quanto_multiplier": "1",
            },
        ],
        observed_at="2026-07-03T00:00:00+00:00",
    )

    assert len(gate_connectors()) == 4
    assert spot.markets[0].active is True
    assert spot.markets[0].raw["_metadata"]["ASSET_TAGS"][0]["tag"] == "ST"
    assert perpetual.markets[0].contract_type == "PERP"
    assert perpetual.markets[0].max_market_order_size == "250000"
    assert [market.contract_type for market in delivery.markets] == ["DATED", "DATED"]
    assert [market.product for market in delivery.markets] == ["DATED", "DATED"]
    assert [market.expiry_cycle for market in delivery.markets] == ["W", "BW"]
    assert delivery.markets[0].expires_at == "2026-07-10T08:00:00+00:00"


def test_bitget_parsers_accept_spot_and_all_future_product_shapes():
    spot = BitgetSpotConnector().parse(
        {
            "code": "00000",
            "msg": "success",
            "data": [
                {
                    "symbol": "BTCUSDT",
                    "baseCoin": "BTC",
                    "quoteCoin": "USDT",
                    "status": "online",
                    "areaSymbol": "yes",
                }
            ],
        },
        observed_at="2026-07-03T00:00:00+00:00",
    )
    connector = BitgetFutureConnector(product_type="COIN-FUTURES", product="COIN-M")
    future = connector.parse(
        {
            "code": "00000",
            "msg": "success",
            "data": [
                {
                    "symbol": "BTCUSD",
                    "baseCoin": "BTC",
                    "quoteCoin": "USD",
                    "symbolType": "perpetual",
                    "symbolStatus": "normal",
                    "maxMarketOrderQty": "30",
                    "isRwa": "YES",
                },
                {
                    "symbol": "BTCUSDU26",
                    "baseCoin": "BTC",
                    "quoteCoin": "USD",
                    "symbolType": "delivery",
                    "symbolStatus": "normal",
                    "deliveryPeriod": "this_quarter",
                    "deliveryTime": "1782460799000",
                    "maxMarketOrderQty": "10",
                    "isRwa": "NO",
                },
            ],
        },
        observed_at="2026-07-03T00:00:00+00:00",
    )

    assert len(bitget_connectors()) == 4
    assert spot.markets[0].raw["_metadata"]["ASSET_TAGS"][0]["tag"] == "AREA"
    assert future.markets[0].settle_symbol == "BTC"
    assert future.markets[0].contract_type == "PERP"
    assert future.markets[0].max_market_order_size == "30"
    assert future.markets[0].raw["_metadata"]["ASSET_TAGS"][0]["tag"] == "RWA"
    assert future.markets[0].contract_direction == "INVERSE"
    assert future.markets[0].venue_product == "COIN-M"
    assert future.markets[1].contract_type == "DATED"
    assert future.markets[1].product == "DATED"
    assert future.markets[1].expiry_cycle == "Q"
    assert future.markets[1].expires_at == "2026-06-26T07:59:59+00:00"


def test_financing_parsers_keep_margin_and_crypto_loan_separate():
    bybit = fixture("bybit_financing.json")
    bitget = fixture("bitget_financing.json")
    binance_margin = BinanceCrossMarginPublicConnector().parse(
        fixture("binance_financing.json"), observed_at="2026-07-05T00:00:00+00:00"
    )
    observed_at = "2026-07-05T00:00:00+00:00"

    bybit_margin = BybitCrossMarginConnector().parse(
        bybit["margin"], observed_at=observed_at
    )
    bybit_loan = BybitCryptoLoanConnector().parse(
        bybit["loan"], bybit["collateral"], observed_at=observed_at
    )
    bitget_margin = BitgetCrossMarginConnector().parse(
        bitget["margin"], observed_at=observed_at
    )
    bitget_loan = BitgetCryptoLoanConnector().parse(
        bitget["loan"], observed_at=observed_at
    )
    gate_margin = GateCrossMarginConnector().parse(
        fixture("gate_financing.json"), observed_at=observed_at
    )
    gate_loan = GateCryptoLoanConnector().parse(
        fixture("gate_crypto_loan.json"), observed_at=observed_at
    )
    kucoin_margin = KucoinCrossMarginConnector().parse(
        fixture("kucoin_financing.json"), observed_at=observed_at
    )

    bitfinex_margin = BitfinexCrossMarginConnector().parse(
        fixture("bitfinex_financing.json"), observed_at=observed_at
    )

    assert len(financing_connectors()) == 9
    assert {row.raw_asset_symbol for row in binance_margin.records} == {"BTC", "USDT"}
    assert all(row.status == "ENABLED" for row in binance_margin.records)
    assert binance_margin.records[0].raw["evidence_granularity"] == "PAIR"
    assert bybit_margin.product == "CROSS_MARGIN"
    assert bybit_margin.records[0].regular_user_tier == "No VIP"
    assert bybit_margin.records[0].rates[0]["rate_unit"] == "HOURLY"
    assert bybit_margin.records[1].eligible is False
    assert bybit_loan.product == "CRYPTO_LOAN"
    assert {row.asset_role for row in bybit_loan.records} == {"BORROWABLE", "COLLATERAL"}
    assert bybit_loan.records[0].rates[0]["rate_type"] == "FLEXIBLE"
    assert {row.raw_asset_symbol for row in bitget_margin.records} == {"BTC", "USDT"}
    assert bitget_margin.records[0].raw["evidence_granularity"] == "PAIR"
    assert {row.asset_role for row in bitget_loan.records} == {"BORROWABLE", "COLLATERAL"}
    assert gate_margin.records[0].eligible is True
    assert gate_margin.records[1].eligible is False
    assert {row.asset_role for row in gate_loan.records} == {"BORROWABLE", "COLLATERAL"}
    assert kucoin_margin.records[0].limits["max_leverage"] == 5
    assert {row.raw_asset_symbol for row in bitfinex_margin.records} == {"AAVE", "BTC", "USD"}

    sources = {connector.source for connector in default_collection_connectors()}
    assert {
        "BINANCE_CROSS_MARGIN_PUBLIC", "BYBIT_CROSS_MARGIN", "BYBIT_CRYPTO_LOAN",
        "BITGET_CROSS_MARGIN", "BITGET_CRYPTO_LOAN", "GATE_CROSS_MARGIN",
        "GATE_CRYPTO_LOAN", "KUCOIN_CROSS_MARGIN", "BITFINEX_CROSS_MARGIN",
    }.issubset(sources)


def test_financing_parsers_reject_recorded_malformed_payloads():
    malformed = fixture("financing_malformed.json")
    observed_at = "2026-07-05T00:00:00+00:00"

    with pytest.raises(ValueError):
        BinanceCrossMarginPublicConnector().parse(
            malformed["binance"], observed_at=observed_at
        )
    with pytest.raises(ValueError):
        BybitCrossMarginConnector().parse(malformed["bybit"], observed_at=observed_at)
    with pytest.raises(ValueError):
        BitgetCrossMarginConnector().parse(malformed["bitget"], observed_at=observed_at)
    with pytest.raises(ValueError):
        GateCrossMarginConnector().parse(malformed["gate"], observed_at=observed_at)
    with pytest.raises(ValueError):
        GateCryptoLoanConnector().parse(malformed["gate_loan"], observed_at=observed_at)
    with pytest.raises(ValueError):
        KucoinCrossMarginConnector().parse(malformed["kucoin"], observed_at=observed_at)
