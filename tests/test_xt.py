import asyncio
import json
from pathlib import Path

import httpx
import pytest

from mdv.connectors.registry import default_collection_connectors, market_trade_url, supported_venues
from mdv.connectors.xt import (
    XtCrossMarginConnector,
    XtCryptoLoanConnector,
    XtFutureConnector,
    XtSpotConnector,
)


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_xt_recorded_market_fixtures_preserve_native_and_normalized_fields():
    spot = XtSpotConnector().parse(
        fixture("xt_spot.json"), observed_at="2026-07-06T00:00:00+00:00"
    )
    future = XtFutureConnector().parse(
        fixture("xt_future.json"), observed_at="2026-07-06T00:00:00+00:00"
    )

    assert [market.active for market in spot.markets] == [True, False]
    assert spot.markets[0].raw_symbol == "XT_USDT"
    assert spot.markets[0].raw["_metadata"]["ASSET_TAGS"][0]["tag"] == "HOT"
    assert [market.product for market in future.markets] == ["PERP", "DATED", "DATED"]
    assert [market.active for market in future.markets] == [True, True, False]
    assert future.markets[0].contract_direction == "LINEAR"
    assert future.markets[0].contract_multiplier == "0.0001"
    assert future.markets[0].max_market_order_size == "250"
    assert future.markets[1].expiry_cycle == "Q"
    assert future.markets[1].expires_at == "2026-09-25T08:00:00+00:00"
    assert future.markets[2].venue_status == "DELIVERED"


def test_xt_recorded_financing_fixtures_cover_margin_and_crypto_loans():
    margin = XtCrossMarginConnector().parse(
        fixture("xt_margin.json"), observed_at="2026-07-06T00:00:00+00:00"
    )
    loan = XtCryptoLoanConnector().parse_pages(
        [fixture("xt_loan.json")],
        [fixture("xt_pledge.json")],
        observed_at="2026-07-06T00:00:00+00:00",
    )

    assert {record.raw_asset_symbol for record in margin.records} == {"USDT", "XT"}
    assert all(record.product == "CROSS_MARGIN" for record in margin.records)
    assert margin.records[0].pair_symbols == ("XT_USDT",)
    assert [(record.asset_role, record.raw_asset_symbol) for record in loan.records] == [
        ("BORROWABLE", "USDT"),
        ("COLLATERAL", "USDT"),
    ]
    assert [rate["term_days"] for rate in loan.records[0].rates if "term_days" in rate] == [7, 30]
    assert loan.records[1].limits["initial_pledge_rate"] == 0.8


def test_xt_crypto_loan_fetch_follows_public_pagination():
    connector = XtCryptoLoanConnector()
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        requested.append((request.url.path, page))
        payload = fixture("xt_loan.json" if "loan-currency" in request.url.path else "xt_pledge.json")
        if "loan-currency" in request.url.path and page == 1:
            payload["result"]["hasNext"] = True
        elif "loan-currency" in request.url.path and page == 2:
            payload["result"]["items"][0]["currency"] = "btc"
        return httpx.Response(200, json=payload)

    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await connector.fetch(client)

    snapshot = asyncio.run(fetch())

    assert sorted(requested) == sorted(
        [
            ("/v4/public/finance/loan/product/loan-currency", 1),
            ("/v4/public/finance/loan/product/loan-currency", 2),
            ("/v4/public/finance/loan/product/pledge-currency", 1),
        ]
    )
    assert len(snapshot.records) == 3


def test_xt_malformed_and_partial_payloads_fail_closed():
    with pytest.raises(ValueError, match="result.symbols"):
        XtSpotConnector().parse(
            fixture("xt_malformed.json"), observed_at="2026-07-06T00:00:00+00:00"
        )
    with pytest.raises(ValueError, match="quoteCurrency"):
        XtSpotConnector().parse(
            fixture("xt_partial.json"), observed_at="2026-07-06T00:00:00+00:00"
        )
    with pytest.raises(ValueError, match="result.symbols"):
        XtFutureConnector().parse(
            fixture("xt_malformed.json"), observed_at="2026-07-06T00:00:00+00:00"
        )
    with pytest.raises(ValueError, match="not an array"):
        XtCrossMarginConnector().parse(
            fixture("xt_malformed.json"), observed_at="2026-07-06T00:00:00+00:00"
        )
    with pytest.raises(ValueError, match="result.items"):
        XtCryptoLoanConnector().parse_pages(
            [fixture("xt_malformed.json")],
            [fixture("xt_pledge.json")],
            observed_at="2026-07-06T00:00:00+00:00",
        )


def test_xt_registry_extension_covers_collection_and_trade_links():
    assert "XT" in supported_venues()
    assert {connector.source for connector in default_collection_connectors() if connector.venue == "XT"} == {
        "XT_SPOT",
        "XT_FUTURE",
        "XT_CROSS_MARGIN",
        "XT_CRYPTO_LOAN",
    }
    assert market_trade_url(
        {"venue": "XT", "market_type": "SPOT", "raw_symbol": "XT_USDT"}
    ) == "https://www.xt.com/en/trade/xt_usdt"
    assert market_trade_url(
        {"venue": "XT", "market_type": "FUTURE", "raw_symbol": "BTC_USDT"}
    ) == "https://www.xt.com/en/futures/trade/btc_usdt"
