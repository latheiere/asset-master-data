from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from collections import defaultdict
from fnmatch import fnmatchcase
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path
from typing import Iterator

from mdv.connectors import market_metadata, market_trade_url, market_trading_schedule
from mdv.connectors.base import market_availability

from mdv.matching import (
    MATCHER_VERSION,
    evaluate_alias_hint,
    normalize_asset_symbol,
    normalize_venue_asset_symbol,
    score_symbol_groups,
    stable_asset_id,
)
from mdv.models import FinancingSnapshot, MarketSnapshot
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


class CollectionBusyError(ValueError):
    """Raised when another process owns the catalog-writer lease."""


class OutOfOrderSnapshotError(ValueError):
    """Raised before an older snapshot can replace a newer current view."""


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


def requested_financing_keys(
    filters: dict[str, object], suffix: str = ""
) -> set[str]:
    values = requested_values(filters, f"financing{suffix}")
    for value in values:
        venue, separator, product = value.partition(":")
        if not separator or not venue or product not in {"MARGIN", "LOAN"}:
            raise ValueError(
                "FINANCING must use VENUE:MARGIN or VENUE:LOAN, "
                "for example BINANCE:MARGIN"
            )
    return values


class SQLiteStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._migrated = False
        self._migration_lock = threading.Lock()

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
    def collection_writer_lease(self) -> Iterator[None]:
        """Hold a non-blocking, process-scoped lease for a complete collection."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f"{self.path.name}.collection.lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        locked = False
        try:
            try:
                import fcntl
            except ImportError as exc:
                raise RuntimeError(
                    "collection writer leases require a POSIX-compatible runtime"
                ) from exc
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CollectionBusyError("another collection is already running") from exc
            locked = True
            yield
        finally:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

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
        if self._migrated:
            return
        with self._migration_lock:
            if self._migrated:
                return
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
                self._sync_delivery_manual_actions(conn)
                schedule_backfill = "market-trading-schedules-v1"
                pending_backfill = conn.execute(
                    "SELECT 1 FROM data_backfills WHERE name = ?",
                    (schedule_backfill,),
                ).fetchone() is None
                if pending_backfill:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        self._sync_market_schedules(conn)
                        conn.execute(
                            "INSERT INTO data_backfills(name, completed_at) VALUES (?, ?)",
                            (schedule_backfill, utc_now()),
                        )
                        conn.execute("COMMIT")
                    except Exception:
                        conn.execute("ROLLBACK")
                        raise
                self._migrated = True
            finally:
                conn.close()

    @staticmethod
    def _sync_market_schedules(conn: sqlite3.Connection) -> None:
        """Backfill normalized schedules through provider-registered policies."""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(markets)")}
        if "trading_schedule_json" not in columns:
            return
        rows = conn.execute(
            """
            SELECT market_id, source, venue, market_type, product, status, active,
                   venue_status, last_seen_at, raw_json
            FROM markets
            """
        ).fetchall()
        for row in rows:
            market = dict(row)
            market["observed_at"] = market["last_seen_at"]
            try:
                raw = json.loads(market.pop("raw_json") or "{}")
            except (TypeError, ValueError):
                raw = {}
            schedule = market_trading_schedule(market, raw)
            normalized_status = market["status"]
            venue_normalized = normalize_status(market.get("venue_status"))
            if schedule is not None and normalized_status == "UNKNOWN" and venue_normalized != "UNKNOWN":
                normalized_status = venue_normalized
            availability = market_availability(
                venue_status=str(market.get("venue_status") or normalized_status),
                normalized_status=normalized_status,
                default_active=bool(market["active"]),
                trading_schedule=schedule,
            )
            conn.execute(
                """
                UPDATE markets
                SET status = ?, active = ?, trading_schedule_json = ?
                WHERE market_id = ?
                """,
                (
                    availability.status,
                    int(availability.active),
                    canonical_json(schedule.as_dict()) if schedule else None,
                    market["market_id"],
                ),
            )

    @staticmethod
    def _normalize_manual_symbol(value: object, *, field: str, required: bool = True) -> str | None:
        raw = str(value or "").strip()
        if not raw:
            if required:
                raise ValueError(f"{field} is required")
            return None
        return normalize_asset_symbol(raw, allow_unit_prefix=False).symbol

    @classmethod
    def _delivery_manual_actions(cls) -> list[dict]:
        try:
            payload = json.loads(
                resources.files("mdv").joinpath("manual_asset_actions.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError("invalid bundled manual asset actions") from exc
        if not isinstance(payload, list):
            raise RuntimeError("bundled manual asset actions must be an array")
        return [cls._validated_manual_action(item, delivery=True) for item in payload]

    @classmethod
    def _validated_manual_action(cls, payload: object, *, delivery: bool = False) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("manual action must be an object")
        action_type = str(payload.get("action_type") or "").strip().upper()
        if action_type not in {"MAP_SYMBOL", "RENAME_ASSET", "OTHER"}:
            raise ValueError("action_type must be MAP_SYMBOL, RENAME_ASSET, or OTHER")
        action_id = str(payload.get("action_id") or "").strip()
        if delivery and not action_id:
            raise ValueError("delivery manual action requires action_id")
        venue = str(payload.get("venue") or "").strip().upper() or None
        source_symbol = cls._normalize_manual_symbol(
            payload.get("source_symbol"), field="source_symbol", required=action_type != "OTHER"
        )
        target_symbol = cls._normalize_manual_symbol(
            payload.get("target_symbol"), field="target_symbol", required=action_type != "OTHER"
        )
        if action_type == "MAP_SYMBOL" and not venue:
            raise ValueError("MAP_SYMBOL requires venue")
        if action_type == "RENAME_ASSET" and venue:
            raise ValueError("RENAME_ASSET must not set venue")
        return {
            "action_id": action_id,
            "action_type": action_type,
            "venue": venue,
            "source_symbol": source_symbol,
            "target_symbol": target_symbol,
            "note": str(payload.get("note") or "").strip()[:2000],
            "enabled": int(bool(payload.get("enabled", True))),
        }

    def _sync_delivery_manual_actions(self, conn: sqlite3.Connection) -> None:
        """Reconcile tracked overrides without overwriting local CRUD changes."""
        actions = self._delivery_manual_actions()
        action_ids = {action["action_id"] for action in actions}
        now = utc_now()
        tombstoned = {
            str(row[0])
            for row in conn.execute("SELECT action_id FROM manual_asset_action_tombstones")
        }
        for action in actions:
            if action["action_id"] in tombstoned:
                continue
            existing = conn.execute(
                "SELECT origin FROM manual_asset_actions WHERE action_id = ?",
                (action["action_id"],),
            ).fetchone()
            if existing is not None and existing["origin"] == "LOCAL":
                continue
            conn.execute(
                """
                INSERT INTO manual_asset_actions(
                    action_id, action_type, venue, source_symbol, target_symbol,
                    note, enabled, origin, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'DELIVERY', ?, ?)
                ON CONFLICT(action_id) DO UPDATE SET
                    action_type=excluded.action_type, venue=excluded.venue,
                    source_symbol=excluded.source_symbol, target_symbol=excluded.target_symbol,
                    note=excluded.note, enabled=excluded.enabled,
                    origin='DELIVERY', updated_at=excluded.updated_at
                """,
                (
                    action["action_id"], action["action_type"], action["venue"],
                    action["source_symbol"], action["target_symbol"], action["note"],
                    action["enabled"], now, now,
                ),
            )
        delivery_rows = [str(row[0]) for row in conn.execute(
            "SELECT action_id FROM manual_asset_actions WHERE origin = 'DELIVERY'"
        )]
        for action_id in delivery_rows:
            if action_id not in action_ids:
                conn.execute("DELETE FROM manual_asset_actions WHERE action_id = ?", (action_id,))

    def list_manual_asset_actions(self) -> list[dict]:
        self.migrate()
        with self.readonly() as conn:
            return [dict(row) for row in conn.execute(
                """
                SELECT action_id, action_type, venue, source_symbol, target_symbol,
                       note, enabled, origin, created_at, updated_at
                FROM manual_asset_actions
                ORDER BY action_type, venue, source_symbol, action_id
                """
            )]

    def create_manual_asset_action(self, payload: dict) -> dict:
        self.migrate()
        action = self._validated_manual_action(payload)
        action_id = str(uuid.uuid4())
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO manual_asset_actions(
                    action_id, action_type, venue, source_symbol, target_symbol,
                    note, enabled, origin, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'LOCAL', ?, ?)
                """,
                (action_id, action["action_type"], action["venue"], action["source_symbol"],
                 action["target_symbol"], action["note"], action["enabled"], now, now),
            )
        self.rebuild_symbol_matches()
        return next(row for row in self.list_manual_asset_actions() if row["action_id"] == action_id)

    def update_manual_asset_action(self, action_id: str, payload: dict) -> dict:
        self.migrate()
        action = self._validated_manual_action(payload)
        now = utc_now()
        with self.transaction() as conn:
            if not conn.execute(
                "SELECT 1 FROM manual_asset_actions WHERE action_id = ?", (action_id,)
            ).fetchone():
                raise ValueError("manual action not found")
            conn.execute(
                """
                UPDATE manual_asset_actions
                SET action_type=?, venue=?, source_symbol=?, target_symbol=?, note=?,
                    enabled=?, origin='LOCAL', updated_at=?
                WHERE action_id=?
                """,
                (action["action_type"], action["venue"], action["source_symbol"],
                 action["target_symbol"], action["note"], action["enabled"], now, action_id),
            )
        self.rebuild_symbol_matches()
        return next(row for row in self.list_manual_asset_actions() if row["action_id"] == action_id)

    def delete_manual_asset_action(self, action_id: str) -> None:
        self.migrate()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT origin FROM manual_asset_actions WHERE action_id = ?", (action_id,)
            ).fetchone()
            if row is None:
                raise ValueError("manual action not found")
            conn.execute("DELETE FROM manual_asset_actions WHERE action_id = ?", (action_id,))
            if row["origin"] == "DELIVERY":
                conn.execute(
                    "INSERT OR REPLACE INTO manual_asset_action_tombstones(action_id, deleted_at) VALUES (?, ?)",
                    (action_id, utc_now()),
                )
        self.rebuild_symbol_matches()

    def market_count(self) -> int:
        self.migrate()
        with self.readonly() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0])

    def reconcile_stale_collection_runs(self, *, stale_after_seconds: int) -> int:
        """Fail closed any parent/child runs abandoned by a dead collector."""
        self.migrate()
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)).isoformat()
        now = utc_now()
        message = "collector exited before completing this run"
        with self.transaction() as conn:
            parent_ids = {
                str(row[0])
                for row in conn.execute(
                    """
                    SELECT collection_run_id FROM collection_runs
                    WHERE status = 'RUNNING'
                      AND julianday(started_at) < julianday(?)
                    """,
                    (cutoff,),
                )
            }
            stale_children = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT run_id, collection_run_id FROM ingest_runs
                    WHERE status = 'RUNNING'
                      AND julianday(started_at) < julianday(?)
                    """,
                    (cutoff,),
                )
            ]
            parent_ids.update(
                str(row["collection_run_id"])
                for row in stale_children
                if row["collection_run_id"] is not None
            )
            if not parent_ids and not stale_children:
                return 0
            if stale_children:
                child_placeholders = ",".join("?" for _ in stale_children)
                conn.execute(
                    f"""
                    UPDATE ingest_runs
                    SET completed_at = ?, status = 'FAILED', complete = 0, error = ?
                    WHERE run_id IN ({child_placeholders}) AND status = 'RUNNING'
                    """,
                    [now, message, *[row["run_id"] for row in stale_children]],
                )
            run_ids = sorted(parent_ids)
            if not run_ids:
                return len(stale_children)
            placeholders = ",".join("?" for _ in run_ids)
            conn.execute(
                f"""
                UPDATE ingest_runs
                SET completed_at = ?, status = 'FAILED', complete = 0, error = ?
                WHERE collection_run_id IN ({placeholders}) AND status = 'RUNNING'
                """,
                [now, message, *run_ids],
            )
            conn.execute(
                f"""
                UPDATE collection_runs
                SET completed_at = ?, status = 'FAILED', error = ?
                WHERE collection_run_id IN ({placeholders})
                """,
                [now, message, *run_ids],
            )
            orphan_count = sum(
                row["collection_run_id"] is None for row in stale_children
            )
            return len(run_ids) + orphan_count

    def readiness(self, *, max_collection_age_seconds: int = 0) -> dict:
        """Return low-cost operational readiness without building projections."""
        self.migrate()
        with self.readonly() as conn:
            active_markets = int(
                conn.execute("SELECT COUNT(*) FROM markets WHERE active = 1").fetchone()[0]
            )
            running_runs = int(
                conn.execute(
                    "SELECT COUNT(*) FROM collection_runs WHERE status = 'RUNNING'"
                ).fetchone()[0]
            )
            running_ingests = int(
                conn.execute(
                    "SELECT COUNT(*) FROM ingest_runs WHERE status = 'RUNNING'"
                ).fetchone()[0]
            )
            latest = conn.execute(
                """
                SELECT collection_run_id, status, completed_at
                FROM collection_runs
                WHERE completed_at IS NOT NULL
                ORDER BY started_at DESC, collection_run_id DESC
                LIMIT 1
                """
            ).fetchone()
            latest_usable = conn.execute(
                """
                SELECT completed_at FROM collection_runs
                WHERE status IN ('SUCCEEDED', 'PARTIAL') AND completed_at IS NOT NULL
                ORDER BY completed_at DESC, collection_run_id DESC
                LIMIT 1
                """
            ).fetchone()
            audit_compaction = {
                str(row["observation_table"]): {
                    "payloads_compacted": int(row["payloads_compacted"]),
                    "evidence_rows_pruned": int(row["evidence_rows_pruned"]),
                    "updated_at": row["updated_at"],
                }
                for row in conn.execute(
                    "SELECT * FROM audit_compaction_stats ORDER BY observation_table"
                )
            }
            retained_observations = {
                table: int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE raw_retained = 1"
                    ).fetchone()[0]
                )
                for table in ("market_observations", "financing_observations")
            }
        age_seconds = None
        fresh = True
        if latest_usable is not None:
            completed = datetime.fromisoformat(
                str(latest_usable["completed_at"]).replace("Z", "+00:00")
            )
            age_seconds = max(
                0, int((datetime.now(timezone.utc) - completed.astimezone(timezone.utc)).total_seconds())
            )
            if max_collection_age_seconds:
                fresh = age_seconds <= max_collection_age_seconds
        elif max_collection_age_seconds:
            fresh = False
        database_bytes = sum(
            candidate.stat().st_size
            for candidate in (
                self.path,
                Path(f"{self.path}-wal"),
                Path(f"{self.path}-shm"),
            )
            if candidate.exists()
        )
        ready = active_markets > 0 and fresh
        return {
            "ready": ready,
            "database": "ok",
            "active_markets": active_markets,
            "running_collections": running_runs,
            "running_ingests": running_ingests,
            "latest_collection": dict(latest) if latest is not None else None,
            "last_usable_collection_age_seconds": age_seconds,
            "collection_fresh": fresh,
            "database_bytes": database_bytes,
            "retained_observations": retained_observations,
            "audit_compaction": audit_compaction,
        }

    def compact_audit_history(
        self,
        *,
        unchanged_retention_days: int,
        changed_payload_retention_days: int = 7,
        max_retained_observations_per_table: int = 100_000,
        batch_size: int = 10_000,
    ) -> dict[str, int]:
        """Bound audit rows/payloads while preserving current state and events."""
        self.migrate()
        if min(
            unchanged_retention_days,
            changed_payload_retention_days,
            max_retained_observations_per_table,
        ) < 0:
            raise ValueError("audit retention values must not be negative")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        now = datetime.now(timezone.utc)
        unchanged_cutoff = (
            now - timedelta(days=unchanged_retention_days)
        ).isoformat()
        payload_cutoff = (
            now - timedelta(days=changed_payload_retention_days)
        ).isoformat()
        result = {
            "market_observations": 0,
            "financing_observations": 0,
            "market_payloads_compacted": 0,
            "financing_payloads_compacted": 0,
            "market_evidence_rows_pruned": 0,
            "financing_evidence_rows_pruned": 0,
        }
        payload_updates = {
            "market_observations": "raw_json = '{}', payload_compacted = 1",
            "financing_observations": (
                "rates_json = '[]', terms_json = '[]', limits_json = '{}', "
                "pair_symbols_json = '[]', raw_json = '{}', payload_compacted = 1"
            ),
        }
        for table in ("market_observations", "financing_observations"):
            if unchanged_retention_days > 0:
                while True:
                    with self.transaction() as conn:
                        cursor = conn.execute(
                            f"""
                            DELETE FROM {table}
                            WHERE rowid IN (
                                SELECT rowid FROM {table}
                                WHERE raw_retained = 0 AND observed_at < ?
                                ORDER BY observed_at, rowid
                                LIMIT ?
                            )
                            """,
                            (unchanged_cutoff, batch_size),
                        )
                        count = max(cursor.rowcount, 0)
                    result[table] += count
                    if count < batch_size:
                        break

            compacted_key = (
                "market_payloads_compacted"
                if table == "market_observations"
                else "financing_payloads_compacted"
            )
            if changed_payload_retention_days > 0:
                while True:
                    with self.transaction() as conn:
                        cursor = conn.execute(
                            f"""
                            UPDATE {table}
                            SET {payload_updates[table]}
                            WHERE rowid IN (
                                SELECT rowid FROM {table}
                                WHERE raw_retained = 1
                                  AND payload_compacted = 0
                                  AND observed_at < ?
                                ORDER BY observed_at, rowid
                                LIMIT ?
                            )
                            """,
                            (payload_cutoff, batch_size),
                        )
                        count = max(cursor.rowcount, 0)
                    result[compacted_key] += count
                    if count < batch_size:
                        break

            pruned_key = (
                "market_evidence_rows_pruned"
                if table == "market_observations"
                else "financing_evidence_rows_pruned"
            )
            if max_retained_observations_per_table > 0:
                while True:
                    with self.transaction() as conn:
                        retained = int(
                            conn.execute(
                                f"SELECT COUNT(*) FROM {table} WHERE raw_retained = 1"
                            ).fetchone()[0]
                        )
                        excess = retained - max_retained_observations_per_table
                        if excess <= 0:
                            count = 0
                        else:
                            cursor = conn.execute(
                                f"""
                                DELETE FROM {table}
                                WHERE rowid IN (
                                    SELECT rowid FROM {table}
                                    WHERE raw_retained = 1
                                    ORDER BY observed_at, rowid
                                    LIMIT ?
                                )
                                """,
                                (min(excess, batch_size),),
                            )
                            count = max(cursor.rowcount, 0)
                    result[pruned_key] += count
                    if count == 0 or count < batch_size:
                        break

            if result[compacted_key] or result[pruned_key]:
                with self.transaction() as conn:
                    conn.execute(
                        """
                        UPDATE audit_compaction_stats
                        SET payloads_compacted = payloads_compacted + ?,
                            evidence_rows_pruned = evidence_rows_pruned + ?,
                            updated_at = ?
                        WHERE observation_table = ?
                        """,
                        (
                            result[compacted_key],
                            result[pruned_key],
                            now.isoformat(),
                            table,
                        ),
                    )
        with self.readonly() as conn:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        return result

    def _assert_snapshot_order(self, *, source: str, observed_at: str) -> None:
        candidate = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        with self.readonly() as conn:
            latest = conn.execute(
                """
                SELECT started_at FROM ingest_runs
                WHERE source = ? AND status = 'SUCCEEDED'
                ORDER BY julianday(started_at) DESC, run_id DESC
                LIMIT 1
                """,
                (source,),
            ).fetchone()
        if latest is None:
            return
        previous = datetime.fromisoformat(str(latest["started_at"]).replace("Z", "+00:00"))
        if candidate.astimezone(timezone.utc) < previous.astimezone(timezone.utc):
            raise OutOfOrderSnapshotError(
                f"{source} snapshot {observed_at} is older than applied snapshot {latest['started_at']}"
            )

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
                    SUM(CASE WHEN status IN ('SUCCEEDED', 'PARTIAL') THEN 1 ELSE 0 END) AS succeeded_count,
                    SUM(CASE WHEN status IN ('FAILED', 'PARTIAL') THEN 1 ELSE 0 END) AS failed_count,
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

    def apply_snapshot(
        self,
        snapshot: MarketSnapshot,
        *,
        collection_run_id: str | None = None,
        rebuild: bool = True,
    ) -> str:
        snapshot.validate()
        self.migrate()
        self._assert_snapshot_order(source=snapshot.source, observed_at=snapshot.observed_at)
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
                raw_json = market.raw_json or canonical_json(market.raw)
                content_hash = hashlib.sha256(raw_json.encode()).hexdigest()
                previous = conn.execute(
                    """
                    SELECT status, active, content_hash, trading_schedule_json
                    FROM markets WHERE market_id = ?
                    """,
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
                schedule_raw = market.raw
                if market.raw_json is not None and not schedule_raw:
                    schedule_raw = json.loads(raw_json)
                schedule = market.trading_schedule or market_trading_schedule(
                    {
                        "source": market.source,
                        "venue": market.venue,
                        "market_type": market.market_type,
                        "product": normalized_product,
                        "status": normalized_status,
                        "venue_status": venue_status,
                        "observed_at": snapshot.observed_at,
                    },
                    schedule_raw,
                )
                availability = market_availability(
                    venue_status=venue_status,
                    normalized_status=normalized_status,
                    default_active=market.active,
                    trading_schedule=schedule,
                )
                normalized_status = availability.status
                active = availability.active
                schedule_json = canonical_json(schedule.as_dict()) if schedule else None
                retain_observation_raw = (
                    previous is None
                    or bool(previous["active"]) != active
                    or previous["status"] != normalized_status
                    or previous["trading_schedule_json"] != schedule_json
                    or previous["content_hash"] != content_hash
                )
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
                        status, active, contract_multiplier, contract_multiplier_unit,
                        contract_value_currency, open_interest_unit,
                        contract_metadata_reason, contract_metadata_source,
                        contract_metadata_observed_at,
                        contract_metadata_normalization_version,
                        expires_at, max_market_order_size,
                        underlying_multiplier, venue_product, venue_status,
                        contract_direction, expiry_cycle, trading_schedule_json,
                        first_seen_at, last_seen_at, raw_json, content_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(market_id) DO UPDATE SET
                        product=excluded.product,
                        base_symbol=excluded.base_symbol,
                        quote_symbol=excluded.quote_symbol,
                        settle_symbol=excluded.settle_symbol,
                        contract_type=excluded.contract_type,
                        status=excluded.status,
                        active=excluded.active,
                        contract_multiplier=excluded.contract_multiplier,
                        contract_multiplier_unit=excluded.contract_multiplier_unit,
                        contract_value_currency=excluded.contract_value_currency,
                        open_interest_unit=excluded.open_interest_unit,
                        contract_metadata_reason=excluded.contract_metadata_reason,
                        contract_metadata_source=excluded.contract_metadata_source,
                        contract_metadata_observed_at=excluded.contract_metadata_observed_at,
                        contract_metadata_normalization_version=excluded.contract_metadata_normalization_version,
                        expires_at=excluded.expires_at,
                        max_market_order_size=excluded.max_market_order_size,
                        underlying_multiplier=excluded.underlying_multiplier,
                        venue_product=excluded.venue_product,
                        venue_status=excluded.venue_status,
                        contract_direction=excluded.contract_direction,
                        expiry_cycle=excluded.expiry_cycle,
                        trading_schedule_json=excluded.trading_schedule_json,
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
                        int(active),
                        market.contract_multiplier,
                        market.contract_multiplier_unit,
                        market.contract_value_currency,
                        market.open_interest_unit,
                        market.contract_metadata_reason,
                        market.contract_metadata_source,
                        market.contract_metadata_observed_at,
                        market.contract_metadata_normalization_version,
                        market.expires_at,
                        market.max_market_order_size,
                        str(normalized.multiplier),
                        venue_product,
                        venue_status,
                        normalized_direction,
                        expiry_cycle,
                        schedule_json,
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
                        content_hash, raw_json, raw_retained
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        market.market_id,
                        snapshot.observed_at,
                        normalized_status,
                        int(active),
                        content_hash,
                        raw_json if retain_observation_raw else "{}",
                        int(retain_observation_raw),
                    ),
                )
                if previous is None:
                    self._insert_event(conn, run_id, market.market_id, "DISCOVERED", None, normalized_status, snapshot.observed_at)
                else:
                    session_only_status_change = (
                        schedule is not None
                        and previous["status"] in {"TRADING", "PAUSED", "UNKNOWN"}
                        and normalized_status in {"TRADING", "PAUSED", "UNKNOWN"}
                    )
                    if bool(previous["active"]) != active and not session_only_status_change:
                        event_type = "ACTIVATED" if active else "DEACTIVATED"
                        self._insert_event(
                            conn,
                            run_id,
                            market.market_id,
                            event_type,
                            str(bool(previous["active"])),
                            str(active),
                            snapshot.observed_at,
                        )
                    if previous["status"] != normalized_status and not session_only_status_change:
                        self._insert_event(
                            conn,
                            run_id,
                            market.market_id,
                            "STATUS_CHANGED",
                            previous["status"],
                            normalized_status,
                            snapshot.observed_at,
                        )

            for issue_index, issue in enumerate(snapshot.issues):
                conn.execute(
                    """
                    INSERT INTO market_ingest_issues(
                        run_id, issue_index, source, raw_symbol, error, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        issue_index,
                        snapshot.source,
                        issue.raw_symbol,
                        issue.error[:2000],
                        canonical_json(issue.raw),
                    ),
                )

            if not snapshot.issues:
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

            issue_error = None
            if snapshot.issues:
                details = "; ".join(
                    f"{issue.raw_symbol}: {issue.error}" for issue in snapshot.issues
                )
                issue_error = f"{len(snapshot.issues)} symbol error(s): {details}"[:2000]

            conn.execute(
                """
                UPDATE ingest_runs
                SET completed_at = ?, status = ?, complete = ?,
                    record_count = ?, error = ?
                WHERE run_id = ?
                """,
                (
                    utc_now(),
                    "PARTIAL" if snapshot.issues else "SUCCEEDED",
                    0 if snapshot.issues else 1,
                    len(snapshot.markets),
                    issue_error,
                    run_id,
                ),
            )

        if rebuild:
            self.rebuild_collection_projections(
                tag_run_id=run_id, tag_observed_at=snapshot.observed_at
            )
        if own_collection_run:
            self.finish_collection_run(collection_run_id)
        return run_id

    def apply_financing_snapshot(
        self,
        snapshot: FinancingSnapshot,
        *,
        collection_run_id: str | None = None,
        rebuild: bool = True,
    ) -> str:
        """Apply one complete public financing universe without account data."""
        snapshot.validate()
        self.migrate()
        self._assert_snapshot_order(source=snapshot.source, observed_at=snapshot.observed_at)
        own_collection_run = collection_run_id is None
        if collection_run_id is None:
            collection_run_id = self.start_collection_run(
                scope=snapshot.venue, venues=[snapshot.venue]
            )
        run_id = str(uuid.uuid4())
        seen_ids = {record.financing_id for record in snapshot.records}
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
                ) VALUES (?, ?, ?, 'FINANCING', ?, ?, 'RUNNING', 0, 0, ?)
                """,
                (
                    run_id,
                    snapshot.source,
                    snapshot.venue,
                    snapshot.product,
                    snapshot.observed_at,
                    collection_run_id,
                ),
            )
            previous_rows = {
                row["financing_id"]: dict(row)
                for row in conn.execute(
                    "SELECT * FROM financing_products WHERE source = ?",
                    (snapshot.source,),
                )
            }
            for record in snapshot.records:
                rates_json = canonical_json(record.rates)
                terms_json = canonical_json(record.terms)
                limits_json = canonical_json(record.limits)
                pair_symbols_json = canonical_json(record.pair_symbols)
                raw_json = canonical_json(record.raw)
                content_hash = hashlib.sha256(raw_json.encode()).hexdigest()
                previous = previous_rows.get(record.financing_id)
                retain_observation_raw = (
                    previous is None
                    or not previous["active"]
                    or bool(previous["eligible"]) != record.eligible
                    or previous["status"] != record.status
                    or previous["content_hash"] != content_hash
                )
                conn.execute(
                    """
                    INSERT INTO financing_products(
                        financing_id, source, venue, product, asset_role,
                        raw_asset_symbol, eligible, status, active,
                        regular_user_tier, rates_json, terms_json, limits_json,
                        pair_symbols_json, first_seen_at, last_seen_at,
                        raw_json, content_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(financing_id) DO UPDATE SET
                        eligible=excluded.eligible,
                        status=excluded.status,
                        active=1,
                        regular_user_tier=excluded.regular_user_tier,
                        rates_json=excluded.rates_json,
                        terms_json=excluded.terms_json,
                        limits_json=excluded.limits_json,
                        pair_symbols_json=excluded.pair_symbols_json,
                        last_seen_at=excluded.last_seen_at,
                        raw_json=excluded.raw_json,
                        content_hash=excluded.content_hash
                    """,
                    (
                        record.financing_id,
                        record.source,
                        record.venue,
                        record.product,
                        record.asset_role,
                        record.raw_asset_symbol,
                        int(record.eligible),
                        record.status,
                        record.regular_user_tier,
                        rates_json,
                        terms_json,
                        limits_json,
                        pair_symbols_json,
                        snapshot.observed_at,
                        snapshot.observed_at,
                        raw_json,
                        content_hash,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO financing_observations(
                        run_id, financing_id, observed_at, eligible, status,
                        content_hash, rates_json, terms_json, limits_json,
                        pair_symbols_json, raw_json, raw_retained
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        record.financing_id,
                        snapshot.observed_at,
                        int(record.eligible),
                        record.status,
                        content_hash,
                        rates_json if retain_observation_raw else "[]",
                        terms_json if retain_observation_raw else "[]",
                        limits_json if retain_observation_raw else "{}",
                        pair_symbols_json if retain_observation_raw else "[]",
                        raw_json if retain_observation_raw else "{}",
                        int(retain_observation_raw),
                    ),
                )
                if previous is None:
                    self._insert_financing_event(
                        conn, run_id, record.financing_id, "DISCOVERED", None,
                        record.status, snapshot.observed_at,
                    )
                elif not previous["active"]:
                    self._insert_financing_event(
                        conn, run_id, record.financing_id, "ACTIVATED", "MISSING",
                        record.status, snapshot.observed_at,
                    )
                else:
                    if bool(previous["eligible"]) != record.eligible:
                        self._insert_financing_event(
                            conn, run_id, record.financing_id, "ELIGIBILITY_CHANGED",
                            str(bool(previous["eligible"])).lower(),
                            str(record.eligible).lower(), snapshot.observed_at,
                        )
                    if previous["status"] != record.status:
                        self._insert_financing_event(
                            conn, run_id, record.financing_id, "STATUS_CHANGED",
                            previous["status"], record.status, snapshot.observed_at,
                        )

            for financing_id, previous in previous_rows.items():
                if financing_id in seen_ids or not previous["active"]:
                    continue
                conn.execute(
                    "UPDATE financing_products SET active = 0 WHERE financing_id = ?",
                    (financing_id,),
                )
                self._insert_financing_event(
                    conn, run_id, financing_id, "MISSING", previous["status"],
                    "MISSING", snapshot.observed_at,
                )
            conn.execute(
                """
                UPDATE ingest_runs
                SET completed_at = ?, status = 'SUCCEEDED', complete = 1,
                    record_count = ?
                WHERE run_id = ?
                """,
                (utc_now(), len(snapshot.records), run_id),
            )
        if rebuild:
            self.rebuild_financing_mappings()
        if own_collection_run:
            self.finish_collection_run(collection_run_id)
        return run_id

    def rebuild_collection_projections(
        self,
        *,
        tag_run_id: str | None = None,
        tag_observed_at: str | None = None,
    ) -> None:
        """Rebuild derived mappings once after a complete collection batch."""
        self.rebuild_symbol_matches()
        if tag_run_id is not None and tag_observed_at is not None:
            self.rebuild_asset_tags(run_id=tag_run_id, observed_at=tag_observed_at)

    @staticmethod
    def _insert_financing_event(
        conn, run_id, financing_id, event_type, old_value, new_value, observed_at
    ) -> None:
        conn.execute(
            """
            INSERT INTO financing_lifecycle_events(
                event_id, financing_id, run_id, event_type, old_value,
                new_value, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()), financing_id, run_id, event_type,
                old_value, new_value, observed_at,
            ),
        )

    def rebuild_financing_mappings(self) -> None:
        """Map financing symbols only through same-venue market mappings."""
        self.migrate()
        now = utc_now()
        with self.transaction() as conn:
            candidates: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
            for row in conn.execute(
                """
                SELECT m.venue, m.base_symbol, map.asset_id,
                       map.normalized_symbol, map.method AS market_method,
                       map.confidence AS market_confidence
                FROM markets m
                JOIN market_asset_mappings map ON map.market_id = m.market_id
                """
            ):
                key = (str(row["venue"]), str(row["base_symbol"]).upper())
                candidates[key][str(row["asset_id"])] = dict(row)
            for financing in conn.execute(
                """
                SELECT financing_id, venue, raw_asset_symbol
                FROM financing_products
                WHERE active = 1
                """
            ):
                financing_id = str(financing["financing_id"])
                financing_symbol = normalize_asset_symbol(
                    str(financing["raw_asset_symbol"]), allow_unit_prefix=False
                ).symbol
                matching_symbols = [financing_symbol]
                if financing_symbol.endswith("STOCK"):
                    stripped_symbol = financing_symbol.removesuffix("STOCK")
                    if stripped_symbol:
                        matching_symbols.append(stripped_symbol)
                else:
                    matching_symbols.append(f"{financing_symbol}STOCK")
                options: dict[str, dict] = {}
                for symbol in matching_symbols:
                    for asset_id, candidate in candidates.get(
                        (str(financing["venue"]), symbol), {}
                    ).items():
                        options.setdefault(asset_id, candidate)
                if len(options) != 1:
                    conn.execute(
                        "DELETE FROM financing_asset_mappings WHERE financing_id = ?",
                        (financing_id,),
                    )
                    continue
                selected = next(iter(options.values()))
                symbol_match = (
                    "EXACT"
                    if str(selected["base_symbol"]).upper() == financing_symbol
                    else "STOCK_SUFFIX_POLICY"
                )
                evidence = canonical_json({
                    "venue": financing["venue"],
                    "financing_symbol": financing["raw_asset_symbol"],
                    "matched_market_symbol": selected["base_symbol"],
                    "symbol_match": symbol_match,
                    "market_mapping_method": selected["market_method"],
                    "market_mapping_confidence": selected["market_confidence"],
                })
                current = (
                    selected["asset_id"],
                    financing["raw_asset_symbol"],
                    selected["normalized_symbol"],
                    (
                        "SAME_VENUE_MARKET_SYMBOL"
                        if symbol_match == "EXACT"
                        else "SAME_VENUE_MARKET_SYMBOL+STOCK_SUFFIX_POLICY"
                    ),
                    float(selected["market_confidence"]),
                    MATCHER_VERSION,
                    evidence,
                )
                previous = conn.execute(
                    """
                    SELECT asset_id, venue_symbol, normalized_symbol, method,
                           confidence, matcher_version, evidence_json
                    FROM financing_asset_mappings WHERE financing_id = ?
                    """,
                    (financing_id,),
                ).fetchone()
                if previous is None or tuple(previous) != current:
                    conn.execute(
                        """
                        INSERT INTO financing_asset_mapping_revisions(
                            revision_id, financing_id, asset_id, venue_symbol,
                            normalized_symbol, method, confidence, matcher_version,
                            evidence_json, recorded_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (str(uuid.uuid4()), financing_id, *current, now),
                    )
                conn.execute(
                    """
                    INSERT INTO financing_asset_mappings(
                        financing_id, asset_id, venue_symbol, normalized_symbol,
                        method, confidence, matcher_version, evidence_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(financing_id) DO UPDATE SET
                        asset_id=excluded.asset_id,
                        venue_symbol=excluded.venue_symbol,
                        normalized_symbol=excluded.normalized_symbol,
                        method=excluded.method,
                        confidence=excluded.confidence,
                        matcher_version=excluded.matcher_version,
                        evidence_json=excluded.evidence_json,
                        updated_at=excluded.updated_at
                    """,
                    (financing_id, *current, now),
                )
            conn.execute(
                """
                DELETE FROM financing_asset_mappings
                WHERE financing_id IN (
                    SELECT financing_id FROM financing_products WHERE active = 0
                )
                """
            )

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
        self._rebuild_market_symbol_matches()
        # Keep the full-market projection's temporary allocations out of the
        # financing pass.  On production-sized databases those allocations
        # otherwise remain live for the duration of this call and inflate the
        # process RSS while the financing candidate index is constructed.
        self.rebuild_financing_mappings()

    def _rebuild_market_symbol_matches(self) -> None:
        self.migrate()
        now = utc_now()
        with self.transaction() as conn:
            manual_actions = [dict(row) for row in conn.execute(
                """
                SELECT action_id, action_type, venue, source_symbol, target_symbol, note
                FROM manual_asset_actions
                WHERE enabled = 1
                """
            )]
            manual_maps = {
                (str(action["venue"]), str(action["source_symbol"])): action
                for action in manual_actions
                if action["action_type"] == "MAP_SYMBOL"
            }
            manual_renames = {
                str(action["source_symbol"]): action
                for action in manual_actions
                if action["action_type"] == "RENAME_ASSET"
            }
            # Stream raw provider payloads and retain only the identity traits
            # needed by this projection.  Materializing every raw_json string,
            # its parsed object graph, and a second prepared market dictionary
            # at once made this pass the collection process's RSS peak.
            market_traits: dict[str, tuple[bool, tuple]] = {}
            exact_symbols: set[str] = set()
            active_symbols_by_venue: dict[str, set[str]] = defaultdict(set)
            classified_symbols_by_venue: dict[str, set[str]] = defaultdict(set)
            for row in conn.execute(
                """
                SELECT market_id, venue, market_type, base_symbol, active, raw_json
                FROM markets
                """
            ):
                market = dict(row)
                try:
                    raw = json.loads(market.pop("raw_json") or "{}")
                except (TypeError, ValueError):
                    raw = {}
                metadata = market_metadata(market, raw)
                symbol = normalize_asset_symbol(
                    market["base_symbol"], allow_unit_prefix=False
                ).symbol
                exact_symbols.add(symbol)
                is_stock = "EQUITY" in metadata.classifications
                if is_stock or metadata.alias_hints:
                    market_traits[market["market_id"]] = (
                        is_stock,
                        metadata.alias_hints,
                    )
                if market["market_type"] == "FUTURE" and market["active"]:
                    active_symbols_by_venue[market["venue"]].add(symbol)
                    if is_stock:
                        classified_symbols_by_venue[market["venue"]].add(symbol)
            prepared = []
            for row in conn.execute(
                """
                SELECT market_id, venue, market_type, base_symbol
                FROM markets
                """
            ):
                market = dict(row)
                is_stock, alias_hints = market_traits.get(
                    market["market_id"], (False, ())
                )
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

                for hint in alias_hints:
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

                stock_suffix_symbol = (
                    raw_symbol.symbol.removesuffix("STOCK")
                    if raw_symbol.symbol.endswith("STOCK")
                    else ""
                )
                if stock_suffix_symbol:
                    stock_suffix_evidence = {
                        "rule": "STOCK_SUFFIX_POLICY",
                        "raw_base_symbol": market["base_symbol"],
                        "proposed_symbol": stock_suffix_symbol,
                        "reason": "Explicit STOCK suffix mapping policy",
                    }
                    candidates.append(
                        {
                            "proposed_symbol": stock_suffix_symbol,
                            "rule": "STOCK_SUFFIX_POLICY",
                            "decision": "ACCEPTED",
                            "score": 1.0,
                            "evidence": stock_suffix_evidence,
                        }
                    )
                    canonical_symbol = stock_suffix_symbol
                    identity_method = "STOCK_SUFFIX_POLICY"
                    multiplier = 1
                    mapping_evidence = stock_suffix_evidence

                manual_action = manual_maps.get((market["venue"], raw_symbol.symbol))
                if manual_action is None:
                    manual_action = manual_renames.get(canonical_symbol)
                if manual_action is not None:
                    canonical_symbol = str(manual_action["target_symbol"])
                    identity_method = f"MANUAL_{manual_action['action_type']}"
                    multiplier = 1
                    mapping_evidence = {
                        "action_id": manual_action["action_id"],
                        "action_type": manual_action["action_type"],
                        "note": manual_action["note"],
                        "raw_base_symbol": market["base_symbol"],
                        "canonical_symbol": canonical_symbol,
                    }

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
                        "is_stock": is_stock,
                        "evidence_score": max(
                            [candidate["score"] for candidate in candidates if candidate["decision"] == "ACCEPTED"],
                            default=0.0,
                        ),
                    }
                )
            del (
                market_traits,
                exact_symbols,
                active_symbols_by_venue,
                classified_symbols_by_venue,
            )
            scores = score_symbol_groups(prepared)
            stock_by_symbol: dict[str, bool] = defaultdict(bool)
            for item in prepared:
                stock_by_symbol[item["normalized_symbol"]] |= bool(item["is_stock"])
            for row in prepared:
                canonical_symbol = row["normalized_symbol"]
                asset_id = stable_asset_id(canonical_symbol)
                group_method, confidence = scores[canonical_symbol]
                method = group_method
                if row["normalizer_method"].startswith(("MANUAL_", "STOCK_SUFFIX_POLICY")):
                    method = f"{row['normalizer_method']}+{group_method}"
                    confidence = 1.0
                elif row["normalizer_method"] != "EXACT_SYMBOL":
                    method = f"{row['normalizer_method']}+{group_method}"
                    confidence = min(max(confidence, row["evidence_score"]), 0.99)
                conn.execute(
                    """
                    INSERT INTO assets(
                        asset_id, canonical_symbol, created_at, updated_at, is_stock
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(asset_id) DO UPDATE SET
                        updated_at=excluded.updated_at,
                        is_stock=excluded.is_stock
                    """,
                    (
                        asset_id,
                        canonical_symbol,
                        now,
                        now,
                        int(stock_by_symbol[canonical_symbol]),
                    ),
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
                    m.contract_multiplier, m.contract_multiplier_unit,
                    m.contract_value_currency, m.open_interest_unit,
                    m.contract_metadata_reason, m.contract_metadata_source,
                    m.contract_metadata_observed_at,
                    m.contract_metadata_normalization_version,
                    m.expires_at, m.max_market_order_size,
                    m.underlying_multiplier, m.venue_product, m.venue_status,
                    m.contract_direction, m.expiry_cycle, m.trading_schedule_json,
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
        result = [dict(row) for row in rows]
        for row in result:
            encoded_schedule = row.pop("trading_schedule_json")
            row["trading_schedule"] = (
                json.loads(encoded_schedule) if encoded_schedule else None
            )
        return result

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

    def list_financing(self, filters: dict[str, object]) -> dict:
        """Return public venue-level margin and crypto-loan metadata."""
        self.migrate()
        clauses = ["f.active = 1"]
        params: list[object] = []
        products = requested_values(filters, "product")
        roles = requested_values(filters, "role")
        invalid_products = products - {"CROSS_MARGIN", "CRYPTO_LOAN"}
        invalid_roles = roles - {"BORROWABLE", "COLLATERAL"}
        if invalid_products:
            raise ValueError(
                f"PRODUCT has unknown value(s): {', '.join(sorted(invalid_products))}"
            )
        if invalid_roles:
            raise ValueError(
                f"ROLE has unknown value(s): {', '.join(sorted(invalid_roles))}"
            )
        for column, key in (
            ("f.venue", "venue"),
            ("f.product", "product"),
            ("f.asset_role", "role"),
            ("f.raw_asset_symbol", "symbol"),
        ):
            values = requested_values(filters, key)
            if values:
                placeholders = ",".join("?" for _ in values)
                clauses.append(f"{column} IN ({placeholders})")
                params.extend(sorted(values))
        if filters.get("eligible") not in (None, ""):
            value = str(filters["eligible"]).strip().lower()
            if value not in {"true", "false", "1", "0"}:
                raise ValueError("ELIGIBLE must be true or false")
            clauses.append("f.eligible = ?")
            params.append(1 if value in {"true", "1"} else 0)
        limit = min(max(int(filters.get("limit") or 5000), 1), 5000)
        offset = max(int(filters.get("offset") or 0), 0)
        where = " AND ".join(clauses)
        with self.readonly() as conn:
            total = int(conn.execute(
                f"SELECT COUNT(*) FROM financing_products f WHERE {where}", params
            ).fetchone()[0])
            rows = [dict(row) for row in conn.execute(
                f"""
                SELECT f.*, map.asset_id, a.canonical_symbol,
                       map.method AS match_method,
                       map.confidence AS match_confidence,
                       map.matcher_version
                FROM financing_products f
                LEFT JOIN financing_asset_mappings map
                  ON map.financing_id = f.financing_id
                LEFT JOIN assets a ON a.asset_id = map.asset_id
                WHERE {where}
                ORDER BY f.venue, f.product, f.asset_role, f.raw_asset_symbol
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            )]
        for row in rows:
            for field in ("rates", "terms", "limits", "pair_symbols"):
                row[field] = json.loads(row.pop(f"{field}_json"))
            row["raw"] = json.loads(row.pop("raw_json"))
            row["eligible"] = bool(row["eligible"])
            row["active"] = bool(row["active"])
        return {"count": total, "financing": rows}

    def list_assets(self, filters: dict[str, object], *, include_details: bool = True) -> dict:
        """Build the active asset -> venue symbol -> market projection."""
        self.migrate()
        symbol_patterns = requested_values(filters, "symbol")

        def sql_pattern(value: str) -> str:
            return (
                value.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
                .replace("*", "%")
            )

        symbol_where = ""
        symbol_params: list[str] = []
        if symbol_patterns:
            matching_conditions = []
            for pattern in sorted(symbol_patterns):
                matching_conditions.append(
                    "(UPPER(a2.canonical_symbol) LIKE ? ESCAPE '\\' OR "
                    "UPPER(m2.base_symbol) LIKE ? ESCAPE '\\' OR "
                    "UPPER(m2.raw_symbol) LIKE ? ESCAPE '\\')"
                )
                symbol_params.extend([sql_pattern(pattern)] * 3)
            symbol_where = (
                "AND a.asset_id IN ("
                "SELECT DISTINCT a2.asset_id "
                "FROM markets m2 "
                "JOIN market_asset_mappings map2 ON map2.market_id = m2.market_id "
                "JOIN assets a2 ON a2.asset_id = map2.asset_id "
                "WHERE m2.active = 1 AND (" + " OR ".join(matching_conditions) + "))"
            )
        with self.readonly() as conn:
            rows = [dict(row) for row in conn.execute(
                """
                SELECT
                    m.market_id, m.venue, m.market_type, m.product,
                    m.raw_symbol, m.base_symbol, m.quote_symbol,
                    m.settle_symbol, m.contract_type, m.status,
                    m.contract_multiplier, m.contract_multiplier_unit,
                    m.contract_value_currency, m.open_interest_unit,
                    m.contract_metadata_reason, m.contract_metadata_source,
                    m.contract_metadata_observed_at,
                    m.contract_metadata_normalization_version,
                    m.expires_at, m.max_market_order_size,
                    m.underlying_multiplier, m.venue_product, m.venue_status,
                    m.contract_direction, m.expiry_cycle, m.trading_schedule_json,
                    m.first_seen_at, m.last_seen_at,
                    a.asset_id, a.canonical_symbol, a.is_stock AS asset_is_stock
                FROM markets m
                JOIN market_asset_mappings map ON map.market_id = m.market_id
                JOIN assets a ON a.asset_id = map.asset_id
                WHERE m.active = 1
                """ + symbol_where + """
                ORDER BY a.canonical_symbol, m.venue, m.market_type,
                         m.product, m.raw_symbol
                """,
                symbol_params,
            )]
            for row in rows:
                encoded_schedule = row.pop("trading_schedule_json")
                row["trading_schedule"] = (
                    json.loads(encoded_schedule) if encoded_schedule else None
                )
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
            financing_rows = [dict(row) for row in conn.execute(
                """
                SELECT f.financing_id, f.venue, f.product, f.asset_role,
                       f.raw_asset_symbol, f.status, f.regular_user_tier,
                       f.last_seen_at, map.asset_id
                FROM financing_products f
                JOIN financing_asset_mappings map
                  ON map.financing_id = f.financing_id
                WHERE f.active = 1 AND f.eligible = 1
                ORDER BY f.venue, f.product, f.asset_role, f.raw_asset_symbol
                """
            )]

        grouped: dict[str, dict] = {}
        for row in rows:
            asset = grouped.setdefault(
                row["asset_id"],
                {
                    "asset_id": row["asset_id"],
                    "canonical_symbol": row["canonical_symbol"],
                    "is_stock": bool(row.pop("asset_is_stock")),
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
        requested_financing = requested_financing_keys(filters)
        excluded_financing = requested_financing_keys(filters, "_not")

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
        financing_by_asset: dict[str, list[dict]] = {}
        for financing_row in financing_rows:
            financing_by_asset.setdefault(financing_row["asset_id"], []).append(
                financing_row
            )
        for asset in grouped.values():
            all_markets = asset["markets"]
            asset_tags = tags_by_asset.get(asset["asset_id"], [])
            asset_financing = financing_by_asset.get(asset["asset_id"], [])
            borrow_eligibility = [
                row for row in asset_financing if row["asset_role"] == "BORROWABLE"
            ]
            financing_keys = {
                f"{row['venue']}:{'MARGIN' if row['product'] == 'CROSS_MARGIN' else 'LOAN'}"
                for row in borrow_eligibility
            }
            if not requested_financing.issubset(financing_keys):
                continue
            if excluded_financing & financing_keys:
                continue
            asset_tag_keys = {(item["provider"], item["tag"]) for item in asset_tags}
            if not requested_tags.issubset(asset_tag_keys):
                continue
            if excluded_tags & asset_tag_keys:
                continue
            is_stock = bool(asset["is_stock"])
            for row in all_markets:
                row.pop("asset_is_stock", None)
                row["is_stock"] = is_stock
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
                target = venue["spot"] if market["market_type"] == "SPOT" else venue["futures"]
                target.append(market)

            for financing in borrow_eligibility:
                if financing["venue"] in venues:
                    venues[financing["venue"]].setdefault("financing", []).append(financing)

            venue_rows = []
            for venue in sorted(venues.values(), key=lambda item: item["venue"]):
                venue["symbols"] = sorted(venue["symbols"])
                venue["spot"].sort(key=lambda item: (item["product"], item["raw_symbol"]))
                venue["futures"].sort(key=lambda item: (item["product"], item["raw_symbol"]))
                venue.setdefault("financing", [])
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
            perp_venues = [
                {
                    "venue": venue["venue"],
                    "count": sum(
                        market["product"] == "PERP" for market in venue["futures"]
                    ),
                }
                for venue in venue_rows
                if any(market["product"] == "PERP" for market in venue["futures"])
            ]
            dated_venues = [
                {
                    "venue": venue["venue"],
                    "count": sum(
                        market["product"] == "DATED" for market in venue["futures"]
                    ),
                }
                for venue in venue_rows
                if any(market["product"] == "DATED" for market in venue["futures"])
            ]
            margin_venues = [
                {"venue": financing["venue"], "count": 1}
                for financing in borrow_eligibility
                if financing["product"] == "CROSS_MARGIN"
            ]
            loan_venues = [
                {"venue": financing["venue"], "count": 1}
                for financing in borrow_eligibility
                if financing["product"] == "CRYPTO_LOAN"
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
                    "markets": markets if include_details else [],
                    "venues": venue_rows if include_details else [],
                    "venue_symbols": [
                        {"venue": venue["venue"], "symbols": venue["symbols"]}
                        for venue in venue_rows
                    ] if include_details else [],
                    "spot_venues": spot_venues,
                    "future_venues": future_venues,
                    "perp_venues": perp_venues,
                    "dated_venues": dated_venues,
                    "margin_venues": margin_venues,
                    "loan_venues": loan_venues,
                    "future_coverage": coverage_label,
                    "future_coverage_kind": coverage_kind,
                    "active_market_count": len(markets),
                    "is_stock": is_stock,
                    "tags": asset_tags if include_details else [],
                    "financing": asset_financing if include_details else [],
                    "borrow_eligibility": borrow_eligibility if include_details else [],
                }
            )
            assets.append(asset)

        assets.sort(key=lambda item: item["canonical_symbol"])
        total = len(assets)
        assets = assets[offset : offset + limit]
        if include_details and assets:
            selected_ids = [asset["asset_id"] for asset in assets]
            placeholders = ",".join("?" for _ in selected_ids)
            with self.readonly() as conn:
                detail_rows = [dict(row) for row in conn.execute(
                    f"""
                    SELECT f.financing_id, f.venue, f.product, f.asset_role,
                           f.raw_asset_symbol, f.status, f.regular_user_tier,
                           f.rates_json, f.terms_json, f.limits_json,
                           f.pair_symbols_json, f.last_seen_at, map.asset_id
                    FROM financing_products f
                    JOIN financing_asset_mappings map
                      ON map.financing_id = f.financing_id
                    WHERE f.active = 1 AND f.eligible = 1
                      AND map.asset_id IN ({placeholders})
                    ORDER BY f.venue, f.product, f.asset_role, f.raw_asset_symbol
                    """,
                    selected_ids,
                )]
            detailed_financing: dict[str, list[dict]] = defaultdict(list)
            for financing_row in detail_rows:
                rates = json.loads(financing_row.pop("rates_json"))
                terms = json.loads(financing_row.pop("terms_json"))
                limits = json.loads(financing_row.pop("limits_json"))
                pair_symbols = json.loads(financing_row.pop("pair_symbols_json"))
                regular_rates = [rate for rate in rates if rate.get("regular_user")]
                preferred_rate = next(
                    (rate for rate in regular_rates if rate.get("rate_type") == "FLEXIBLE"),
                    regular_rates[0] if regular_rates else None,
                )
                detailed_financing[financing_row["asset_id"]].append(
                    {
                        **financing_row,
                        "regular_rate": preferred_rate,
                        "rate_count": len(rates),
                        "terms": terms,
                        "limits": limits,
                        "pair_symbols": pair_symbols,
                    }
                )
            for asset in assets:
                for market in asset["markets"]:
                    market["underlying_unit"] = asset["canonical_symbol"]
                    if market["underlying_multiplier"] != "1":
                        market["underlying_unit"] = (
                            f"{market['underlying_multiplier']} {asset['canonical_symbol']}"
                        )
                    market["trade_url"] = market_trade_url(market)
                financing = detailed_financing.get(asset["asset_id"], [])
                borrow = [row for row in financing if row["asset_role"] == "BORROWABLE"]
                asset["financing"] = financing
                asset["borrow_eligibility"] = borrow
                for venue in asset["venues"]:
                    venue["financing"] = [
                        row for row in borrow if row["venue"] == venue["venue"]
                    ]
        return {
            "assets": assets,
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
            financing = [str(row[0]) for row in conn.execute(
                """
                SELECT DISTINCT
                    venue || ':' ||
                    CASE product
                        WHEN 'CROSS_MARGIN' THEN 'MARGIN'
                        ELSE 'LOAN'
                    END
                FROM financing_products
                WHERE active = 1 AND eligible = 1
                  AND asset_role = 'BORROWABLE'
                ORDER BY 1
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
                "FINANCING": enum(
                    financing,
                    "Provider-scoped cross-margin or crypto-loan eligibility.",
                ),
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
                "LIMIT": {"kind": "integer", "default": 500, "minimum": 1, "maximum": 5000},
                "OFFSET": {"kind": "integer", "default": 0, "minimum": 0},
            }
        return {"filters": filters}

    def list_collection_runs(
        self,
        *,
        limit: int = 10,
        offset: int = 0,
        venue: str | None = None,
        action: str | None = None,
        tag: str | None = None,
        symbol: str | None = None,
        product: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        changed_only: bool = False,
    ) -> dict:
        """Return collection invocations with universe outcomes and audit changes."""
        self.migrate()
        limit = min(max(int(limit), 1), 500)
        offset = max(int(offset), 0)
        requested_venue = str(venue or "").strip().upper()
        requested_action = str(action or "").strip().upper()
        action_event_types = {
            "LISTING": ("lifecycle", "DISCOVERED"),
            "REMOVAL": ("lifecycle", "MISSING"),
            "TAG_ADDED": ("tag", "ADDED"),
            "TAG_REMOVED": ("tag", "REMOVED"),
        }
        if requested_action and requested_action not in action_event_types:
            raise ValueError(
                "ACTION must be LISTING, REMOVAL, TAG_ADDED, or TAG_REMOVED"
            )
        requested_tag = normalized_tag_key(tag) if str(tag or "").strip() else None
        requested_symbol = str(symbol or "").strip().upper()
        requested_product = str(product or "").strip().upper()
        if requested_product and requested_product not in {"SPOT", "PERP", "DATED"}:
            raise ValueError("PRODUCT must be SPOT, PERP, or DATED")
        if requested_tag and requested_symbol:
            raise ValueError("TAG and SYMBOL cannot be combined")
        if requested_tag and requested_product:
            raise ValueError("TAG and PRODUCT cannot be combined")
        if requested_tag and requested_action in {"LISTING", "REMOVAL"}:
            raise ValueError("TAG cannot filter LISTING or REMOVAL actions")
        if requested_symbol and requested_action in {"TAG_ADDED", "TAG_REMOVED"}:
            raise ValueError("SYMBOL cannot filter TAG_ADDED or TAG_REMOVED actions")
        if requested_product and requested_action in {"TAG_ADDED", "TAG_REMOVED"}:
            raise ValueError("PRODUCT cannot filter TAG_ADDED or TAG_REMOVED actions")

        def change_date(value: str | None, name: str) -> str | None:
            raw = str(value or "").strip()
            if not raw:
                return None
            try:
                parsed = datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
            except ValueError as exc:
                raise ValueError(f"{name} must use YYYY-MM-DD") from exc
            if parsed != raw:
                raise ValueError(f"{name} must use YYYY-MM-DD")
            return parsed

        requested_date_from = change_date(date_from, "DATE_FROM")
        requested_date_to = change_date(date_to, "DATE_TO")
        if requested_date_from and requested_date_to and requested_date_from > requested_date_to:
            raise ValueError("DATE_FROM must be on or before DATE_TO")

        where_clauses: list[str] = []
        params: list[object] = []
        if requested_venue:
            where_clauses.append(
                "EXISTS (SELECT 1 FROM ingest_runs ir "
                "WHERE ir.collection_run_id = cr.collection_run_id AND ir.venue = ?)"
            )
            params.append(requested_venue)

        if changed_only:
            where_clauses.append(
                "(EXISTS ("
                "SELECT 1 FROM market_lifecycle_events e "
                "JOIN ingest_runs ir ON ir.run_id = e.run_id "
                "WHERE ir.collection_run_id = cr.collection_run_id"
                ") OR EXISTS ("
                "SELECT 1 FROM asset_tag_events e "
                "JOIN ingest_runs ir ON ir.run_id = e.run_id "
                "WHERE ir.collection_run_id = cr.collection_run_id"
                "))"
            )

        change_filters_active = bool(
            requested_action
            or requested_tag
            or requested_symbol
            or requested_product
            or requested_date_from
            or requested_date_to
            or changed_only
        )
        if change_filters_active:
            matching_queries: list[str] = []
            matching_params: list[object] = []
            action_family, action_event_type = action_event_types.get(
                requested_action, (None, None)
            )

            if action_family in (None, "lifecycle") and requested_tag is None:
                lifecycle_clauses: list[str] = []
                if action_event_type:
                    lifecycle_clauses.append("e.event_type = ?")
                    matching_params.append(action_event_type)
                if requested_venue:
                    lifecycle_clauses.append("ir.venue = ?")
                    matching_params.append(requested_venue)
                if requested_symbol:
                    escaped_symbol = (
                        requested_symbol.replace("\\", "\\\\")
                        .replace("%", "\\%")
                        .replace("_", "\\_")
                        .replace("*", "%")
                    )
                    lifecycle_clauses.append(
                        "(UPPER(m.raw_symbol) LIKE ? ESCAPE '\\' OR "
                        "UPPER(m.base_symbol) LIKE ? ESCAPE '\\' OR "
                        "UPPER(a.canonical_symbol) LIKE ? ESCAPE '\\')"
                    )
                    matching_params.extend([escaped_symbol] * 3)
                if requested_product:
                    lifecycle_clauses.append("m.product = ?")
                    matching_params.append(requested_product)
                if requested_date_from:
                    lifecycle_clauses.append("date(e.observed_at) >= ?")
                    matching_params.append(requested_date_from)
                if requested_date_to:
                    lifecycle_clauses.append("date(e.observed_at) <= ?")
                    matching_params.append(requested_date_to)
                matching_queries.append(
                    "SELECT ir.collection_run_id "
                    "FROM market_lifecycle_events e "
                    "JOIN ingest_runs ir ON ir.run_id = e.run_id "
                    "JOIN markets m ON m.market_id = e.market_id "
                    "LEFT JOIN market_asset_mappings map ON map.market_id = m.market_id "
                    "LEFT JOIN assets a ON a.asset_id = map.asset_id "
                    + ("WHERE " + " AND ".join(lifecycle_clauses) if lifecycle_clauses else "")
                )

            if (
                action_family in (None, "tag")
                and not requested_symbol
                and not requested_product
            ):
                tag_clauses: list[str] = []
                if action_event_type:
                    tag_clauses.append("e.event_type = ?")
                    matching_params.append(action_event_type)
                if requested_tag:
                    tag_clauses.extend(("e.provider = ?", "e.tag = ?"))
                    matching_params.extend(requested_tag)
                if requested_venue:
                    tag_clauses.append("e.provider = ?")
                    matching_params.append(requested_venue)
                if requested_date_from:
                    tag_clauses.append("date(e.observed_at) >= ?")
                    matching_params.append(requested_date_from)
                if requested_date_to:
                    tag_clauses.append("date(e.observed_at) <= ?")
                    matching_params.append(requested_date_to)
                matching_queries.append(
                    "SELECT ir.collection_run_id "
                    "FROM asset_tag_events e "
                    "JOIN ingest_runs ir ON ir.run_id = e.run_id "
                    + ("WHERE " + " AND ".join(tag_clauses) if tag_clauses else "")
                )

            if matching_queries:
                where_clauses.append(
                    "cr.collection_run_id IN (" + " UNION ".join(matching_queries) + ")"
                )
                params.extend(matching_params)
            else:
                where_clauses.append("0")

        where = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
        with self.readonly() as conn:
            available_tags = [str(row[0]) for row in conn.execute(
                """
                SELECT DISTINCT provider || ':' || tag
                FROM asset_tag_events
                ORDER BY provider || ':' || tag
                """
            )]
            available_venues = [str(row[0]) for row in conn.execute(
                "SELECT DISTINCT venue FROM ingest_runs ORDER BY venue"
            )]
            filter_options = {
                "actions": list(action_event_types),
                "tags": available_tags,
                "venues": available_venues,
                "products": ["PERP", "DATED", "SPOT"],
            }
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
                return {"count": total, "runs": [], "filter_options": filter_options}
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
                       e.market_id,
                       e.event_type, e.old_value, e.new_value, e.observed_at,
                       m.raw_symbol, m.base_symbol, m.product, m.market_type,
                       m.first_seen_at, m.trading_schedule_json,
                       map.asset_id AS mapping_asset_id,
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
            earliest_market_rows = [dict(row) for row in conn.execute(
                """
                SELECT m.venue, m.market_type,
                       COALESCE(map.asset_id, m.base_symbol) AS asset_key,
                       MIN(m.first_seen_at) AS earliest_first_seen_at
                FROM markets m
                LEFT JOIN market_asset_mappings map ON map.market_id = m.market_id
                GROUP BY m.venue, m.market_type, COALESCE(map.asset_id, m.base_symbol)
                """
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
        earliest_market_by_key = {
            (row["venue"], row["market_type"], row["asset_key"]): row["earliest_first_seen_at"]
            for row in earliest_market_rows
        }
        lifecycle_labels = {
            "DISCOVERED": "listed",
            "MISSING": "removed",
            "ACTIVATED": "activated",
            "DEACTIVATED": "deactivated",
        }
        session_values = {"TRADING", "PAUSED", "UNKNOWN"}
        session_transition_keys = {
            (row["run_id"], row["market_id"])
            for row in lifecycle_rows
            if row["trading_schedule_json"]
            and row["event_type"] == "STATUS_CHANGED"
            and row["old_value"] in session_values
            and row["new_value"] in session_values
        }
        for row in lifecycle_rows:
            event_type = row["event_type"]
            session_transition = (row["run_id"], row["market_id"]) in session_transition_keys
            if session_transition and event_type in {
                "STATUS_CHANGED", "ACTIVATED", "DEACTIVATED",
            }:
                continue
            asset = row["canonical_symbol"]
            kind = f"MARKET_{event_type}"
            if event_type == "STATUS_CHANGED":
                message = f"{asset} status changed from {row['old_value']} to {row['new_value']}"
            else:
                message = f"{asset} {lifecycle_labels.get(event_type, event_type.lower().replace('_', ' '))}"
            asset_key = row["mapping_asset_id"] or row["base_symbol"]
            if (
                event_type == "DISCOVERED"
                and earliest_market_by_key.get(
                    (row["venue"], row["market_type"], asset_key)
                ) < row["first_seen_at"]
            ):
                kind = "MARKET_LISTED"
                message = f"{asset} listed"
            changes_by_run.setdefault(row["collection_run_id"], []).append(
                {
                    "kind": kind,
                    "venue": row["venue"],
                    "source": row["source"],
                    "run_id": row["run_id"],
                    "observed_at": row["observed_at"],
                    "asset": asset,
                    "market": row["raw_symbol"],
                    "product": row["product"],
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
                    "product": None,
                    "old_value": None,
                    "new_value": f"{row['provider']}:{row['tag']}",
                    "message": f"{asset} {action} {row['raw_tag']} tag",
                }
            )

        if change_filters_active:
            expected_kinds = {
                "LISTING": {"MARKET_DISCOVERED", "MARKET_LISTED"},
                "REMOVAL": "MARKET_MISSING",
                "TAG_ADDED": "TAG_ADDED",
                "TAG_REMOVED": "TAG_REMOVED",
            }.get(requested_action)

            def selected_change(change: dict) -> bool:
                if expected_kinds and change["kind"] not in (
                    expected_kinds if isinstance(expected_kinds, set) else {expected_kinds}
                ):
                    return False
                if requested_tag and change["new_value"] != ":".join(requested_tag):
                    return False
                if requested_symbol and not any(
                    fnmatchcase(str(value or "").upper(), requested_symbol)
                    for value in (change["asset"], change["market"])
                ):
                    return False
                if requested_product and change["product"] != requested_product:
                    return False
                if requested_venue and change["venue"] != requested_venue:
                    return False
                observed_date = datetime.fromisoformat(
                    str(change["observed_at"]).replace("Z", "+00:00")
                ).astimezone(timezone.utc).date().isoformat()
                if requested_date_from and observed_date < requested_date_from:
                    return False
                if requested_date_to and observed_date > requested_date_to:
                    return False
                return True

            changes_by_run = {
                run_id: [change for change in changes if selected_change(change)]
                for run_id, changes in changes_by_run.items()
            }

        for run_id, run in runs_by_id.items():
            try:
                requested_venues = json.loads(run.pop("requested_venues_json"))
            except (TypeError, ValueError):
                requested_venues = []
            universes = universes_by_run.get(run_id, [])
            product_order = {"PERP": 0, "DATED": 1, "SPOT": 2}
            changes = sorted(
                changes_by_run.get(run_id, []),
                key=lambda item: (
                    product_order.get(item["product"], 3)
                    if requested_action in {"LISTING", "REMOVAL"}
                    else 0,
                    item["observed_at"],
                    item["venue"],
                    item["message"],
                ),
            )
            venue_names = set(requested_venues)
            venue_names.update(row["venue"] for row in universes)
            venue_names.update(row["venue"] for row in changes)
            venue_rows = []
            for venue_name in sorted(venue_names):
                venue_universes = [row for row in universes if row["venue"] == venue_name]
                venue_changes = [row for row in changes if row["venue"] == venue_name]
                if change_filters_active and not venue_changes:
                    continue
                statuses = {row["status"] for row in venue_universes}
                if "PARTIAL" in statuses or (
                    "FAILED" in statuses and "SUCCEEDED" in statuses
                ):
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
        return {"count": total, "runs": run_rows, "filter_options": filter_options}

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
