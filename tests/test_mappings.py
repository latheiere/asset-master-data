import base64
import math
import time
from concurrent.futures import ThreadPoolExecutor

import yaml
from fastapi.testclient import TestClient

from mdv.auth import hash_password
from mdv.config import Settings
from mdv.db import SQLiteStore
from mdv.matching import stable_asset_id
from mdv.models import MarketRecord, MarketSnapshot
from mdv.web import create_app


OBSERVED_AT = "2026-07-03T14:41:10+00:00"
TARGET = {
    "venue": "GATE",
    "market_type": "FUTURE",
    "product": "PERP",
    "contract_type": "PERP",
    "quote_symbol": "USDT",
    "settle_symbol": "USDT",
    "status": "TRADING",
    "venue_product": "USDT-PERP",
    "contract_direction": "LINEAR",
}


def market(
    *,
    source: str,
    venue: str,
    raw_symbol: str,
    base_symbol: str,
    market_type: str = "SPOT",
    product: str = "SPOT",
    contract_type: str = "SPOT",
    quote_symbol: str = "USDT",
    settle_symbol: str | None = None,
    status: str = "TRADING",
    active: bool = True,
    venue_product: str | None = None,
    contract_direction: str | None = None,
    expiry_cycle: str | None = None,
) -> MarketRecord:
    return MarketRecord(
        source=source,
        venue=venue,
        market_type=market_type,
        product=product,
        raw_symbol=raw_symbol,
        base_symbol=base_symbol,
        quote_symbol=quote_symbol,
        settle_symbol=settle_symbol,
        contract_type=contract_type,
        status=status,
        active=active,
        contract_multiplier=None,
        raw={"symbol": raw_symbol},
        venue_product=venue_product or product,
        venue_status=status,
        contract_direction=contract_direction,
        expiry_cycle=expiry_cycle,
    )


def apply(store: SQLiteStore, row: MarketRecord) -> None:
    store.apply_snapshot(
        MarketSnapshot(
            source=row.source,
            venue=row.venue,
            market_type=row.market_type,
            product=row.venue_product or row.product,
            observed_at=OBSERVED_AT,
            markets=(row,),
        )
    )


def seed_resolved(store: SQLiteStore, symbol: str) -> tuple[MarketRecord, MarketRecord]:
    source = market(
        source=f"BINANCE_{symbol}_SPOT",
        venue="BINANCE",
        raw_symbol=f"{symbol}USDT",
        base_symbol=symbol,
    )
    target = market(
        source=f"GATE_{symbol}_USDT_FUTURE",
        venue="GATE",
        raw_symbol=f"{symbol}_USDT",
        base_symbol=symbol,
        market_type="FUTURE",
        product="PERP",
        contract_type="PERP",
        settle_symbol="USDT",
        venue_product="USDT-PERP",
        contract_direction="LINEAR",
    )
    apply(store, source)
    apply(store, target)
    return source, target


def resolve(store: SQLiteStore, symbols: list[str], target: dict | None = None) -> dict:
    return store.resolve_venue_mappings(
        source={"venue": "BINANCE", "symbol_type": "BASE"},
        target=target or TARGET,
        symbols=symbols,
    )


def app_client(tmp_path, store: SQLiteStore) -> tuple[TestClient, dict[str, str]]:
    entitlements_path = tmp_path / "entitlements.yaml"
    entitlements_path.write_text(
        yaml.safe_dump(
            {
                "session_secret": "mapping-test-session-secret-at-least-32-characters",
                "users": {"bot": {"password_hash": hash_password("secret")}},
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        db_path=store.path,
        host="127.0.0.1",
        port=8090,
        http_timeout_seconds=1,
        collection_schedule="*-*-* 00:00:00 UTC",
        entitlements_path=entitlements_path,
        session_cookie_name="mdv_session",
        session_ttl_seconds=3600,
        session_cookie_secure=False,
    )
    token = base64.b64encode(b"bot:secret").decode("ascii")
    return TestClient(create_app(settings=settings, store=store)), {
        "Authorization": f"Basic {token}"
    }


def request_payload(symbols: list[str], **target_overrides) -> dict:
    return {
        "source": {"venue": "binance", "symbol_type": "base", "symbols": symbols},
        "target": {**TARGET, **target_overrides},
    }


def test_resolves_multiple_symbols_in_input_order_from_one_snapshot(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    seed_resolved(store, "BTC")
    seed_resolved(store, "ETH")

    response = resolve(store, ["ETH", "MISSING", "BTC"])

    assert response["schema_version"] == "1"
    assert response["snapshot_revision"] == "2026-07-03T14:41:10Z"
    assert [result["source_symbol"] for result in response["results"]] == [
        "ETH",
        "MISSING",
        "BTC",
    ]
    assert [result["status"] for result in response["results"]] == [
        "resolved",
        "source_not_found",
        "resolved",
    ]
    assert response["results"][0]["target"] == {
        "market_id": "GATE_ETH_USDT_FUTURE:ETH_USDT",
        "raw_symbol": "ETH_USDT",
        "base_symbol": "ETH",
        "last_seen_at": OBSERVED_AT,
    }


def test_excludes_ineligible_target_markets(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    cases = [
        ("INACTIVE", {"active": False}),
        ("PAUSED", {"status": "PAUSED"}),
        ("WRONGQUOTE", {"quote_symbol": "USDC"}),
        ("WRONGSETTLE", {"settle_symbol": "USDC"}),
        (
            "DATED",
            {
                "product": "DATED",
                "contract_type": "DATED",
                "venue_product": "USDT-DELIVERY",
                "expiry_cycle": "Q",
            },
        ),
    ]
    for symbol, overrides in cases:
        apply(
            store,
            market(
                source=f"BINANCE_{symbol}",
                venue="BINANCE",
                raw_symbol=f"{symbol}USDT",
                base_symbol=symbol,
            ),
        )
        target_values = {
            "source": f"GATE_{symbol}",
            "venue": "GATE",
            "raw_symbol": f"{symbol}_USDT",
            "base_symbol": symbol,
            "market_type": "FUTURE",
            "product": "PERP",
            "contract_type": "PERP",
            "settle_symbol": "USDT",
            "venue_product": "USDT-PERP",
            "contract_direction": "LINEAR",
            **overrides,
        }
        apply(store, market(**target_values))

    response = resolve(store, [symbol for symbol, _ in cases])

    assert [result["status"] for result in response["results"]] == [
        "target_not_found"
    ] * len(cases)


def test_reports_ambiguous_source_and_target_without_guessing(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    source, _ = seed_resolved(store, "BTC")
    second_source = market(
        source="BINANCE_BTC_SECOND",
        venue="BINANCE",
        raw_symbol="BTCFDUSD",
        base_symbol="BTC",
        quote_symbol="FDUSD",
    )
    apply(store, second_source)
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO assets(asset_id, canonical_symbol, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("alternate-btc", "BTC-ALTERNATE", OBSERVED_AT, OBSERVED_AT),
        )
        conn.execute(
            "UPDATE market_asset_mappings SET asset_id = ? WHERE market_id = ?",
            ("alternate-btc", second_source.market_id),
        )

    assert resolve(store, ["BTC"])["results"][0] == {
        "source_symbol": "BTC",
        "status": "ambiguous_source",
        "error_code": "MULTIPLE_SOURCE_ASSETS",
    }

    store = SQLiteStore(tmp_path / "ambiguous-target.sqlite3")
    source, _ = seed_resolved(store, "ETH")
    apply(
        store,
        market(
            source="GATE_ETH_SECOND",
            venue="GATE",
            raw_symbol="ETH_USDT_SECOND",
            base_symbol="ETH",
            market_type="FUTURE",
            product="PERP",
            contract_type="PERP",
            settle_symbol="USDT",
            venue_product="USDT-PERP",
            contract_direction="LINEAR",
        ),
    )

    result = resolve(store, ["ETH"])["results"][0]
    assert result["status"] == "ambiguous_target"
    assert result["error_code"] == "MULTIPLE_TARGETS"
    assert result["canonical_symbol"] == "ETH"


def test_resolves_unit_prefixed_source_to_canonical_target(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    apply(
        store,
        market(
            source="BINANCE_BONK",
            venue="BINANCE",
            raw_symbol="1000BONKUSDT",
            base_symbol="1000BONK",
        ),
    )
    apply(
        store,
        market(
            source="GATE_BONK",
            venue="GATE",
            raw_symbol="BONK_USDT",
            base_symbol="BONK",
            market_type="FUTURE",
            product="PERP",
            contract_type="PERP",
            settle_symbol="USDT",
            venue_product="USDT-PERP",
            contract_direction="LINEAR",
        ),
    )

    result = resolve(store, ["1000BONK"])["results"][0]

    assert result["status"] == "resolved"
    assert result["canonical_symbol"] == "BONK"
    assert result["target"]["raw_symbol"] == "BONK_USDT"


def test_marks_preserved_data_stale_after_failed_target_refresh(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    _, target = seed_resolved(store, "BTC")
    store.record_failed_run(
        source=target.source,
        venue=target.venue,
        market_type=target.market_type,
        product=target.venue_product or target.product,
        error="temporary endpoint failure",
    )

    result = resolve(store, ["BTC"])["results"][0]

    assert result["status"] == "stale"
    assert result["error_code"] == "STALE_SNAPSHOT"


def test_endpoint_auth_validation_and_minimal_response(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    seed_resolved(store, "BTC")
    client, headers = app_client(tmp_path, store)
    payload = request_payload(["btc"])

    with client:
        unauthorized = client.post("/api/v1/mappings/resolve", json=payload)
        response = client.post(
            "/api/v1/mappings/resolve", json=payload, headers=headers
        )
        duplicate = client.post(
            "/api/v1/mappings/resolve",
            json=request_payload(["BTC", "btc"]),
            headers=headers,
        )
        empty = client.post(
            "/api/v1/mappings/resolve",
            json=request_payload([""]),
            headers=headers,
        )
        oversized = client.post(
            "/api/v1/mappings/resolve",
            json=request_payload([f"SYMBOL{position}" for position in range(101)]),
            headers=headers,
        )
        legacy_product = client.post(
            "/api/v1/mappings/resolve",
            json=request_payload(["BTC"], product="USDT-PERP"),
            headers=headers,
        )

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    assert response.json()["results"][0]["status"] == "resolved"
    assert len(response.content) < 1024
    assert b"raw_json" not in response.content
    assert b"tags" not in response.content
    assert duplicate.status_code == 422
    assert empty.status_code == 422
    assert oversized.status_code == 422
    assert legacy_product.status_code == 422


def seed_performance_store(store: SQLiteStore, count: int = 100) -> list[str]:
    store.migrate()
    symbols = [f"PERF{position:03d}" for position in range(count)]
    with store.transaction() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO venues(venue, display_name) VALUES (?, ?)",
            [("BINANCE", "BINANCE"), ("GATE", "GATE")],
        )
        conn.executemany(
            """
            INSERT INTO ingest_runs(
                run_id, source, venue, market_type, product, started_at,
                completed_at, status, complete, record_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'SUCCEEDED', 1, ?)
            """,
            [
                (
                    "perf-source-run", "PERF_BINANCE", "BINANCE", "SPOT", "SPOT",
                    OBSERVED_AT, OBSERVED_AT, count,
                ),
                (
                    "perf-target-run", "PERF_GATE", "GATE", "FUTURE", "USDT-PERP",
                    OBSERVED_AT, OBSERVED_AT, count,
                ),
            ],
        )
        for position, symbol in enumerate(symbols):
            asset_id = stable_asset_id(symbol)
            source_id = f"PERF_BINANCE:{symbol}USDT"
            target_id = f"PERF_GATE:{symbol}_USDT"
            conn.execute(
                "INSERT INTO assets(asset_id, canonical_symbol, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (asset_id, symbol, OBSERVED_AT, OBSERVED_AT),
            )
            for values in (
                (
                    source_id, "PERF_BINANCE", "BINANCE", "SPOT", "SPOT",
                    f"{symbol}USDT", symbol, "USDT", None, "SPOT", None, "SPOT",
                ),
                (
                    target_id, "PERF_GATE", "GATE", "FUTURE", "PERP",
                    f"{symbol}_USDT", symbol, "USDT", "USDT", "PERP", "LINEAR",
                    "USDT-PERP",
                ),
            ):
                (
                    market_id, source, venue, market_type, product, raw_symbol,
                    base_symbol, quote_symbol, settle_symbol, contract_type,
                    direction, venue_product,
                ) = values
                conn.execute(
                    """
                    INSERT INTO markets(
                        market_id, source, venue, market_type, product, raw_symbol,
                        base_symbol, quote_symbol, settle_symbol, contract_type,
                        status, active, underlying_multiplier, first_seen_at,
                        last_seen_at, raw_json, content_hash, venue_product,
                        venue_status, contract_direction
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'TRADING', 1, '1', ?, ?, '{}', ?, ?, 'TRADING', ?)
                    """,
                    (
                        market_id, source, venue, market_type, product, raw_symbol,
                        base_symbol, quote_symbol, settle_symbol, contract_type,
                        OBSERVED_AT, OBSERVED_AT, market_id, venue_product, direction,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO market_asset_mappings(
                        market_id, asset_id, venue_symbol, normalized_symbol,
                        method, confidence, matcher_version, updated_at, evidence_json
                    ) VALUES (?, ?, ?, ?, 'PERF_FIXTURE', 1, 'test', ?, '{}')
                    """,
                    (market_id, asset_id, base_symbol, symbol, OBSERVED_AT),
                )
    return symbols


def percentile_95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def test_resolver_latency_size_and_concurrency_benchmarks(tmp_path):
    store = SQLiteStore(tmp_path / "mdv.sqlite3")
    symbols = seed_performance_store(store)
    client, headers = app_client(tmp_path, store)

    with client:
        client.post(
            "/api/v1/mappings/resolve",
            json=request_payload([symbols[0]]),
            headers=headers,
        )

        single_latencies = []
        for _ in range(20):
            started = time.perf_counter()
            response = client.post(
                "/api/v1/mappings/resolve",
                json=request_payload([symbols[0]]),
                headers=headers,
            )
            single_latencies.append((time.perf_counter() - started) * 1000)
            assert response.status_code == 200

        started = time.perf_counter()
        ten = client.post(
            "/api/v1/mappings/resolve",
            json=request_payload(symbols[:10]),
            headers=headers,
        )
        ten_latency = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        hundred = client.post(
            "/api/v1/mappings/resolve",
            json=request_payload(symbols),
            headers=headers,
        )
        hundred_latency = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=10) as executor:
            concurrent = list(
                executor.map(
                    lambda _: client.post(
                        "/api/v1/mappings/resolve",
                        json=request_payload(symbols[:10]),
                        headers=headers,
                    ),
                    range(10),
                )
            )
        concurrent_latency = (time.perf_counter() - started) * 1000

    assert percentile_95(single_latencies) <= 20
    assert ten_latency <= 40
    assert hundred_latency <= 100
    assert len(hundred.content) <= 100 * 1024
    assert all(response.status_code == 200 for response in concurrent)
    assert concurrent_latency <= 100
