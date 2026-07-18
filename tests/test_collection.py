import asyncio
from dataclasses import replace

import httpx
import pytest

from mdv.cli import build_parser
from mdv.collection import CollectionService
from mdv.connectors.base import fetch_json
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


class ConcurrentFakeConnector(FakeConnector):
    def __init__(self, *, source: str, venue: str, state: dict[str, int]):
        super().__init__(source=source, venue=venue)
        self.state = state

    async def fetch(self, client):
        self.state["active"] += 1
        self.state["peak"] = max(self.state["peak"], self.state["active"])
        try:
            await asyncio.sleep(0.01)
            return await super().fetch(client)
        finally:
            self.state["active"] -= 1


class PartialFakeConnector(FakeConnector):
    async def fetch(self, client):
        valid = await super().fetch(client)
        malformed = replace(
            valid.markets[0],
            raw_symbol=f"{self.venue}BADUSDT",
            base_symbol=f"{self.venue}BAD",
            quote_symbol="",
            raw={"symbol": f"{self.venue}BADUSDT"},
        )
        return replace(valid, markets=(valid.markets[0], malformed))


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


def test_collection_service_reports_symbol_partial_after_applying_valid_rows(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    service = CollectionService(
        store,
        connectors=[PartialFakeConnector(source="BINANCE_SPOT", venue="BINANCE")],
    )

    result = asyncio.run(service.collect_all())[0]

    assert result.ok is False
    assert result.records == 1
    assert "BINANCEBADUSDT" in result.error
    assert store.market_count() == 1
    saved = store.list_collection_runs()["runs"][0]
    assert saved["status"] == "PARTIAL"
    assert saved["venues"][0]["status"] == "PARTIAL"
    assert saved["venues"][0]["universes"][0]["record_count"] == 1


@pytest.mark.parametrize(
    ("setting", "value", "message"),
    [
        ("timeout_seconds", 0, "timeout_seconds must be positive"),
        ("max_concurrent_fetches", 0, "max_concurrent_fetches must be positive"),
        ("stale_after_seconds", 0, "stale_after_seconds must be positive"),
        (
            "unchanged_observation_retention_days",
            -1,
            "unchanged_observation_retention_days must not be negative",
        ),
        (
            "changed_payload_retention_days",
            -1,
            "changed_payload_retention_days must not be negative",
        ),
        (
            "max_retained_observations_per_table",
            -1,
            "max_retained_observations_per_table must not be negative",
        ),
    ],
)
def test_collection_rejects_unsafe_operational_limits(
    tmp_path, setting, value, message
):
    with pytest.raises(ValueError, match=message):
            CollectionService(
                SQLiteStore(tmp_path / "mdv.sqlite3"),
                connectors=[FakeConnector(source="BINANCE_SPOT", venue="BINANCE")],
                **{setting: value},
            )


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


def test_collection_bounds_fetches_and_rebuilds_projections_once(tmp_path, monkeypatch):
    state = {"active": 0, "peak": 0}
    connectors = [
        ConcurrentFakeConnector(source=f"VENUE{index}_SPOT", venue=f"VENUE{index}", state=state)
        for index in range(5)
    ]
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    rebuild_calls = []
    original_rebuild = store.rebuild_collection_projections

    def rebuild(**kwargs):
        rebuild_calls.append(kwargs)
        return original_rebuild(**kwargs)

    monkeypatch.setattr(store, "rebuild_collection_projections", rebuild)
    results = asyncio.run(
        CollectionService(
            store, connectors=connectors, max_concurrent_fetches=2
        ).collect_all()
    )

    assert [result.source for result in results] == [connector.source for connector in connectors]
    assert state["peak"] == 2
    assert len(rebuild_calls) == 1
    assert store.market_count() == 5


def test_snapshot_validation_rejects_cross_universe_and_naive_timestamps():
    connector = FakeConnector(source="BINANCE_SPOT", venue="BINANCE")
    valid = asyncio.run(connector.fetch(None))
    with pytest.raises(ValueError, match="without a timezone"):
        replace(valid, observed_at="2026-07-03T00:00:00").validate()
    mixed = replace(
        valid,
        markets=(valid.markets[0], replace(valid.markets[0], raw_symbol="BAD", venue="MEXC")),
    )
    mixed.validate()
    assert len(mixed.markets) == 1
    assert len(mixed.issues) == 1
    assert "another venue" in mixed.issues[0].error
    settled = replace(
        valid,
        markets=(
            valid.markets[0],
            replace(valid.markets[0], raw_symbol="SETTLED", settle_symbol="USDT"),
        ),
    )
    settled.validate()
    assert len(settled.markets) == 1
    assert "settled spot market" in settled.issues[0].error


def test_connector_retry_is_bounded_and_skips_permanent_http_errors(monkeypatch):
    calls = {"permanent": 0, "transient": 0}

    def permanent(request):
        calls["permanent"] += 1
        return httpx.Response(400, request=request)

    transient_responses = iter((503, 200))

    def transient(request):
        calls["transient"] += 1
        status = next(transient_responses)
        return httpx.Response(status, request=request, json={"ok": True})

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("mdv.connectors.base.asyncio.sleep", no_sleep)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(permanent)) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await fetch_json(client, "https://example.test", attempts=3)
        async with httpx.AsyncClient(transport=httpx.MockTransport(transient)) as client:
            assert await fetch_json(client, "https://example.test", attempts=3) == {
                "ok": True
            }

    asyncio.run(run())
    assert calls == {"permanent": 1, "transient": 2}
