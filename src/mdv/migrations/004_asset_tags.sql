CREATE TABLE asset_tags (
    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
    provider TEXT NOT NULL,
    tag TEXT NOT NULL,
    raw_tag TEXT NOT NULL,
    active INTEGER NOT NULL,
    evidence_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(asset_id, provider, tag)
);

CREATE INDEX idx_asset_tags_provider_tag_active
ON asset_tags(provider, tag, active);

CREATE TABLE asset_tag_events (
    event_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
    run_id TEXT NOT NULL REFERENCES ingest_runs(run_id),
    provider TEXT NOT NULL,
    tag TEXT NOT NULL,
    raw_tag TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('ADDED', 'REMOVED')),
    observed_at TEXT NOT NULL
);

CREATE INDEX idx_asset_tag_events_asset_time
ON asset_tag_events(asset_id, observed_at DESC);
