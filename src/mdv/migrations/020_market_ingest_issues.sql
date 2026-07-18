CREATE TABLE market_ingest_issues (
    run_id TEXT NOT NULL REFERENCES ingest_runs(run_id),
    issue_index INTEGER NOT NULL,
    source TEXT NOT NULL,
    raw_symbol TEXT NOT NULL,
    error TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    PRIMARY KEY(run_id, issue_index)
);

CREATE INDEX idx_market_ingest_issues_source_symbol
ON market_ingest_issues(source, raw_symbol);
