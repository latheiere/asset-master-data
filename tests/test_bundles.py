import asyncio
import copy
import hashlib

import pytest

from mdv.bundles import (
    apply_collection_bundle,
    bundle_succeeded,
    canonical_json,
    export_collection_bundle,
)
from mdv.db import SQLiteStore
from mdv.models import FinancingRecord, FinancingSnapshot, MarketRecord, MarketSnapshot


class BundleMarketConnector:
    source = "TEST_SPOT"
    venue = "TEST"
    market_type = "SPOT"
    product = "SPOT"

    def __init__(self, *, fail: bool = False):
        self.fail = fail

    async def fetch(self, _client):
        if self.fail:
            raise RuntimeError("remote endpoint unavailable")
        market = MarketRecord(
            source=self.source,
            venue=self.venue,
            market_type=self.market_type,
            product=self.product,
            raw_symbol="BTC_USDT",
            base_symbol="BTC",
            quote_symbol="USDT",
            settle_symbol=None,
            contract_type="SPOT",
            status="TRADING",
            active=True,
            contract_multiplier=None,
            raw={"symbol": "btc_usdt"},
            venue_product="SPOT",
            venue_status="ONLINE",
        )
        return MarketSnapshot(
            self.source,
            self.venue,
            self.market_type,
            self.product,
            "2026-07-06T00:00:00+00:00",
            (market,),
        )


class BundleFinancingConnector:
    source = "TEST_LOAN"
    venue = "TEST"
    market_type = "FINANCING"
    product = "CRYPTO_LOAN"

    async def fetch(self, _client):
        record = FinancingRecord(
            source=self.source,
            venue=self.venue,
            product=self.product,
            asset_role="BORROWABLE",
            raw_asset_symbol="BTC",
            eligible=True,
            status="ENABLED",
            regular_user_tier="REGULAR",
            rates=({"rate_unit": "APR", "value": "0.05"},),
            terms=(),
            limits={"min": "1"},
            pair_symbols=(),
            raw={"currency": "btc"},
        )
        return FinancingSnapshot(
            self.source,
            self.venue,
            self.product,
            "2026-07-06T00:00:00+00:00",
            (record,),
        )


def test_collection_bundle_round_trip_applies_market_and_financing_snapshots(tmp_path):
    connectors = [BundleMarketConnector(), BundleFinancingConnector()]
    bundle = asyncio.run(
        export_collection_bundle(venue="TEST", timeout_seconds=1, connectors=connectors)
    )

    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    results = apply_collection_bundle(store, bundle, connectors=connectors)

    assert bundle_succeeded(bundle) is True
    assert [result.ok for result in results] == [True, True]
    assert store.list_markets({})[0]["raw_symbol"] == "BTC_USDT"
    assert store.list_financing({})["financing"][0]["raw_asset_symbol"] == "BTC"
    assert store.list_collection_runs()["runs"][0]["scope"] == "TEST"


def test_collection_bundle_checksum_rejects_tampering_before_database_write(tmp_path):
    connectors = [BundleMarketConnector()]
    bundle = asyncio.run(
        export_collection_bundle(venue="TEST", timeout_seconds=1, connectors=connectors)
    )
    tampered = copy.deepcopy(bundle)
    tampered["entries"][0]["snapshot"]["markets"][0]["raw_symbol"] = "ETH_USDT"
    store = SQLiteStore(tmp_path / "mdv.sqlite3")

    with pytest.raises(ValueError, match="checksum mismatch"):
        apply_collection_bundle(store, tampered, connectors=connectors)

    assert store.market_count() == 0


def test_collection_bundle_rejects_incomplete_registered_source_set(tmp_path):
    connectors = [BundleMarketConnector(), BundleFinancingConnector()]
    bundle = asyncio.run(
        export_collection_bundle(venue="TEST", timeout_seconds=1, connectors=connectors)
    )
    incomplete = copy.deepcopy(bundle)
    incomplete["entries"] = incomplete["entries"][:1]
    unsigned = {key: value for key, value in incomplete.items() if key != "content_sha256"}
    incomplete["content_sha256"] = hashlib.sha256(
        canonical_json(unsigned).encode()
    ).hexdigest()

    with pytest.raises(ValueError, match="source set is incomplete"):
        apply_collection_bundle(
            SQLiteStore(tmp_path / "mdv.sqlite3"), incomplete, connectors=connectors
        )


def test_failed_remote_bundle_records_failure_and_preserves_current_snapshot(tmp_path):
    success_connector = BundleMarketConnector()
    failed_connector = BundleMarketConnector(fail=True)
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    first = asyncio.run(
        export_collection_bundle(
            venue="TEST", timeout_seconds=1, connectors=[success_connector]
        )
    )
    apply_collection_bundle(store, first, connectors=[success_connector])
    failed = asyncio.run(
        export_collection_bundle(
            venue="TEST", timeout_seconds=1, connectors=[failed_connector]
        )
    )

    results = apply_collection_bundle(store, failed, connectors=[failed_connector])

    assert bundle_succeeded(failed) is False
    assert results[0].ok is False
    assert bool(store.list_markets({})[0]["active"]) is True
    assert store.list_collection_runs()["runs"][0]["status"] == "FAILED"
