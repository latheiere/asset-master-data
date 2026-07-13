from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict
from typing import Any

import httpx

from mdv import __version__, build_revision
from mdv.collection import CollectionResult
from mdv.connectors import default_collection_connectors
from mdv.connectors.base import Connector, utc_now
from mdv.db import SQLiteStore
from mdv.models import FinancingRecord, FinancingSnapshot, MarketRecord, MarketSnapshot


BUNDLE_FORMAT = "mdv.collection-bundle"
BUNDLE_FORMAT_VERSION = 1


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(payload: dict) -> str:
    content = {key: value for key, value in payload.items() if key != "content_sha256"}
    return hashlib.sha256(canonical_json(content).encode()).hexdigest()


async def export_collection_bundle(
    *,
    venue: str,
    timeout_seconds: float,
    connectors: list[Connector] | None = None,
    max_concurrent_fetches: int = 2,
) -> dict:
    if max_concurrent_fetches <= 0:
        raise ValueError("max_concurrent_fetches must be positive")
    requested = str(venue or "").strip().upper()
    available = connectors or default_collection_connectors()
    selected = [connector for connector in available if connector.venue == requested]
    if not selected:
        choices = ", ".join(sorted({connector.venue for connector in available}))
        raise ValueError(f"VENUE must be one of: {choices}")
    timeout = httpx.Timeout(timeout_seconds)
    headers = {
        "User-Agent": f"Mozilla/5.0 (compatible; AssetMasterData/{__version__})"
    }
    semaphore = asyncio.Semaphore(max_concurrent_fetches)

    async def fetch(connector: Connector, client: httpx.AsyncClient):
        async with semaphore:
            return await connector.fetch(client)

    try:
        limits = httpx.Limits(
            max_connections=max(4, max_concurrent_fetches * 3),
            max_keepalive_connections=max(2, max_concurrent_fetches * 2),
        )
        async with httpx.AsyncClient(
            timeout=timeout, headers=headers, follow_redirects=True, limits=limits
        ) as client:
            values = await asyncio.gather(
                *(fetch(connector, client) for connector in selected),
                return_exceptions=True,
            )
    except Exception as exc:
        values = [exc for _ in selected]
    entries = []
    for connector, value in zip(selected, values, strict=True):
        entry: dict[str, Any] = {
            "source": connector.source,
            "venue": connector.venue,
            "market_type": connector.market_type,
            "product": connector.product,
        }
        if isinstance(value, BaseException):
            entry.update(
                status="FAILED",
                error=f"{type(value).__name__}: {value}",
            )
        else:
            try:
                value.validate()
            except Exception as exc:
                entry.update(
                    status="FAILED", error=f"{type(exc).__name__}: {exc}"
                )
            else:
                entry.update(
                    status="SUCCEEDED",
                    snapshot_type=(
                        "FINANCING"
                        if isinstance(value, FinancingSnapshot)
                        else "MARKET"
                    ),
                    snapshot=json.loads(canonical_json(asdict(value))),
                )
        entries.append(entry)
    bundle = {
        "format": BUNDLE_FORMAT,
        "format_version": BUNDLE_FORMAT_VERSION,
        "producer": {"version": __version__, "revision": build_revision()},
        "scope": requested,
        "created_at": utc_now(),
        "entries": entries,
    }
    bundle["content_sha256"] = _digest(bundle)
    return bundle


def bundle_succeeded(bundle: dict) -> bool:
    entries = bundle.get("entries")
    return bool(entries) and all(
        isinstance(entry, dict) and entry.get("status") == "SUCCEEDED"
        for entry in entries
    )


def apply_collection_bundle(
    store: SQLiteStore,
    payload: object,
    *,
    connectors: list[Connector] | None = None,
) -> list[CollectionResult]:
    with store.collection_writer_lease():
        return _apply_collection_bundle_unlocked(
            store, payload, connectors=connectors
        )


def _apply_collection_bundle_unlocked(
    store: SQLiteStore,
    payload: object,
    *,
    connectors: list[Connector] | None = None,
) -> list[CollectionResult]:
    entries, metadata = _validate_bundle(payload, connectors=connectors)
    scope = metadata["scope"]
    collection_run_id = store.start_collection_run(scope=scope, venues=[scope])
    results = []
    tag_run_id: str | None = None
    tag_observed_at: str | None = None
    tag_result_index: int | None = None
    for entry in entries:
        if entry["status"] == "FAILED":
            error = entry["error"]
            run_id = store.record_failed_run(
                source=entry["source"],
                venue=entry["venue"],
                market_type=entry["market_type"],
                product=entry["product"],
                error=error,
                collection_run_id=collection_run_id,
            )
            results.append(
                CollectionResult(
                    entry["source"], False, 0, run_id, collection_run_id, error
                )
            )
            continue
        snapshot = entry["snapshot"]
        try:
            run_id = (
                store.apply_financing_snapshot(
                    snapshot, collection_run_id=collection_run_id, rebuild=False
                )
                if isinstance(snapshot, FinancingSnapshot)
                else store.apply_snapshot(
                    snapshot, collection_run_id=collection_run_id, rebuild=False
                )
            )
            count = (
                len(snapshot.records)
                if isinstance(snapshot, FinancingSnapshot)
                else len(snapshot.markets)
            )
            results.append(
                CollectionResult(
                    entry["source"], True, count, run_id, collection_run_id
                )
            )
            if not isinstance(snapshot, FinancingSnapshot) and (
                tag_observed_at is None or snapshot.observed_at > tag_observed_at
            ):
                tag_run_id = run_id
                tag_observed_at = snapshot.observed_at
                tag_result_index = len(results) - 1
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            run_id = store.record_failed_run(
                source=entry["source"],
                venue=entry["venue"],
                market_type=entry["market_type"],
                product=entry["product"],
                error=error,
                collection_run_id=collection_run_id,
            )
            results.append(
                CollectionResult(
                    entry["source"], False, 0, run_id, collection_run_id, error
                )
            )
    projection_error: str | None = None
    if any(result.ok for result in results):
        try:
            store.rebuild_collection_projections(
                tag_run_id=tag_run_id, tag_observed_at=tag_observed_at
            )
        except Exception as exc:
            projection_error = f"projection rebuild failed: {type(exc).__name__}: {exc}"
            failed_index = tag_result_index
            if failed_index is None:
                failed_index = next(index for index, result in enumerate(results) if result.ok)
            completed = results[failed_index]
            results[failed_index] = CollectionResult(
                completed.source,
                False,
                completed.records,
                completed.run_id,
                completed.collection_run_id,
                projection_error,
            )
    store.finish_collection_run(collection_run_id, error=projection_error)
    return results


def _validate_bundle(
    payload: object,
    *,
    connectors: list[Connector] | None,
) -> tuple[list[dict], dict]:
    if not isinstance(payload, dict):
        raise ValueError("collection bundle root must be an object")
    if payload.get("format") != BUNDLE_FORMAT:
        raise ValueError("unsupported collection bundle format")
    if payload.get("format_version") != BUNDLE_FORMAT_VERSION:
        raise ValueError("unsupported collection bundle format version")
    digest = payload.get("content_sha256")
    if not isinstance(digest, str) or digest != _digest(payload):
        raise ValueError("collection bundle checksum mismatch")
    scope = str(payload.get("scope") or "").strip().upper()
    entries_payload = payload.get("entries")
    if not scope or not isinstance(entries_payload, list) or not entries_payload:
        raise ValueError("collection bundle has no scope or entries")
    available = connectors or default_collection_connectors()
    expected = {
        connector.source: connector for connector in available if connector.venue == scope
    }
    if not expected:
        raise ValueError(f"collection bundle uses unsupported venue {scope}")
    sources = [
        str(entry.get("source") or "")
        for entry in entries_payload
        if isinstance(entry, dict)
    ]
    if len(sources) != len(entries_payload) or len(sources) != len(set(sources)):
        raise ValueError("collection bundle has malformed or duplicate source entries")
    if set(sources) != set(expected):
        raise ValueError("collection bundle source set is incomplete or unexpected")
    entries = []
    for raw_entry in entries_payload:
        connector = expected[raw_entry["source"]]
        for field in ("venue", "market_type", "product"):
            if raw_entry.get(field) != getattr(connector, field):
                raise ValueError(
                    f"collection bundle {connector.source} metadata does not match registry"
                )
        status = raw_entry.get("status")
        base = {
            "source": connector.source,
            "venue": connector.venue,
            "market_type": connector.market_type,
            "product": connector.product,
            "status": status,
        }
        if status == "FAILED":
            error = raw_entry.get("error")
            if not isinstance(error, str) or not error:
                raise ValueError(f"collection bundle {connector.source} has no failure error")
            base["error"] = error
        elif status == "SUCCEEDED":
            base["snapshot"] = _decode_snapshot(raw_entry, connector=connector)
        else:
            raise ValueError(f"collection bundle {connector.source} has invalid status")
        entries.append(base)
    return entries, {"scope": scope}


def _decode_snapshot(
    entry: dict, *, connector: Connector
) -> MarketSnapshot | FinancingSnapshot:
    value = entry.get("snapshot")
    if not isinstance(value, dict):
        raise ValueError(f"collection bundle {connector.source} has no snapshot object")
    if entry.get("snapshot_type") == "MARKET":
        rows = value.get("markets")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"collection bundle {connector.source} has invalid markets")
        try:
            snapshot = MarketSnapshot(
                source=value["source"],
                venue=value["venue"],
                market_type=value["market_type"],
                product=value["product"],
                observed_at=value["observed_at"],
                markets=tuple(MarketRecord(**row) for row in rows),
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(
                f"collection bundle {connector.source} market snapshot is malformed"
            ) from exc
    elif entry.get("snapshot_type") == "FINANCING":
        rows = value.get("records")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"collection bundle {connector.source} has invalid financing records")
        try:
            records = tuple(
                FinancingRecord(
                    **{
                        **row,
                        "rates": tuple(row.get("rates", ())),
                        "terms": tuple(row.get("terms", ())),
                        "pair_symbols": tuple(row.get("pair_symbols", ())),
                    }
                )
                for row in rows
            )
            snapshot = FinancingSnapshot(
                source=value["source"],
                venue=value["venue"],
                product=value["product"],
                observed_at=value["observed_at"],
                records=records,
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(
                f"collection bundle {connector.source} financing snapshot is malformed"
            ) from exc
    else:
        raise ValueError(f"collection bundle {connector.source} has invalid snapshot type")
    if (
        snapshot.source != connector.source
        or snapshot.venue != connector.venue
        or snapshot.product != connector.product
    ):
        raise ValueError(f"collection bundle {connector.source} snapshot metadata mismatch")
    if isinstance(snapshot, MarketSnapshot) and snapshot.market_type != connector.market_type:
        raise ValueError(f"collection bundle {connector.source} snapshot market type mismatch")
    snapshot.validate()
    return snapshot
