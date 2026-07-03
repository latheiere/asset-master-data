#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from mdv.auth import Entitlements, hash_password
from mdv.config import DEFAULT_CONFIG_PATH, Settings
from mdv.db import SQLiteStore
from mdv.web import create_app


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def eligible_symbols(store: SQLiteStore, limit: int = 100) -> list[str]:
    with store.readonly() as conn:
        rows = conn.execute(
            """
            SELECT source_market.base_symbol
            FROM markets AS source_market
            JOIN market_asset_mappings AS source_mapping
              ON source_mapping.market_id = source_market.market_id
            JOIN market_asset_mappings AS target_mapping
              ON target_mapping.asset_id = source_mapping.asset_id
            JOIN markets AS target_market
              ON target_market.market_id = target_mapping.market_id
            WHERE source_market.venue = 'BINANCE'
              AND source_market.active = 1
              AND target_market.venue = 'GATE'
              AND target_market.active = 1
              AND target_market.market_type = 'FUTURE'
              AND target_market.product = 'PERP'
              AND target_market.contract_type = 'PERP'
              AND target_market.quote_symbol = 'USDT'
              AND target_market.settle_symbol = 'USDT'
              AND target_market.status = 'TRADING'
              AND target_market.venue_product = 'USDT-PERP'
              AND target_market.contract_direction = 'LINEAR'
            GROUP BY source_market.base_symbol
            HAVING COUNT(DISTINCT source_mapping.asset_id) = 1
               AND COUNT(DISTINCT target_market.market_id) = 1
            ORDER BY source_market.base_symbol
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [str(row[0]) for row in rows]


def payload(symbols: list[str]) -> dict:
    return {
        "source": {
            "venue": "BINANCE",
            "symbol_type": "BASE",
            "symbols": symbols,
        },
        "target": {
            "venue": "GATE",
            "market_type": "FUTURE",
            "product": "PERP",
            "contract_type": "PERP",
            "quote_symbol": "USDT",
            "settle_symbol": "USDT",
            "status": "TRADING",
            "venue_product": "USDT-PERP",
            "contract_direction": "LINEAR",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--threshold-ms", type=float, default=100)
    args = parser.parse_args()

    settings = Settings.from_yaml(args.config)
    store = SQLiteStore(settings.db_path)
    store.migrate()
    symbols = eligible_symbols(store)
    if len(symbols) < 100:
        raise RuntimeError(f"need 100 resolvable symbols, found {len(symbols)}")

    username = "resolver-benchmark"
    password = "ephemeral-benchmark-password"
    entitlements = Entitlements(
        session_secret=b"ephemeral-benchmark-session-secret-32-bytes",
        users={username: hash_password(password)},
    )
    app = create_app(settings=settings, store=store, entitlements=entitlements)
    client = TestClient(app)
    basic = base64.b64encode(f"{username}:{password}".encode()).decode()
    headers = {"Authorization": f"Basic {basic}"}

    warmup = client.post(
        "/api/v1/mappings/resolve", json=payload(symbols[:1]), headers=headers
    )
    warmup.raise_for_status()

    batch_limits = {1: 20.0, 10: 40.0, 100: 100.0}
    report: dict[str, object] = {
        "iterations": args.iterations,
        "batch_limits_ms": batch_limits,
        "batches": {},
    }
    failures = []
    for size in (1, 10, 100):
        durations = []
        response_size = 0
        for _ in range(args.iterations):
            started = time.perf_counter()
            response = client.post(
                "/api/v1/mappings/resolve",
                json=payload(symbols[:size]),
                headers=headers,
            )
            durations.append((time.perf_counter() - started) * 1000)
            response.raise_for_status()
            response_size = len(response.content)
            if any(item["status"] != "resolved" for item in response.json()["results"]):
                raise RuntimeError(f"batch {size} returned an unresolved result")
        metrics = {
            "p50_ms": round(percentile(durations, 0.50), 3),
            "p95_ms": round(percentile(durations, 0.95), 3),
            "max_ms": round(max(durations), 3),
            "response_bytes": response_size,
            "bytes_per_symbol": round(response_size / size, 1),
        }
        report["batches"][str(size)] = metrics
        if metrics["p95_ms"] > min(args.threshold_ms, batch_limits[size]):
            failures.append(f"batch {size} p95 {metrics['p95_ms']} ms")
        if metrics["bytes_per_symbol"] > 1024:
            failures.append(
                f"batch {size} response {metrics['bytes_per_symbol']} bytes/symbol"
            )

    def concurrent_request(_: int) -> tuple[float, int]:
        started = time.perf_counter()
        response = client.post(
            "/api/v1/mappings/resolve",
            json=payload(symbols[:10]),
            headers=headers,
        )
        return (time.perf_counter() - started) * 1000, response.status_code

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=10) as executor:
        concurrent = list(executor.map(concurrent_request, range(10)))
    wall_ms = (time.perf_counter() - started) * 1000
    concurrent_durations = [item[0] for item in concurrent]
    concurrent_metrics = {
        "requests": 10,
        "batch_size": 10,
        "wall_ms": round(wall_ms, 3),
        "p95_ms": round(percentile(concurrent_durations, 0.95), 3),
        "max_ms": round(max(concurrent_durations), 3),
    }
    report["concurrent"] = concurrent_metrics
    if any(status != 200 for _, status in concurrent):
        failures.append("concurrent request returned non-200")
    if concurrent_metrics["p95_ms"] > args.threshold_ms:
        failures.append(f"concurrent p95 {concurrent_metrics['p95_ms']} ms")

    report["threshold_ms"] = args.threshold_ms
    report["ok"] = not failures
    report["failures"] = failures
    print(json.dumps(report, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
