from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections import defaultdict
from fnmatch import fnmatchcase
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Iterator

from mdv.connectors import market_metadata, market_trade_url

from mdv.matching import (
    MATCHER_VERSION,
    evaluate_alias_hint,
    normalize_asset_symbol,
    normalize_venue_asset_symbol,
    score_symbol_groups,
    stable_asset_id,
)
from mdv.models import MarketSnapshot
from mdv.normalization import (
    CONTRACT_DIRECTION_VALUES,
    EXPIRY_CYCLE_VALUES,
    PRODUCT_VALUES,
    STATUS_VALUES,
    contract_direction,
    legacy_expiry_cycle,
    normalize_contract_type,
    normalize_product,
    normalize_status,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalized_tag_key(value: str) -> tuple[str, str]:
    provider, separator, tag = value.strip().partition(":")
    provider = provider.strip().upper()
    tag = tag.strip().upper().replace(" ", "_")
    if not separator or not provider or not tag:
        raise ValueError("TAG must use PROVIDER:NAME, for example BINANCE:MONITORING")
    return provider, tag


def requested_tag_keys(value: object) -> set[tuple[str, str]]:
    raw_values = value if isinstance(value, list) else [value]
    return {
        normalized_tag_key(item)
        for raw in raw_values
        if raw not in (None, "")
        for item in str(raw).split(",")
        if item.strip()
    }


def requested_values(filters: dict[str, object], name: str) -> set[str]:
    value = filters.get(name) or []
    raw_values = value if isinstance(value, list) else [value]
    return {
        item.strip().upper()
        for raw in raw_values
        for item in str(raw).split(",")
        if item.strip()
    }


def requested_contract_values(filters: dict[str, object], suffix: str = "") -> set[str]:
    values = requested_values(filters, f"contract{suffix}")
    return {"PERP" if value == "PERPETUAL" else value for value in values}


class SQLiteStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def readonly(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def migrate(self) -> None:
        conn = self.connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    filename TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            migration_dir = resources.files("mdv.migrations")
            for entry in sorted(migration_dir.iterdir(), key=lambda item: item.name):
                if not entry.name.endswith(".sql"):
                    continue
                version = int(entry.name.split("_", 1)[0])
                if version in applied:
                    continue
                filename = entry.name.replace("'", "''")
                applied_at = utc_now().replace("'", "''")
                conn.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + entry.read_text(encoding="utf-8")
                    + f"\nINSERT INTO schema_migrations(version, filename, applied_at) "
                    f"VALUES ({version}, '{filename}', '{applied_at}');\n"
                    "COMMIT;"
                )
        finally:
            conn.close()

    def market_count(self) -> int:
        self.migrate()
        with self.readonly() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0])

    def start_collection_run(self, *, scope: str, venues: list[str]) -> str:
        self.migrate()
        collection_run_id = str(uuid.uuid4())
        normalized_venues = sorted({venue.strip().upper() for venue in venues if venue.strip()})
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO collection_runs(
                    collection_run_id, scope, requested_venues_json,
                    started_at, status
                ) VALUES (?, ?, ?, ?, 'RUNNING')
                """,
                (collection_run_id, scope.strip().upper(), canonical_json(normalized_venues), utc_now()),
            )
        return collection_run_id

    def finish_collection_run(self, collection_run_id: str, *, error: str | None = None) -> dict:
        self.migrate()
        with self.transaction() as conn:
            counts = dict(conn.execute(
                """
                SELECT
                    COUNT(*) AS universe_count,
                    SUM(CASE WHEN status = 'SUCCEEDED' THEN 1 ELSE 0 END) AS succeeded_count,
                    SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS failed_count,
                    COALESCE(SUM(record_count), 0) AS record_count
                FROM ingest_runs
                WHERE collection_run_id = ?
                """,
                (collection_run_id,),
            ).fetchone())
            universe_count = int(counts["universe_count"] or 0)
            succeeded_count = int(counts["succeeded_count"] or 0)
            failed_count = int(counts["failed_count"] or 0)
            if error or universe_count == 0 or succeeded_count == 0:
                status = "FAILED"
            elif failed_count:
                status = "PARTIAL"
            else:
                status = "SUCCEEDED"
            child_errors = [str(row[0]) for row in conn.execute(
                """
                SELECT error FROM ingest_runs
                WHERE collection_run_id = ? AND error IS NOT NULL AND error != ''
                ORDER BY venue, source
                """,
                (collection_run_id,),
            )]
            combined_error = error or ("\n".join(child_errors) if child_errors else None)
            conn.execute(
                """
                UPDATE collection_runs
                SET completed_at = ?, status = ?, universe_count = ?,
                    succeeded_count = ?, failed_count = ?, record_count = ?,
                    error = ?
                WHERE collection_run_id = ?
                """,
                (
                    utc_now(),
                    status,
                    universe_count,
                    succeeded_count,
                    failed_count,
                    int(counts["record_count"] or 0),
                    combined_error,
                    collection_run_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM collection_runs WHERE collection_run_id = ?",
                (collection_run_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown collection run: {collection_run_id}")
            return dict(row)

    def record_failed_run(
        self,
        *,
        source: str,
        venue: str,
        market_type: str,
        product: str,
        error: str,
        collection_run_id: str | None = None,
    ) -> str:
        self.migrate()
        own_collection_run = collection_run_id is None
        if collection_run_id is None:
            collection_run_id = self.start_collection_run(scope=venue, venues=[venue])
        now = utc_now()
        with self.transaction() as conn:
            existing = conn.execute(
                """
                SELECT run_id FROM ingest_runs
                WHERE collection_run_id = ? AND source = ?
                ORDER BY started_at DESC LIMIT 1
                """,
                (collection_run_id, source),
            ).fetchone()
            if existing is not None:
                run_id = str(existing["run_id"])
                conn.execute(
                    """
                    UPDATE ingest_runs
                    SET completed_at = ?, status = 'FAILED', complete = 0,
                        error = ?
                    WHERE run_id = ?
                    """,
                    (now, error[:2000], run_id),
                )
            else:
                run_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO ingest_runs(
                        run_id, source, venue, market_type, product, started_at,
                        completed_at, status, complete, record_count, error,
                        collection_run_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'FAILED', 0, 0, ?, ?)
                    """,
                    (
                        run_id,
                        source,
                        venue,
                        market_type,
                        product,
                        now,
                        now,
                        error[:2000],
                        collection_run_id,
                    ),
                )
        if own_collection_run:
            self.finish_collection_run(collection_run_id)
        return run_id

    def apply_snapshot(self, snapshot: MarketSnapshot, *, collection_run_id: str | None = None) -> str:
        snapshot.validate()
        self.migrate()
        own_collection_run = collection_run_id is None
        if collection_run_id is None:
            collection_run_id = self.start_collection_run(scope=snapshot.venue, venues=[snapshot.venue])
        run_id = str(uuid.uuid4())
        seen_ids = {market.market_id for market in snapshot.markets}
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO venues(venue, display_name) VALUES (?, ?)",
                (snapshot.venue, snapshot.venue.title()),
            )
            conn.execute(
                """
                INSERT INTO ingest_runs(
                    run_id, source, venue, market_type, product, started_at,
                    status, complete, record_count, collection_run_id
                ) VALUES (?, ?, ?, ?, ?, ?, 'RUNNING', 0, 0, ?)
                """,
                (
                    run_id,
                    snapshot.source,
                    snapshot.venue,
                    snapshot.market_type,
                    snapshot.product,
                    snapshot.observed_at,
                    collection_run_id,
                ),
            )

            for market in snapshot.markets:
                raw_json = canonical_json(market.raw)
                content_hash = hashlib.sha256(raw_json.encode()).hexdigest()
                previous = conn.execute(
                    "SELECT status, active FROM markets WHERE market_id = ?",
                    (market.market_id,),
                ).fetchone()
                normalized = normalize_venue_asset_symbol(
                    market.base_symbol,
                    venue=market.venue,
                    market_type=market.market_type,
                )
                normalized_contract_type = normalize_contract_type(
                    market.contract_type,
                    market_type=market.market_type,
                )
                normalized_product = normalize_product(
                    market.market_type,
                    normalized_contract_type,
                )
                venue_product = market.venue_product or market.product
                venue_status = market.venue_status or market.status
                normalized_status = normalize_status(market.status)
                normalized_direction = market.contract_direction or contract_direction(
                    market_type=market.market_type,
                    base_symbol=market.base_symbol,
                    quote_symbol=market.quote_symbol,
                    settle_symbol=market.settle_symbol,
                )
                expiry_cycle = market.expiry_cycle or legacy_expiry_cycle(market.contract_type)
                conn.execute(
                    """
                    INSERT INTO markets(
                        market_id, source, venue, market_type, product, raw_symbol,
                        base_symbol, quote_symbol, settle_symbol, contract_type,
                        status, active, contract_multiplier, expires_at, max_market_order_size,
                        underlying_multiplier, venue_product, venue_status,
                        contract_direction, expiry_cycle,
                        first_seen_at, last_seen_at, raw_json, content_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(market_id) DO UPDATE SET
                        product=excluded.product,
                        base_symbol=excluded.base_symbol,
                        quote_symbol=excluded.quote_symbol,
                        settle_symbol=excluded.settle_symbol,
                        contract_type=excluded.contract_type,
                        status=excluded.status,
                        active=excluded.active,
                        contract_multiplier=excluded.contract_multiplier,
                        expires_at=excluded.expires_at,
                        max_market_order_size=excluded.max_market_order_size,
                        underlying_multiplier=excluded.underlying_multiplier,
                        venue_product=excluded.venue_product,
                        venue_status=excluded.venue_status,
                        contract_direction=excluded.contract_direction,
                        expiry_cycle=excluded.expiry_cycle,
                        last_seen_at=excluded.last_seen_at,
                        raw_json=excluded.raw_json,
                        content_hash=excluded.content_hash
                    """,
                    (
                        market.market_id,
                        market.source,
                        market.venue,
                        market.market_type,
                        normalized_product,
                        market.raw_symbol,
                        market.base_symbol,
                        market.quote_symbol,
                        market.settle_symbol,
                        normalized_contract_type,
                        normalized_status,
                        int(market.active),
                        market.contract_multiplier,
                        market.expires_at,
                        market.max_market_order_size,
                        str(normalized.multiplier),
                        venue_product,
                        venue_status,
                        normalized_direction,
                        expiry_cycle,
                        snapshot.observed_at,
                        snapshot.observed_at,
                        raw_json,
                        content_hash,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO market_observations(
                        run_id, market_id, observed_at, status, active,
                        content_hash, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        market.market_id,
                        snapshot.observed_at,
                        normalized_status,
                        int(market.active),
                        content_hash,
                        raw_json,
                    ),
                )
                if previous is None:
                    self._insert_event(conn, run_id, market.market_id, "DISCOVERED", None, normalized_status, snapshot.observed_at)
                else:
                    if bool(previous["active"]) != market.active:
                        event_type = "ACTIVATED" if market.active else "DEACTIVATED"
                        self._insert_event(
                            conn,
                            run_id,
                            market.market_id,
                            event_type,
                            str(bool(previous["active"])),
                            str(market.active),
                            snapshot.observed_at,
                        )
                    if previous["status"] != normalized_status:
                        self._insert_event(
                            conn,
                            run_id,
                            market.market_id,
                            "STATUS_CHANGED",
                            previous["status"],
                            normalized_status,
                            snapshot.observed_at,
                        )

            existing = conn.execute(
                "SELECT market_id, status FROM markets WHERE source = ? AND active = 1",
                (snapshot.source,),
            ).fetchall()
            for row in existing:
                if row["market_id"] in seen_ids:
                    continue
                conn.execute(
                    "UPDATE markets SET active = 0, status = 'MISSING' WHERE market_id = ?",
                    (row["market_id"],),
                )
                self._insert_event(
                    conn,
                    run_id,
                    row["market_id"],
                    "MISSING",
                    row["status"],
                    "MISSING",
                    snapshot.observed_at,
                )

            conn.execute(
                """
                UPDATE ingest_runs
                SET completed_at = ?, status = 'SUCCEEDED', complete = 1,
                    record_count = ?
                WHERE run_id = ?
                """,
                (utc_now(), len(snapshot.markets), run_id),
            )

        self.rebuild_symbol_matches()
        self.rebuild_asset_tags(run_id=run_id, observed_at=snapshot.observed_at)
        if own_collection_run:
            self.finish_collection_run(collection_run_id)
        return run_id

    @staticmethod
    def _insert_event(conn, run_id, market_id, event_type, old_value, new_value, observed_at) -> None:
        conn.execute(
            """
            INSERT INTO market_lifecycle_events(
                event_id, market_id, run_id, event_type, old_value,
                new_value, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), market_id, run_id, event_type, old_value, new_value, observed_at),
        )

    def rebuild_symbol_matches(self) -> None:
        self.migrate()
        now = utc_now()
        with self.transaction() as conn:
            markets = [dict(row) for row in conn.execute(
                """
                SELECT market_id, venue, market_type, base_symbol, active, raw_json
                FROM markets
                """
            )]
            metadata_by_market = {}
            for market in markets:
                try:
                    raw = json.loads(market["raw_json"] or "{}")
                except (TypeError, ValueError):
                    raw = {}
                metadata_by_market[market["market_id"]] = market_metadata(market, raw)
            exact_symbols = {
                normalize_asset_symbol(market["base_symbol"], allow_unit_prefix=False).symbol
                for market in markets
            }
            active_symbols_by_venue: dict[str, set[str]] = defaultdict(set)
            classified_symbols_by_venue: dict[str, set[str]] = defaultdict(set)
            for market in markets:
                if market["market_type"] != "FUTURE" or not market["active"]:
                    continue
                symbol = normalize_asset_symbol(
                    market["base_symbol"], allow_unit_prefix=False
                ).symbol
                active_symbols_by_venue[market["venue"]].add(symbol)
                if "EQUITY" in metadata_by_market[market["market_id"]].classifications:
                    classified_symbols_by_venue[market["venue"]].add(symbol)
            prepared = []
            for market in markets:
                raw_symbol = normalize_asset_symbol(market["base_symbol"], allow_unit_prefix=False)
                unit_candidate = normalize_asset_symbol(market["base_symbol"], allow_unit_prefix=True)
                canonical_symbol = raw_symbol.symbol
                identity_method = "EXACT_SYMBOL"
                multiplier = 1
                mapping_evidence = {
                    "raw_base_symbol": market["base_symbol"],
                    "canonical_symbol": canonical_symbol,
                }
                candidates = []

                if unit_candidate.method == "UNIT_PREFIX_SYMBOL":
                    counterpart_exists = unit_candidate.symbol in exact_symbols
                    unit_score = 0.95 if counterpart_exists else 0.50
                    unit_evidence = {
                        "raw_base_symbol": market["base_symbol"],
                        "proposed_symbol": unit_candidate.symbol,
                        "unit_multiplier": unit_candidate.multiplier,
                        "counterpart_symbol_exists": counterpart_exists,
                    }
                    candidates.append(
                        {
                            "proposed_symbol": unit_candidate.symbol,
                            "rule": "UNIT_PREFIX_COUNTERPART",
                            "decision": "ACCEPTED" if counterpart_exists else "PROPOSED",
                            "score": unit_score,
                            "evidence": unit_evidence,
                        }
                    )
                    if counterpart_exists:
                        canonical_symbol = unit_candidate.symbol
                        identity_method = "UNIT_PREFIX_COUNTERPART"
                        multiplier = unit_candidate.multiplier
                        mapping_evidence = unit_evidence

                for hint in metadata_by_market[market["market_id"]].alias_hints:
                    alias_candidate = evaluate_alias_hint(
                        hint=hint,
                        active_symbols_by_venue=active_symbols_by_venue,
                        classified_symbols_by_venue=classified_symbols_by_venue,
                        required_classification="EQUITY",
                    )
                    candidates.append(
                        {
                            "proposed_symbol": alias_candidate.proposed_symbol,
                            "rule": alias_candidate.rule,
                            "decision": alias_candidate.decision,
                            "score": alias_candidate.score,
                            "evidence": alias_candidate.evidence,
                        }
                    )
                    if alias_candidate.decision == "ACCEPTED":
                        canonical_symbol = alias_candidate.proposed_symbol
                        identity_method = alias_candidate.rule
                        multiplier = 1
                        mapping_evidence = alias_candidate.evidence

                for candidate in candidates:
                    candidate_id = str(uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"https://mdv.local/candidates/{market['market_id']}/{candidate['rule']}/{candidate['proposed_symbol']}",
                    ))
                    conn.execute(
                        """
                        INSERT INTO asset_match_candidates(
                            candidate_id, source_market_id,
                            proposed_canonical_symbol, rule, decision, score,
                            evidence_json, matcher_version, first_evaluated_at,
                            last_evaluated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source_market_id, proposed_canonical_symbol, rule)
                        DO UPDATE SET
                            decision=excluded.decision,
                            score=excluded.score,
                            evidence_json=excluded.evidence_json,
                            matcher_version=excluded.matcher_version,
                            last_evaluated_at=excluded.last_evaluated_at
                        """,
                        (
                            candidate_id,
                            market["market_id"],
                            candidate["proposed_symbol"],
                            candidate["rule"],
                            candidate["decision"],
                            candidate["score"],
                            canonical_json(candidate["evidence"]),
                            MATCHER_VERSION,
                            now,
                            now,
                        ),
                    )

                conn.execute(
                    "UPDATE markets SET underlying_multiplier = ? WHERE market_id = ?",
                    (str(multiplier), market["market_id"]),
                )
                prepared.append(
                    {
                        **market,
                        "normalized_symbol": canonical_symbol,
                        "normalizer_method": identity_method,
                        "mapping_evidence_json": canonical_json(mapping_evidence),
                        "evidence_score": max(
                            [candidate["score"] for candidate in candidates if candidate["decision"] == "ACCEPTED"],
                            default=0.0,
                        ),
                    }
                )
            scores = score_symbol_groups(prepared)
            for row in prepared:
                canonical_symbol = row["normalized_symbol"]
                asset_id = stable_asset_id(canonical_symbol)
                group_method, confidence = scores[canonical_symbol]
                method = group_method
                if row["normalizer_method"] != "EXACT_SYMBOL":
                    method = f"{row['normalizer_method']}+{group_method}"
                    confidence = min(max(confidence, row["evidence_score"]), 0.99)
                conn.execute(
                    """
                    INSERT INTO assets(asset_id, canonical_symbol, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(asset_id) DO UPDATE SET updated_at=excluded.updated_at
                    """,
                    (asset_id, canonical_symbol, now, now),
                )
                previous = conn.execute(
                    """
                    SELECT asset_id, normalized_symbol, method, confidence,
                           matcher_version, evidence_json
                    FROM market_asset_mappings WHERE market_id = ?
                    """,
                    (row["market_id"],),
                ).fetchone()
                current = (
                    asset_id,
                    canonical_symbol,
                    method,
                    confidence,
                    MATCHER_VERSION,
                    row["mapping_evidence_json"],
                )
                if previous is None or tuple(previous) != current:
                    conn.execute(
                        """
                        INSERT INTO market_asset_mapping_revisions(
                            revision_id, market_id, asset_id, venue_symbol,
                            normalized_symbol, method, confidence, matcher_version,
                            recorded_at, evidence_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            row["market_id"],
                            asset_id,
                            row["base_symbol"],
                            canonical_symbol,
                            method,
                            confidence,
                            MATCHER_VERSION,
                            now,
                            row["mapping_evidence_json"],
                        ),
                    )
                conn.execute(
                    """
                    INSERT INTO market_asset_mappings(
                        market_id, asset_id, venue_symbol, normalized_symbol,
                        method, confidence, matcher_version, updated_at
                        , evidence_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(market_id) DO UPDATE SET
                        asset_id=excluded.asset_id,
                        venue_symbol=excluded.venue_symbol,
                        normalized_symbol=excluded.normalized_symbol,
                        method=excluded.method,
                        confidence=excluded.confidence,
                        matcher_version=excluded.matcher_version,
                        updated_at=excluded.updated_at,
                        evidence_json=excluded.evidence_json
                    """,
                    (
                        row["market_id"],
                        asset_id,
                        row["base_symbol"],
                        canonical_symbol,
                        method,
                        confidence,
                        MATCHER_VERSION,
                        now,
                        row["mapping_evidence_json"],
                    ),
                )

    def rebuild_asset_tags(self, *, run_id: str, observed_at: str) -> None:
        """Project provider metadata onto canonical assets and version changes."""
        self.migrate()
        with self.transaction() as conn:
            desired: dict[tuple[str, str, str], dict] = {}
            rows = conn.execute(
                """
                SELECT m.market_id, m.venue, m.market_type, m.base_symbol,
                       m.raw_json, map.asset_id
                FROM markets m
                JOIN market_asset_mappings map ON map.market_id = m.market_id
                WHERE m.active = 1
                """
            )
            for row in rows:
                try:
                    raw = json.loads(row["raw_json"] or "{}")
                except (TypeError, ValueError):
                    continue
                tag_rows = market_metadata(dict(row), raw).tags
                for tag_row in tag_rows:
                    provider = str(tag_row.get("provider") or "").strip().upper()
                    raw_tag = tag_row.get("raw_tag", tag_row.get("tag"))
                    raw_name = str(raw_tag).strip()
                    tag = str(tag_row.get("tag") or raw_name).strip().upper().replace(" ", "_")
                    if not provider or not raw_name or not tag:
                        continue
                    key = (row["asset_id"], provider, tag)
                    value = desired.setdefault(
                        key,
                        {
                            "raw_tag": raw_name,
                            "markets": [],
                            "product_symbols": [],
                            "sources": [],
                        },
                    )
                    value["markets"].append(row["market_id"])
                    value["sources"].append(str(tag_row.get("source") or "PROVIDER_METADATA"))
                    product_symbol = str(tag_row.get("product_symbol") or "").upper()
                    if product_symbol:
                        value["product_symbols"].append(product_symbol)

            existing = {
                (row["asset_id"], row["provider"], row["tag"]): dict(row)
                for row in conn.execute("SELECT * FROM asset_tags WHERE active = 1")
            }
            for key, value in desired.items():
                asset_id, provider, tag = key
                evidence = canonical_json(
                    {
                        "sources": sorted(set(value["sources"])),
                        "market_ids": sorted(set(value["markets"])),
                        "product_symbols": sorted(set(value["product_symbols"])),
                    }
                )
                previous = existing.get(key)
                conn.execute(
                    """
                    INSERT INTO asset_tags(
                        asset_id, provider, tag, raw_tag, active, evidence_json,
                        first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                    ON CONFLICT(asset_id, provider, tag) DO UPDATE SET
                        raw_tag=excluded.raw_tag,
                        active=1,
                        evidence_json=excluded.evidence_json,
                        last_seen_at=excluded.last_seen_at
                    """,
                    (asset_id, provider, tag, value["raw_tag"], evidence, observed_at, observed_at),
                )
                if previous is None:
                    conn.execute(
                        """
                        INSERT INTO asset_tag_events(
                            event_id, asset_id, run_id, provider, tag, raw_tag,
                            event_type, observed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'ADDED', ?)
                        """,
                        (str(uuid.uuid4()), asset_id, run_id, provider, tag, value["raw_tag"], observed_at),
                    )

            for key, previous in existing.items():
                if key in desired:
                    continue
                conn.execute(
                    """
                    UPDATE asset_tags SET active = 0, last_seen_at = ?
                    WHERE asset_id = ? AND provider = ? AND tag = ?
                    """,
                    (observed_at, *key),
                )
                conn.execute(
                    """
                    INSERT INTO asset_tag_events(
                        event_id, asset_id, run_id, provider, tag, raw_tag,
                        event_type, observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'REMOVED', ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        key[0],
                        run_id,
                        key[1],
                        key[2],
                        previous["raw_tag"],
                        observed_at,
                    ),
                )

    def list_markets(self, filters: dict[str, object]) -> list[dict]:
        self.migrate()
        clauses = []
        params: list[object] = []

        def add_set_filter(column: str, key: str) -> None:
            included = requested_values(filters, key)
            excluded = requested_values(filters, f"{key}_not")
            if included:
                placeholders = ",".join("?" for _ in included)
                clauses.append(f"{column} IN ({placeholders})")
                params.extend(sorted(included))
            if excluded:
                placeholders = ",".join("?" for _ in excluded)
                clauses.append(f"({column} IS NULL OR {column} NOT IN ({placeholders}))")
                params.extend(sorted(excluded))

        for column, key in (
            ("m.market_type", "type"),
            ("m.venue", "venue"),
            ("m.product", "product"),
            ("m.expiry_cycle", "expiry"),
            ("m.contract_direction", "direction"),
            ("m.quote_symbol", "quote"),
            ("m.settle_symbol", "settle"),
            ("m.status", "status"),
        ):
            add_set_filter(column, key)

        for suffix, excluded_filter in (("", False), ("_not", True)):
            contracts = requested_contract_values(filters, suffix)
            if contracts:
                expressions = []
                direct = contracts & {"PERP", "DATED"}
                if direct:
                    placeholders = ",".join("?" for _ in direct)
                    expressions.append(f"m.contract_type IN ({placeholders})")
                    params.extend(sorted(direct))
                for alias, expiry in (("CQ", "Q"), ("NQ", "BQ")):
                    if alias in contracts:
                        expressions.append("(m.contract_type = 'DATED' AND m.expiry_cycle = ?)")
                        params.append(expiry)
                expression = "(" + " OR ".join(expressions) + ")"
                clauses.append(f"NOT {expression}" if excluded_filter else expression)

        active_included = requested_values(filters, "active")
        active_excluded = requested_values(filters, "active_not")
        if not active_included and not active_excluded:
            clauses.append("m.active = 1")
        else:
            def active_values(values: set[str]) -> set[int]:
                invalid = values - {"0", "1", "TRUE", "FALSE"}
                if invalid:
                    raise ValueError("ACTIVE must be true, false, 1, or 0")
                return {1 if value in {"1", "TRUE"} else 0 for value in values}

            for values, operator in (
                (active_values(active_included), "IN"),
                (active_values(active_excluded), "NOT IN"),
            ):
                if values:
                    placeholders = ",".join("?" for _ in values)
                    clauses.append(f"m.active {operator} ({placeholders})")
                    params.extend(sorted(values))

        def add_symbol_filter(key: str, *, excluded: bool = False) -> None:
            for symbol in sorted(requested_values(filters, key)):
                escaped = symbol.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("*", "%")
                expression = (
                    "(UPPER(m.raw_symbol) LIKE ? ESCAPE '\\' OR "
                    "UPPER(m.base_symbol) LIKE ? ESCAPE '\\' OR "
                    "UPPER(a.canonical_symbol) LIKE ? ESCAPE '\\')"
                )
                clauses.append(f"NOT {expression}" if excluded else expression)
                params.extend([escaped, escaped, escaped])

        add_symbol_filter("symbol")
        add_symbol_filter("symbol_not", excluded=True)
        for provider, tag in sorted(requested_tag_keys(filters.get("tags") or [])):
            clauses.append(
                "EXISTS (SELECT 1 FROM asset_tags at "
                "WHERE at.asset_id = a.asset_id AND at.provider = ? AND at.tag = ? AND at.active = 1)"
            )
            params.extend([provider, tag])
        for provider, tag in sorted(requested_tag_keys(filters.get("tags_not") or [])):
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM asset_tags at "
                "WHERE at.asset_id = a.asset_id AND at.provider = ? AND at.tag = ? AND at.active = 1)"
            )
            params.extend([provider, tag])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = min(max(int(filters.get("limit") or 5000), 1), 5000)
        offset = max(int(filters.get("offset") or 0), 0)
        params.extend([limit, offset])
        with self.readonly() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    m.market_id, m.venue, m.market_type, m.product,
                    m.raw_symbol, m.base_symbol, m.quote_symbol,
                    m.settle_symbol, m.contract_type, m.status, m.active,
                    m.contract_multiplier, m.expires_at, m.max_market_order_size,
                    m.underlying_multiplier, m.venue_product, m.venue_status,
                    m.contract_direction, m.expiry_cycle,
                    m.first_seen_at, m.last_seen_at, m.raw_json,
                    a.asset_id, a.canonical_symbol,
                    map.method AS match_method,
                    map.confidence AS match_confidence,
                    map.matcher_version
                FROM markets m
                LEFT JOIN market_asset_mappings map ON map.market_id = m.market_id
                LEFT JOIN assets a ON a.asset_id = map.asset_id
                {where}
                ORDER BY m.venue, m.product, m.raw_symbol
                LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def resolve_venue_mappings(
        self,
        *,
        source: dict[str, object],
        target: dict[str, object],
        symbols: list[str],
    ) -> dict:
        """Resolve a symbol basket without building the generic asset projection."""
        requested_rows = ",".join("(?, ?)" for _ in symbols)
        source_params: list[object] = [
            item
            for position, symbol in enumerate(symbols)
            for item in (position, symbol)
        ]
        source_params.append(source["venue"])

        target_clauses = [
            "target_market.venue = ?",
            "target_market.active = 1",
            "target_market.market_type = ?",
            "target_market.product = ?",
            "target_market.contract_type = ?",
            "target_market.quote_symbol = ?",
            "target_market.settle_symbol = ?",
            "target_market.status = ?",
        ]
        target_params: list[object] = [
            target["venue"],
            target["market_type"],
            target["product"],
            target["contract_type"],
            target["quote_symbol"],
            target["settle_symbol"],
            target["status"],
        ]
        for field in ("venue_product", "contract_direction", "expiry_cycle"):
            value = target.get(field)
            if value is not None:
                target_clauses.append(f"target_market.{field} = ?")
                target_params.append(value)

        source_sql = f"""
            WITH requested(position, source_symbol) AS (VALUES {requested_rows})
            SELECT
                requested.position,
                requested.source_symbol,
                mapping.asset_id,
                asset.canonical_symbol,
                CASE
                    WHEN latest.status = 'SUCCEEDED'
                     AND source_market.last_seen_at >= latest.started_at
                    THEN 0 ELSE 1
                END AS is_stale
            FROM requested
            CROSS JOIN markets AS source_market INDEXED BY idx_markets_mapping_source
              ON source_market.venue = ?
             AND source_market.active = 1
             AND source_market.base_symbol = requested.source_symbol
            JOIN market_asset_mappings AS mapping
              ON mapping.market_id = source_market.market_id
            JOIN assets AS asset
              ON asset.asset_id = mapping.asset_id
            LEFT JOIN ingest_runs AS latest
              ON latest.run_id = (
                  SELECT run_id
                  FROM ingest_runs
                  WHERE source = source_market.source
                  ORDER BY started_at DESC, run_id DESC
                  LIMIT 1
              )
            ORDER BY requested.position, mapping.asset_id
        """

        database_uri = self.path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(database_uri, uri=True, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("BEGIN")
            revision_row = conn.execute(
                """
                SELECT COALESCE(last_seen_at, '') AS revision
                FROM markets
                WHERE active = 1
                ORDER BY last_seen_at DESC
                LIMIT 1
                """
            ).fetchone()
            source_rows = conn.execute(source_sql, source_params).fetchall()
            asset_ids = sorted({row["asset_id"] for row in source_rows})
            target_rows: list[sqlite3.Row] = []
            if asset_ids:
                asset_placeholders = ",".join("?" for _ in asset_ids)
                target_rows = conn.execute(
                    f"""
                    SELECT
                        target_mapping.asset_id,
                        target_market.market_id,
                        target_market.raw_symbol,
                        target_market.base_symbol,
                        target_market.last_seen_at,
                        CASE
                            WHEN latest.status = 'SUCCEEDED'
                             AND target_market.last_seen_at >= latest.started_at
                            THEN 0 ELSE 1
                        END AS is_stale
                    FROM market_asset_mappings AS target_mapping
                    JOIN markets AS target_market
                      ON target_market.market_id = target_mapping.market_id
                    LEFT JOIN ingest_runs AS latest
                      ON latest.run_id = (
                          SELECT run_id
                          FROM ingest_runs
                          WHERE source = target_market.source
                          ORDER BY started_at DESC, run_id DESC
                          LIMIT 1
                      )
                    WHERE target_mapping.asset_id IN ({asset_placeholders})
                      AND {' AND '.join(target_clauses)}
                    ORDER BY target_mapping.asset_id, target_market.market_id
                    """,
                    [*asset_ids, *target_params],
                ).fetchall()
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

        grouped = {
            position: {"source_symbol": symbol, "assets": {}}
            for position, symbol in enumerate(symbols)
        }
        for row in source_rows:
            asset = grouped[row["position"]]["assets"].setdefault(
                row["asset_id"],
                {
                    "asset_id": row["asset_id"],
                    "canonical_symbol": row["canonical_symbol"],
                    "is_stale": True,
                    "targets": {},
                },
            )
            asset["is_stale"] = asset["is_stale"] and bool(row["is_stale"])

        targets_by_asset: dict[str, dict[str, dict[str, object]]] = {}
        for row in target_rows:
            targets_by_asset.setdefault(row["asset_id"], {})[row["market_id"]] = {
                "market_id": row["market_id"],
                "raw_symbol": row["raw_symbol"],
                "base_symbol": row["base_symbol"],
                "last_seen_at": row["last_seen_at"],
                "is_stale": bool(row["is_stale"]),
            }
        for item in grouped.values():
            for asset in item["assets"].values():
                asset["targets"] = targets_by_asset.get(asset["asset_id"], {})

        results = []
        for position in range(len(symbols)):
            item = grouped[position]
            assets = list(item["assets"].values())
            if not assets:
                results.append(
                    {
                        "source_symbol": item["source_symbol"],
                        "status": "source_not_found",
                        "error_code": "SOURCE_NOT_FOUND",
                    }
                )
                continue
            if len(assets) != 1:
                results.append(
                    {
                        "source_symbol": item["source_symbol"],
                        "status": "ambiguous_source",
                        "error_code": "MULTIPLE_SOURCE_ASSETS",
                    }
                )
                continue

            asset = assets[0]
            result = {
                "source_symbol": item["source_symbol"],
                "asset_id": asset["asset_id"],
                "canonical_symbol": asset["canonical_symbol"],
            }
            targets = list(asset["targets"].values())
            if not targets:
                result.update(status="target_not_found", error_code="TARGET_NOT_FOUND")
            elif len(targets) != 1:
                result.update(status="ambiguous_target", error_code="MULTIPLE_TARGETS")
            else:
                selected = targets[0]
                result["target"] = {
                    key: selected[key]
                    for key in ("market_id", "raw_symbol", "base_symbol", "last_seen_at")
                }
                if asset["is_stale"] or selected["is_stale"]:
                    result.update(status="stale", error_code="STALE_SNAPSHOT")
                else:
                    result["status"] = "resolved"
            results.append(result)

        return {
            "schema_version": "1",
            "snapshot_revision": str(
                revision_row["revision"] if revision_row is not None else ""
            ).replace("+00:00", "Z"),
            "results": results,
        }

    def list_assets(self, filters: dict[str, object]) -> dict:
        """Build the active asset -> venue symbol -> market projection."""
        self.migrate()
        with self.readonly() as conn:
            rows = [dict(row) for row in conn.execute(
                """
                SELECT
                    m.market_id, m.venue, m.market_type, m.product,
                    m.raw_symbol, m.base_symbol, m.quote_symbol,
                    m.settle_symbol, m.contract_type, m.status,
                    m.contract_multiplier, m.expires_at, m.max_market_order_size,
                    m.underlying_multiplier, m.venue_product, m.venue_status,
                    m.contract_direction, m.expiry_cycle,
                    m.first_seen_at, m.last_seen_at, m.raw_json,
                    a.asset_id, a.canonical_symbol
                FROM markets m
                JOIN market_asset_mappings map ON map.market_id = m.market_id
                JOIN assets a ON a.asset_id = map.asset_id
                WHERE m.active = 1
                ORDER BY a.canonical_symbol, m.venue, m.market_type,
                         m.product, m.raw_symbol
                """
            )]
            supported_future_venues = [row[0] for row in conn.execute(
                """
                SELECT DISTINCT venue
                FROM ingest_runs
                WHERE market_type = 'FUTURE' AND status = 'SUCCEEDED'
                ORDER BY venue
                """
            )]
            if not supported_future_venues:
                supported_future_venues = [row[0] for row in conn.execute(
                    """
                    SELECT DISTINCT venue FROM markets
                    WHERE market_type = 'FUTURE'
                    ORDER BY venue
                    """
                )]
            tag_rows = [dict(row) for row in conn.execute(
                """
                SELECT asset_id, provider, tag, raw_tag
                FROM asset_tags
                WHERE active = 1
                ORDER BY provider, tag
                """
            )]

        grouped: dict[str, dict] = {}
        for row in rows:
            asset = grouped.setdefault(
                row["asset_id"],
                {
                    "asset_id": row["asset_id"],
                    "canonical_symbol": row["canonical_symbol"],
                    "markets": [],
                },
            )
            asset["markets"].append(row)

        included = {
            name: requested_values(filters, name)
            for name in (
                "type", "product", "expiry", "direction", "venue",
                "quote", "settle", "status",
            )
        }
        excluded = {
            name: requested_values(filters, f"{name}_not")
            for name in included
        }
        contracts = requested_contract_values(filters)
        contracts_not = requested_contract_values(filters, "_not")

        validations = (
            ("TYPE", included["type"] | excluded["type"], {"SPOT", "FUTURE"}),
            ("PRODUCT", included["product"] | excluded["product"], set(PRODUCT_VALUES)),
            ("CONTRACT", contracts | contracts_not, {"PERP", "DATED", "CQ", "NQ"}),
            ("EXPIRY", included["expiry"] | excluded["expiry"], set(EXPIRY_CYCLE_VALUES)),
            ("DIRECTION", included["direction"] | excluded["direction"], set(CONTRACT_DIRECTION_VALUES)),
            ("STATUS", included["status"] | excluded["status"], set(STATUS_VALUES)),
        )
        for name, values, allowed in validations:
            invalid = values - allowed
            if invalid:
                raise ValueError(f"{name} has unknown value(s): {', '.join(sorted(invalid))}")

        required_future_venues = requested_values(filters, "futures")
        excluded_future_venues = requested_values(filters, "futures_not")
        overlap = required_future_venues & excluded_future_venues
        if overlap:
            raise ValueError(f"FUTURES requires and excludes the same venue: {', '.join(sorted(overlap))}")
        requested_tags = requested_tag_keys(filters.get("tags") or [])
        excluded_tags = requested_tag_keys(filters.get("tags_not") or [])

        def stock_values(name: str) -> set[bool]:
            values = requested_values(filters, name)
            aliases = {
                "1": True, "TRUE": True, "YES": True, "STOCK": True,
                "0": False, "FALSE": False, "NO": False,
                "NONSTOCK": False, "NON-STOCK": False,
            }
            invalid = values - set(aliases)
            if invalid:
                raise ValueError("STOCK must be 1 or 0")
            return {aliases[value] for value in values}

        included_stock = stock_values("stock")
        excluded_stock = stock_values("stock_not")
        limit = min(max(int(filters.get("limit") or 1000), 1), 5000)
        offset = max(int(filters.get("offset") or 0), 0)

        assets = []
        tags_by_asset: dict[str, list[dict]] = {}
        for tag_row in tag_rows:
            tags_by_asset.setdefault(tag_row["asset_id"], []).append(
                {
                    "provider": tag_row["provider"],
                    "tag": tag_row["tag"],
                    "raw_tag": tag_row["raw_tag"],
                    "key": f"{tag_row['provider']}:{tag_row['tag']}",
                }
            )
        for asset in grouped.values():
            all_markets = asset["markets"]
            asset_tags = tags_by_asset.get(asset["asset_id"], [])
            asset_tag_keys = {(item["provider"], item["tag"]) for item in asset_tags}
            if not requested_tags.issubset(asset_tag_keys):
                continue
            if excluded_tags & asset_tag_keys:
                continue
            for row in all_markets:
                try:
                    raw_payload = json.loads(row["raw_json"] or "{}")
                except (TypeError, ValueError):
                    raw_payload = {}
                row["is_stock"] = "EQUITY" in market_metadata(
                    row, raw_payload
                ).classifications
                row.pop("raw_json", None)
            is_stock = any(row["is_stock"] for row in all_markets)
            if included_stock and is_stock not in included_stock:
                continue
            if is_stock in excluded_stock:
                continue
            spot_markets = [row for row in all_markets if row["market_type"] == "SPOT"]

            column_by_filter = {
                "type": "market_type",
                "product": "product",
                "expiry": "expiry_cycle",
                "direction": "contract_direction",
                "venue": "venue",
                "quote": "quote_symbol",
                "settle": "settle_symbol",
                "status": "status",
            }

            def matches_contract(row: dict, values: set[str]) -> bool:
                return any(
                    (value == "PERP" and row["contract_type"] == "PERP")
                    or (value == "DATED" and row["contract_type"] == "DATED")
                    or (value == "CQ" and row["contract_type"] == "DATED" and row["expiry_cycle"] == "Q")
                    or (value == "NQ" and row["contract_type"] == "DATED" and row["expiry_cycle"] == "BQ")
                    for value in values
                )

            def matches_included_dimensions(row: dict) -> bool:
                if contracts and not matches_contract(row, contracts):
                    return False
                return all(
                    not values or str(row[column] or "").upper() in values
                    for name, values in included.items()
                    for column in [column_by_filter[name]]
                )

            if contracts_not and any(matches_contract(row, contracts_not) for row in all_markets):
                continue
            if any(
                values and any(str(row[column_by_filter[name]] or "").upper() in values for row in all_markets)
                for name, values in excluded.items()
            ):
                continue

            matching_markets = [row for row in all_markets if matches_included_dimensions(row)]
            if (contracts or any(included.values())) and not matching_markets:
                continue
            matching_futures = [row for row in matching_markets if row["market_type"] == "FUTURE"]
            if not (contracts or any(included.values())):
                matching_futures = [row for row in all_markets if row["market_type"] == "FUTURE"]
            matching_future_venues = {row["venue"] for row in matching_futures}

            if (contracts or required_future_venues or excluded_future_venues) and not matching_futures:
                continue
            if not required_future_venues.issubset(matching_future_venues):
                continue
            if excluded_future_venues & matching_future_venues:
                continue

            markets = all_markets
            candidates = [asset["canonical_symbol"]]
            candidates.extend(row["base_symbol"] for row in markets)
            candidates.extend(row["raw_symbol"] for row in markets)
            symbol_patterns = requested_values(filters, "symbol")
            excluded_symbol_patterns = requested_values(filters, "symbol_not")
            if symbol_patterns and not any(
                fnmatchcase(str(candidate).upper(), pattern)
                for candidate in candidates for pattern in symbol_patterns
            ):
                continue
            if any(
                fnmatchcase(str(candidate).upper(), pattern)
                for candidate in candidates for pattern in excluded_symbol_patterns
            ):
                continue

            venues: dict[str, dict] = {}
            for market in markets:
                venue = venues.setdefault(
                    market["venue"],
                    {"venue": market["venue"], "symbols": set(), "spot": [], "futures": []},
                )
                venue["symbols"].add(market["base_symbol"])
                market["underlying_unit"] = asset["canonical_symbol"]
                if market["underlying_multiplier"] != "1":
                    market["underlying_unit"] = f"{market['underlying_multiplier']} {asset['canonical_symbol']}"
                market["trade_url"] = market_trade_url(market)
                target = venue["spot"] if market["market_type"] == "SPOT" else venue["futures"]
                target.append(market)

            venue_rows = []
            for venue in sorted(venues.values(), key=lambda item: item["venue"]):
                venue["symbols"] = sorted(venue["symbols"])
                venue["spot"].sort(key=lambda item: (item["product"], item["raw_symbol"]))
                venue["futures"].sort(key=lambda item: (item["product"], item["raw_symbol"]))
                venue_rows.append(venue)

            spot_venues = [
                {"venue": venue["venue"], "count": len(venue["spot"])}
                for venue in venue_rows if venue["spot"]
            ]
            future_venues = [
                {
                    "venue": venue["venue"],
                    "count": len(venue["futures"]),
                    "products": sorted({market["product"] for market in venue["futures"]}),
                }
                for venue in venue_rows if venue["futures"]
            ]
            future_venue_names = {row["venue"] for row in future_venues}
            covered = len(future_venue_names)
            possible = len(supported_future_venues)
            if covered == 0:
                coverage_label = "NO FUTURES"
                coverage_kind = "none"
            elif possible == 2 and covered == 2:
                coverage_label = "BOTH · 2/2"
                coverage_kind = "all"
            elif covered == 1:
                only_venue = next(iter(future_venue_names))
                coverage_label = f"{only_venue} ONLY · 1/{possible or 1}"
                coverage_kind = "single"
            elif possible and covered == possible:
                coverage_label = f"ALL · {covered}/{possible}"
                coverage_kind = "all"
            else:
                coverage_label = f"{covered}/{possible or covered} VENUES"
                coverage_kind = "partial"

            asset.update(
                {
                    "markets": markets,
                    "venues": venue_rows,
                    "venue_symbols": [
                        {"venue": venue["venue"], "symbols": venue["symbols"]}
                        for venue in venue_rows
                    ],
                    "spot_venues": spot_venues,
                    "future_venues": future_venues,
                    "future_coverage": coverage_label,
                    "future_coverage_kind": coverage_kind,
                    "active_market_count": len(markets),
                    "is_stock": is_stock,
                    "tags": asset_tags,
                }
            )
            assets.append(asset)

        assets.sort(key=lambda item: item["canonical_symbol"])
        total = len(assets)
        return {
            "assets": assets[offset : offset + limit],
            "count": total,
            "supported_future_venues": supported_future_venues,
        }

    def filter_metadata(self) -> dict:
        """Describe public query filters and values available in stored data."""
        self.migrate()
        with self.readonly() as conn:
            def active_values(column: str, extra_clause: str = "") -> list[str]:
                rows = conn.execute(
                    f"""
                    SELECT DISTINCT {column}
                    FROM markets
                    WHERE active = 1 AND {column} IS NOT NULL AND {column} != ''
                    {extra_clause}
                    ORDER BY {column}
                    """
                )
                return [str(row[0]) for row in rows]

            future_venues = [str(row[0]) for row in conn.execute(
                """
                SELECT DISTINCT venue
                FROM ingest_runs
                WHERE market_type = 'FUTURE' AND status = 'SUCCEEDED'
                ORDER BY venue
                """
            )]
            if not future_venues:
                future_venues = active_values("venue", "AND market_type = 'FUTURE'")

            tags = [str(row[0]) for row in conn.execute(
                """
                SELECT DISTINCT provider || ':' || tag
                FROM asset_tags
                WHERE active = 1
                ORDER BY provider || ':' || tag
                """
            )]

            def enum(values: list[str], description: str, **extra) -> dict:
                return {
                    "kind": "enum",
                    "values": values,
                    "operators": ["=", "!="],
                    "multiple": True,
                    "description": description,
                    **extra,
                }

            filters = {
                "TYPE": enum(
                    active_values("market_type"),
                    "Market family: spot or futures.",
                ),
                "PRODUCT": enum(
                    active_values("product"),
                    "Normalized instrument product. Currency and payoff direction are separate fields.",
                    value_descriptions={
                        "SPOT": "Immediate-delivery spot pair.",
                        "PERP": "Perpetual futures contract with no scheduled expiry.",
                        "DATED": "Futures contract with a scheduled expiry.",
                    },
                ),
                "CONTRACT": enum(
                    active_values("contract_type", "AND market_type = 'FUTURE'"),
                    "Backward-compatible alias for futures PRODUCT; CQ/NQ inputs map to DATED plus Q/BQ expiry.",
                    deprecated_alias_for="PRODUCT",
                ),
                "EXPIRY": enum(
                    active_values("expiry_cycle", "AND market_type = 'FUTURE'"),
                    "Venue-published dated-contract listing cycle.",
                    value_descriptions={
                        "W": "Weekly", "BW": "Bi-weekly", "TW": "Tri-weekly",
                        "M": "Monthly", "BM": "Bi-monthly", "Q": "Quarterly",
                        "BQ": "Bi-quarterly", "TQ": "Tri-quarterly",
                    },
                ),
                "DIRECTION": enum(
                    active_values("contract_direction", "AND market_type = 'FUTURE'"),
                    "Futures payoff/settlement direction.",
                    value_descriptions={
                        "LINEAR": "Settles in the quote asset.",
                        "INVERSE": "Settles in the base asset.",
                        "QUANTO": "Settles in an asset other than base or quote.",
                    },
                ),
                "QUOTE": enum(active_values("quote_symbol"), "Price-denomination asset."),
                "SETTLE": enum(
                    active_values("settle_symbol", "AND market_type = 'FUTURE'"),
                    "Futures settlement or margin asset.",
                ),
                "FUTURES": enum(
                    future_venues,
                    "Futures venue coverage required or excluded at asset level.",
                ),
                "STOCK": enum(["0", "1"], "Canonical asset stock classification."),
                "TAG": enum(tags, "Provider-scoped canonical asset tag."),
                "VENUE": enum(active_values("venue"), "Trading venue."),
                "SYMBOL": {
                    "kind": "text", "wildcard": "*", "operators": ["=", "!="],
                    "multiple": True, "description": "Canonical, venue-base, or raw market symbol.",
                },
                "STATUS": enum(
                    active_values("status"),
                    "Normalized operational market status; venue_status retains the source label.",
                ),
                "ACTIVE": enum(["true", "false"], "Whether the market is in the current active view."),
                "LIMIT": {"kind": "integer", "default": 5000, "minimum": 1, "maximum": 5000},
                "OFFSET": {"kind": "integer", "default": 0, "minimum": 0},
            }
        return {"filters": filters}

    def list_collection_runs(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        venue: str | None = None,
    ) -> dict:
        """Return collection invocations with universe outcomes and audit changes."""
        self.migrate()
        limit = min(max(int(limit), 1), 500)
        offset = max(int(offset), 0)
        requested_venue = str(venue or "").strip().upper()
        where = ""
        params: list[object] = []
        if requested_venue:
            where = (
                "WHERE EXISTS (SELECT 1 FROM ingest_runs ir "
                "WHERE ir.collection_run_id = cr.collection_run_id AND ir.venue = ?)"
            )
            params.append(requested_venue)
        with self.readonly() as conn:
            total = int(conn.execute(
                f"SELECT COUNT(*) FROM collection_runs cr {where}",
                params,
            ).fetchone()[0])
            run_rows = [dict(row) for row in conn.execute(
                f"""
                SELECT * FROM collection_runs cr
                {where}
                ORDER BY cr.started_at DESC, cr.collection_run_id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            )]
            run_ids = [row["collection_run_id"] for row in run_rows]
            if not run_ids:
                return {"count": total, "runs": []}
            placeholders = ",".join("?" for _ in run_ids)
            universe_rows = [dict(row) for row in conn.execute(
                f"""
                SELECT run_id, collection_run_id, source, venue, market_type,
                       product, started_at, completed_at, status, complete,
                       record_count, error
                FROM ingest_runs
                WHERE collection_run_id IN ({placeholders})
                ORDER BY venue, market_type, product, source
                """,
                run_ids,
            )]
            lifecycle_rows = [dict(row) for row in conn.execute(
                f"""
                SELECT ir.collection_run_id, ir.run_id, ir.venue, ir.source,
                       e.event_type, e.old_value, e.new_value, e.observed_at,
                       m.raw_symbol, m.base_symbol,
                       COALESCE(a.canonical_symbol, m.base_symbol) AS canonical_symbol
                FROM market_lifecycle_events e
                JOIN ingest_runs ir ON ir.run_id = e.run_id
                JOIN markets m ON m.market_id = e.market_id
                LEFT JOIN market_asset_mappings map ON map.market_id = m.market_id
                LEFT JOIN assets a ON a.asset_id = map.asset_id
                WHERE ir.collection_run_id IN ({placeholders})
                ORDER BY e.observed_at, e.rowid
                """,
                run_ids,
            )]
            tag_rows = [dict(row) for row in conn.execute(
                f"""
                SELECT ir.collection_run_id, ir.run_id, e.provider AS venue,
                       ir.source, e.event_type, e.observed_at, e.provider,
                       e.tag, e.raw_tag, a.canonical_symbol
                FROM asset_tag_events e
                JOIN ingest_runs ir ON ir.run_id = e.run_id
                JOIN assets a ON a.asset_id = e.asset_id
                WHERE ir.collection_run_id IN ({placeholders})
                ORDER BY e.observed_at, e.rowid
                """,
                run_ids,
            )]

        runs_by_id = {row["collection_run_id"]: row for row in run_rows}
        universes_by_run: dict[str, list[dict]] = {}
        for row in universe_rows:
            universes_by_run.setdefault(row["collection_run_id"], []).append(row)

        changes_by_run: dict[str, list[dict]] = {}
        lifecycle_labels = {
            "DISCOVERED": "listed",
            "MISSING": "removed",
            "ACTIVATED": "activated",
            "DEACTIVATED": "deactivated",
        }
        for row in lifecycle_rows:
            event_type = row["event_type"]
            asset = row["canonical_symbol"]
            if event_type == "STATUS_CHANGED":
                message = f"{asset} status changed from {row['old_value']} to {row['new_value']}"
            else:
                message = f"{asset} {lifecycle_labels.get(event_type, event_type.lower().replace('_', ' '))}"
            changes_by_run.setdefault(row["collection_run_id"], []).append(
                {
                    "kind": f"MARKET_{event_type}",
                    "venue": row["venue"],
                    "source": row["source"],
                    "run_id": row["run_id"],
                    "observed_at": row["observed_at"],
                    "asset": asset,
                    "market": row["raw_symbol"],
                    "old_value": row["old_value"],
                    "new_value": row["new_value"],
                    "message": message,
                }
            )
        for row in tag_rows:
            action = "added" if row["event_type"] == "ADDED" else "removed"
            asset = row["canonical_symbol"]
            changes_by_run.setdefault(row["collection_run_id"], []).append(
                {
                    "kind": f"TAG_{row['event_type']}",
                    "venue": row["venue"],
                    "source": row["source"],
                    "run_id": row["run_id"],
                    "observed_at": row["observed_at"],
                    "asset": asset,
                    "market": None,
                    "old_value": None,
                    "new_value": f"{row['provider']}:{row['tag']}",
                    "message": f"{asset} {action} {row['raw_tag']} tag",
                }
            )

        for run_id, run in runs_by_id.items():
            try:
                requested_venues = json.loads(run.pop("requested_venues_json"))
            except (TypeError, ValueError):
                requested_venues = []
            universes = universes_by_run.get(run_id, [])
            changes = sorted(
                changes_by_run.get(run_id, []),
                key=lambda item: (item["observed_at"], item["venue"], item["message"]),
            )
            venue_names = set(requested_venues)
            venue_names.update(row["venue"] for row in universes)
            venue_names.update(row["venue"] for row in changes)
            venue_rows = []
            for venue_name in sorted(venue_names):
                venue_universes = [row for row in universes if row["venue"] == venue_name]
                venue_changes = [row for row in changes if row["venue"] == venue_name]
                statuses = {row["status"] for row in venue_universes}
                if "FAILED" in statuses and "SUCCEEDED" in statuses:
                    venue_status = "PARTIAL"
                elif "FAILED" in statuses:
                    venue_status = "FAILED"
                elif statuses == {"SUCCEEDED"}:
                    venue_status = "SUCCEEDED"
                else:
                    venue_status = "RUNNING"
                venue_rows.append(
                    {
                        "venue": venue_name,
                        "status": venue_status,
                        "record_count": sum(int(row["record_count"] or 0) for row in venue_universes),
                        "change_count": len(venue_changes),
                        "changes": venue_changes,
                        "universes": venue_universes,
                    }
                )
            run["requested_venues"] = requested_venues
            run["change_count"] = len(changes)
            run["venues"] = venue_rows
        return {"count": total, "runs": run_rows}

    def stats(self) -> dict:
        self.migrate()
        with self.readonly() as conn:
            totals = dict(conn.execute(
                "SELECT COUNT(*) AS markets, SUM(active) AS active_markets FROM markets"
            ).fetchone())
            by_universe = [dict(row) for row in conn.execute(
                """
                SELECT source, venue, market_type, product, COUNT(*) AS markets,
                       SUM(active) AS active_markets, MAX(last_seen_at) AS last_seen_at
                FROM markets
                GROUP BY source, venue, market_type, product
                ORDER BY venue, market_type, product
                """
            )]
            last_runs = [dict(row) for row in conn.execute(
                """
                SELECT r.source, r.status, r.complete, r.record_count,
                       r.error, r.completed_at
                FROM ingest_runs r
                WHERE r.started_at = (
                    SELECT MAX(r2.started_at)
                    FROM ingest_runs r2
                    WHERE r2.source = r.source
                )
                ORDER BY r.source
                """
            )]
        return {**totals, "universes": by_universe, "last_runs": last_runs}
