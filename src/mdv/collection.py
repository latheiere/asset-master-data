from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass

import httpx

from mdv.connectors import default_collection_connectors
from mdv.connectors.base import Connector
from mdv.db import SQLiteStore
from mdv.models import FinancingSnapshot


@dataclass(frozen=True)
class CollectionResult:
    source: str
    ok: bool
    records: int
    run_id: str
    collection_run_id: str
    error: str | None = None


class CollectionService:
    def __init__(self, store: SQLiteStore, *, timeout_seconds: float = 20, connectors: list[Connector] | None = None):
        self.store = store
        self.timeout_seconds = timeout_seconds
        self.connectors = connectors or default_collection_connectors()

    async def collect_all(self) -> list[CollectionResult]:
        return await self.collect()

    async def collect_venue(self, venue: str) -> list[CollectionResult]:
        return await self.collect(venue=venue)

    async def collect(
        self,
        *,
        venue: str | None = None,
        exclude_venues: list[str] | tuple[str, ...] | None = None,
    ) -> list[CollectionResult]:
        requested_venue = str(venue or "").strip().upper()
        available_venues = sorted({connector.venue for connector in self.connectors})
        if requested_venue and requested_venue not in available_venues:
            raise ValueError(f"VENUE must be one of: {', '.join(available_venues)}")
        excluded = {
            str(item or "").strip().upper()
            for item in (exclude_venues or ())
            if str(item or "").strip()
        }
        unknown_excluded = excluded.difference(available_venues)
        if unknown_excluded:
            raise ValueError(f"VENUE must be one of: {', '.join(available_venues)}")
        if requested_venue and excluded:
            raise ValueError("--venue and --exclude-venue cannot be used together")
        connectors = [
            connector for connector in self.connectors
            if (not requested_venue or connector.venue == requested_venue)
            and connector.venue not in excluded
        ]
        if not connectors:
            raise ValueError("collection selection contains no venues")
        scope = requested_venue or (
            "ALL_EXCEPT_" + "_".join(sorted(excluded)) if excluded else "ALL"
        )
        venues = (
            [requested_venue]
            if requested_venue
            else [item for item in available_venues if item not in excluded]
        )
        collection_run_id = self.store.start_collection_run(scope=scope, venues=venues)
        timeout = httpx.Timeout(self.timeout_seconds)
        # Some public venue CDNs reject generic library User-Agent values.
        headers = {"User-Agent": "Mozilla/5.0 (compatible; AssetMasterData/0.1)"}
        try:
            async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
                snapshots = await asyncio.gather(
                    *(connector.fetch(client) for connector in connectors),
                    return_exceptions=True,
                )
        except Exception as exc:
            snapshots = [exc for _ in connectors]
        results = []
        for connector, value in zip(connectors, snapshots, strict=True):
            if isinstance(value, BaseException):
                error = f"{type(value).__name__}: {value}"
                run_id = self.store.record_failed_run(
                    source=connector.source,
                    venue=connector.venue,
                    market_type=connector.market_type,
                    product=connector.product,
                    error=error,
                    collection_run_id=collection_run_id,
                )
                results.append(CollectionResult(connector.source, False, 0, run_id, collection_run_id, error))
                continue
            try:
                run_id = (
                    self.store.apply_financing_snapshot(value, collection_run_id=collection_run_id)
                    if isinstance(value, FinancingSnapshot)
                    else self.store.apply_snapshot(value, collection_run_id=collection_run_id)
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                run_id = self.store.record_failed_run(
                    source=connector.source,
                    venue=connector.venue,
                    market_type=connector.market_type,
                    product=connector.product,
                    error=error,
                    collection_run_id=collection_run_id,
                )
                results.append(CollectionResult(connector.source, False, 0, run_id, collection_run_id, error))
                continue
            record_count = len(value.records) if isinstance(value, FinancingSnapshot) else len(value.markets)
            results.append(CollectionResult(connector.source, True, record_count, run_id, collection_run_id))
        self.store.finish_collection_run(collection_run_id)
        return results


def results_json(results: list[CollectionResult]) -> list[dict]:
    return [asdict(result) for result in results]


def collection_json(results: list[CollectionResult], *, scope: str) -> dict:
    return {
        "collection_run_id": results[0].collection_run_id if results else None,
        "scope": scope,
        "ok": bool(results) and all(result.ok for result in results),
        "results": results_json(results),
    }
