CREATE TABLE collection_runs (
    collection_run_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    requested_venues_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('RUNNING', 'SUCCEEDED', 'PARTIAL', 'FAILED')),
    universe_count INTEGER NOT NULL DEFAULT 0,
    succeeded_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    record_count INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE INDEX idx_collection_runs_started
ON collection_runs(started_at DESC);

INSERT INTO collection_runs(
    collection_run_id, scope, requested_venues_json, started_at,
    completed_at, status, universe_count, succeeded_count,
    failed_count, record_count, error
)
SELECT
    run_id,
    venue,
    '["' || venue || '"]',
    started_at,
    completed_at,
    CASE WHEN status = 'SUCCEEDED' THEN 'SUCCEEDED' ELSE 'FAILED' END,
    1,
    CASE WHEN status = 'SUCCEEDED' THEN 1 ELSE 0 END,
    CASE WHEN status = 'SUCCEEDED' THEN 0 ELSE 1 END,
    record_count,
    error
FROM ingest_runs;

ALTER TABLE ingest_runs
ADD COLUMN collection_run_id TEXT REFERENCES collection_runs(collection_run_id);

UPDATE ingest_runs
SET collection_run_id = run_id;

CREATE INDEX idx_ingest_runs_collection_run
ON ingest_runs(collection_run_id, venue, source);
