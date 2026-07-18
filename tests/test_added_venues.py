import asyncio
import json
from pathlib import Path

import pytest

from mdv.collection import CollectionService
from mdv.connectors.bitfinex import BitfinexConnector, BitfinexCrossMarginConnector
from mdv.connectors.deribit import DeribitConnector
from mdv.connectors.gemini import GeminiFutureConnector, GeminiSpotConnector
from mdv.connectors.registry import default_collection_connectors, market_trade_url, supported_venues
from mdv.db import SQLiteStore


FIXTURES = Path(__file__).parent / "fixtures"
OBSERVED_AT = "2026-07-11T00:00:00+00:00"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_deribit_recorded_spot_and_future_fixtures_normalize_dimensions():
    payload = fixture("deribit_success.json")
    spot = DeribitConnector(kind="spot").parse(payload["spot"], observed_at=OBSERVED_AT)
    futures = DeribitConnector(kind="future").parse(payload["future"], observed_at=OBSERVED_AT)

    assert spot.markets[0].active is True
    assert futures.markets[0].product == "PERP"
    assert futures.markets[0].contract_direction == "INVERSE"
    assert futures.markets[1].product == "DATED"
    assert futures.markets[1].contract_direction == "LINEAR"
    assert futures.markets[1].expires_at == "2026-09-25T08:00:00+00:00"


def test_gemini_recorded_spot_and_future_fixtures_normalize_dimensions():
    payload = fixture("gemini_success.json")
    spot = GeminiSpotConnector().parse(payload["spot"], observed_at=OBSERVED_AT)
    futures = GeminiFutureConnector().parse(payload["future"], observed_at=OBSERVED_AT)

    assert [market.raw_symbol for market in spot.markets] == ["BTCUSD", "ETHUSDC", "JITOSOLSOL"]
    assert (spot.markets[2].base_symbol, spot.markets[2].quote_symbol) == ("JITOSOL", "SOL")
    assert futures.markets[0].settle_symbol == "GUSD"
    assert futures.markets[0].contract_direction == "LINEAR"
    assert futures.markets[1].contract_direction == "INVERSE"
    assert futures.markets[1].active is False


def test_bitfinex_recorded_spot_and_future_fixtures_normalize_dimensions():
    payload = fixture("bitfinex_success.json")
    spot = BitfinexConnector(market_type="SPOT").parse(payload, observed_at=OBSERVED_AT)
    futures = BitfinexConnector(market_type="FUTURE").parse(
        payload,
        observed_at=OBSERVED_AT,
        product_info_payload=fixture("bitfinex_derivative_product_info.json"),
        status_payload=fixture("bitfinex_derivatives_status.json"),
    )

    assert [(market.base_symbol, market.quote_symbol) for market in spot.markets] == [
        ("BTC", "USD"), ("AAVE", "USD")
    ]
    assert futures.markets[0].settle_symbol == "UST"
    assert futures.markets[0].contract_direction == "LINEAR"
    assert futures.markets[1].contract_direction == "LINEAR"
    jasmy = futures.markets[2]
    assert jasmy.raw_symbol == "JASMYF0:USTF0"
    assert jasmy.contract_multiplier == "1"
    assert jasmy.contract_multiplier_unit == "JASMY"
    assert jasmy.contract_value_currency == "JASMY"
    assert jasmy.open_interest_unit == "CONTRACT"
    assert jasmy.contract_metadata_reason is None
    assert jasmy.raw["_metadata"]["CONTRACT_METADATA"]["status"][18] == 1000


def test_bitfinex_missing_instrument_status_is_explicitly_unresolved():
    status = fixture("bitfinex_derivatives_status.json")[:-1]
    futures = BitfinexConnector(market_type="FUTURE").parse(
        fixture("bitfinex_success.json"),
        observed_at=OBSERVED_AT,
        product_info_payload=fixture("bitfinex_derivative_product_info.json"),
        status_payload=status,
    )

    jasmy = futures.markets[2]
    assert jasmy.contract_multiplier is None
    assert jasmy.open_interest_unit is None
    assert jasmy.contract_metadata_reason == "BITFINEX_DERIVATIVES_STATUS_MISSING"


def test_bitfinex_recorded_margin_fixture_preserves_pair_level_eligibility():
    snapshot = BitfinexCrossMarginConnector().parse(
        fixture("bitfinex_financing.json"), observed_at=OBSERVED_AT
    )

    assert {record.raw_asset_symbol for record in snapshot.records} == {"AAVE", "BTC", "USD"}
    assert all(record.eligible for record in snapshot.records)
    assert snapshot.records[0].raw["evidence_granularity"] == "PAIR"


@pytest.mark.parametrize(
    ("connector", "payload"),
    [
        (DeribitConnector(kind="spot"), fixture("deribit_malformed.json")["spot"]),
        (DeribitConnector(kind="future"), fixture("deribit_malformed.json")["future"]),
        (DeribitConnector(kind="spot"), fixture("deribit_partial.json")["spot"]),
        (DeribitConnector(kind="future"), fixture("deribit_partial.json")["future"]),
        (GeminiSpotConnector(), fixture("gemini_malformed.json")["spot"]),
        (GeminiFutureConnector(), fixture("gemini_malformed.json")["future"]),
        (GeminiSpotConnector(), fixture("gemini_partial.json")["spot"]),
        (GeminiFutureConnector(), fixture("gemini_partial.json")["future"]),
        (BitfinexConnector(market_type="SPOT"), fixture("bitfinex_malformed.json")),
        (BitfinexConnector(market_type="FUTURE"), fixture("bitfinex_malformed.json")),
        (BitfinexConnector(market_type="SPOT"), fixture("bitfinex_partial.json")),
        (BitfinexConnector(market_type="FUTURE"), fixture("bitfinex_partial.json")),
        (BitfinexCrossMarginConnector(), fixture("bitfinex_malformed.json")),
        (BitfinexCrossMarginConnector(), fixture("bitfinex_partial.json")),
    ],
)
def test_added_venue_malformed_and_partial_fixtures_fail_complete_snapshots(connector, payload):
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


@pytest.mark.parametrize(
    ("venue", "connector", "payload"),
    [
        ("DERIBIT", DeribitConnector(kind="spot"), fixture("deribit_success.json")["spot"]),
        ("GEMINI", GeminiSpotConnector(), fixture("gemini_success.json")["spot"]),
        ("BITFINEX", BitfinexConnector(market_type="SPOT"), fixture("bitfinex_success.json")),
    ],
)
def test_added_venue_failed_snapshot_preserves_last_active_market(tmp_path, venue, connector, payload):
    snapshot = connector.parse(payload, observed_at=OBSERVED_AT)
    store = SQLiteStore(tmp_path / f"{venue}.sqlite3")
    store.apply_snapshot(snapshot)
    result = asyncio.run(
        CollectionService(store, connectors=[FailingConnector(snapshot)]).collect_all()
    )

    assert result[0].ok is False
    assert bool(store.list_markets({"VENUE": [venue]})[0]["active"]) is True


def test_added_venue_registry_exposes_sources_and_trade_links():
    expected = {
        "BITFINEX": {"BITFINEX_SPOT", "BITFINEX_FUTURE", "BITFINEX_CROSS_MARGIN"},
        "DERIBIT": {"DERIBIT_SPOT", "DERIBIT_FUTURE"},
        "GEMINI": {"GEMINI_SPOT", "GEMINI_FUTURE"},
    }
    connectors = default_collection_connectors()
    for venue, sources in expected.items():
        assert venue in supported_venues()
        assert {item.source for item in connectors if item.venue == venue} == sources

    assert market_trade_url(
        {"venue": "BITFINEX", "market_type": "FUTURE", "raw_symbol": "BTCF0:USTF0"}
    ) == "https://trading.bitfinex.com/t/BTCF0%3AUSTF0"
    assert market_trade_url(
        {"venue": "DERIBIT", "market_type": "FUTURE", "raw_symbol": "BTC-PERPETUAL"}
    ) == "https://www.deribit.com/futures/BTC-PERPETUAL"
    assert market_trade_url(
        {"venue": "GEMINI", "market_type": "SPOT", "raw_symbol": "BTCUSD"}
    ) == "https://exchange.gemini.com/trade/BTCUSD"
