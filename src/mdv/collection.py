from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass

import httpx

from mdv import __version__
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
    def __init__(
        self,
        store: SQLiteStore,
        *,
        timeout_seconds: float = 20,
        connectors: list[Connector] | None = None,
        max_concurrent_fetches: int = 2,
        stale_after_seconds: int = 7200,
        unchanged_observation_retention_days: int = 30,
        changed_payload_retention_days: int = 7,
        max_retained_observations_per_table: int = 100_000,
    ):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_concurrent_fetches <= 0:
            raise ValueError("max_concurrent_fetches must be positive")
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        if unchanged_observation_retention_days < 0:
            raise ValueError(
                "unchanged_observation_retention_days must not be negative"
            )
        if changed_payload_retention_days < 0:
            raise ValueError("changed_payload_retention_days must not be negative")
        if max_retained_observations_per_table < 0:
            raise ValueError(
                "max_retained_observations_per_table must not be negative"
            )
        self.store = store
        self.timeout_seconds = timeout_seconds
        self.connectors = connectors or default_collection_connectors()
        self.max_concurrent_fetches = max_concurrent_fetches
        self.stale_after_seconds = stale_after_seconds
        self.unchanged_observation_retention_days = unchanged_observation_retention_days
        self.changed_payload_retention_days = changed_payload_retention_days
        self.max_retained_observations_per_table = (
            max_retained_observations_per_table
        )

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
        with self.store.collection_writer_lease():
            self.store.reconcile_stale_collection_runs(
                stale_after_seconds=self.stale_after_seconds
            )
            results = await self._collect_unlocked(
                venue=venue, exclude_venues=exclude_venues
            )
            self.store.compact_audit_history(
                unchanged_retention_days=self.unchanged_observation_retention_days,
                changed_payload_retention_days=self.changed_payload_retention_days,
                max_retained_observations_per_table=(
                    self.max_retained_observations_per_table
                ),
            )
            return results

    async def _collect_unlocked(
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
        headers = {
            "User-Agent": (
                f"Mozilla/5.0 (compatible; AssetMasterData/{__version__})"
            )
        }
        results: list[CollectionResult | None] = [None] * len(connectors)
        tag_run_id: str | None = None
        tag_observed_at: str | None = None
        tag_result_index: int | None = None

        async def fetch(index: int, connector: Connector, client: httpx.AsyncClient):
            try:
                async with semaphore:
                    return index, connector, await connector.fetch(client)
            except Exception as exc:
                return index, connector, exc

        semaphore = asyncio.Semaphore(self.max_concurrent_fetches)
        try:
            limits = httpx.Limits(
                max_connections=max(4, self.max_concurrent_fetches * 3),
                max_keepalive_connections=max(2, self.max_concurrent_fetches * 2),
            )
            async with httpx.AsyncClient(
                timeout=timeout,
                headers=headers,
                follow_redirects=True,
                limits=limits,
            ) as client:
                pending = {
                    asyncio.create_task(fetch(index, connector, client))
                    for index, connector in enumerate(connectors)
                }
                while pending:
                    completed, pending = await asyncio.wait(
                        pending, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in completed:
                        index, connector, value = task.result()
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
                            results[index] = CollectionResult(
                                connector.source, False, 0, run_id, collection_run_id, error
                            )
                            continue
                        try:
                            run_id = (
                                self.store.apply_financing_snapshot(
                                    value, collection_run_id=collection_run_id, rebuild=False
                                )
                                if isinstance(value, FinancingSnapshot)
                                else self.store.apply_snapshot(
                                    value, collection_run_id=collection_run_id, rebuild=False
                                )
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
                            results[index] = CollectionResult(
                                connector.source, False, 0, run_id, collection_run_id, error
                            )
                            continue
                        if not isinstance(value, FinancingSnapshot) and (
                            tag_observed_at is None or value.observed_at > tag_observed_at
                        ):
                            tag_run_id = run_id
                            tag_observed_at = value.observed_at
                            tag_result_index = index
                        record_count = (
                            len(value.records)
                            if isinstance(value, FinancingSnapshot)
                            else len(value.markets)
                        )
                        symbol_error = None
                        if not isinstance(value, FinancingSnapshot) and value.issues:
                            details = "; ".join(
                                f"{issue.raw_symbol}: {issue.error}"
                                for issue in value.issues
                            )
                            symbol_error = (
                                f"{len(value.issues)} symbol error(s): {details}"
                            )[:2000]
                        results[index] = CollectionResult(
                            connector.source,
                            symbol_error is None,
                            record_count,
                            run_id,
                            collection_run_id,
                            symbol_error,
                        )
        except Exception as exc:
            for index, connector in enumerate(connectors):
                if results[index] is not None:
                    continue
                error = f"{type(exc).__name__}: {exc}"
                run_id = self.store.record_failed_run(
                    source=connector.source,
                    venue=connector.venue,
                    market_type=connector.market_type,
                    product=connector.product,
                    error=error,
                    collection_run_id=collection_run_id,
                )
                results[index] = CollectionResult(
                    connector.source, False, 0, run_id, collection_run_id, error
                )
        projection_error: str | None = None
        if any(result is not None and result.ok for result in results):
            try:
                self.store.rebuild_collection_projections(
                    tag_run_id=tag_run_id, tag_observed_at=tag_observed_at
                )
            except Exception as exc:
                projection_error = f"projection rebuild failed: {type(exc).__name__}: {exc}"
                failed_index = tag_result_index
                if failed_index is None:
                    failed_index = next(
                        index
                        for index, result in enumerate(results)
                        if result is not None and result.ok
                    )
                completed = results[failed_index]
                assert completed is not None
                results[failed_index] = CollectionResult(
                    completed.source,
                    False,
                    completed.records,
                    completed.run_id,
                    completed.collection_run_id,
                    projection_error,
                )
        self.store.finish_collection_run(collection_run_id, error=projection_error)
        return [result for result in results if result is not None]


def results_json(results: list[CollectionResult]) -> list[dict]:
    return [asdict(result) for result in results]


def collection_json(results: list[CollectionResult], *, scope: str) -> dict:
    return {
        "collection_run_id": results[0].collection_run_id if results else None,
        "scope": scope,
        "ok": bool(results) and all(result.ok for result in results),
        "results": results_json(results),
    }
