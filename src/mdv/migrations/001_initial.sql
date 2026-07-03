CREATE TABLE venues (
    venue TEXT PRIMARY KEY,
    display_name TEXT NOT NULL
);
CREATE TABLE ingest_runs (
    run_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    venue TEXT NOT NULL,
    market_type TEXT NOT NULL,
    product TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    complete INTEGER NOT NULL DEFAULT 0,
    record_count INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE INDEX idx_ingest_runs_source_started
ON ingest_runs(source, started_at DESC);

CREATE TABLE assets (
    asset_id TEXT PRIMARY KEY,
    canonical_symbol TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE markets (
    market_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    venue TEXT NOT NULL REFERENCES venues(venue),
    market_type TEXT NOT NULL CHECK (market_type IN ('SPOT', 'FUTURE')),
    product TEXT NOT NULL,
    raw_symbol TEXT NOT NULL,
    base_symbol TEXT NOT NULL,
    quote_symbol TEXT NOT NULL,
    settle_symbol TEXT,
    contract_type TEXT NOT NULL,
    status TEXT NOT NULL,
    active INTEGER NOT NULL,
    contract_multiplier TEXT,
    underlying_multiplier TEXT NOT NULL DEFAULT '1',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    UNIQUE(source, raw_symbol)
);

CREATE INDEX idx_markets_type_venue_active
ON markets(market_type, venue, active);

CREATE INDEX idx_markets_symbols
ON markets(base_symbol, quote_symbol, raw_symbol);

CREATE TABLE market_observations (
    run_id TEXT NOT NULL REFERENCES ingest_runs(run_id),
    market_id TEXT NOT NULL REFERENCES markets(market_id),
    observed_at TEXT NOT NULL,
    status TEXT NOT NULL,
    active INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    PRIMARY KEY(run_id, market_id)
);

CREATE TABLE market_lifecycle_events (
    event_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL REFERENCES markets(market_id),
    run_id TEXT NOT NULL REFERENCES ingest_runs(run_id),
    event_type TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    observed_at TEXT NOT NULL
);

CREATE INDEX idx_lifecycle_market_time
ON market_lifecycle_events(market_id, observed_at DESC);

CREATE TABLE market_asset_mappings (
    market_id TEXT PRIMARY KEY REFERENCES markets(market_id),
    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
    venue_symbol TEXT NOT NULL,
    normalized_symbol TEXT NOT NULL,
    method TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    matcher_version TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_market_asset_mappings_asset
ON market_asset_mappings(asset_id);

CREATE TABLE market_asset_mapping_revisions (
    revision_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL REFERENCES markets(market_id),
    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
    venue_symbol TEXT NOT NULL,
    normalized_symbol TEXT NOT NULL,
    method TEXT NOT NULL,
    confidence REAL NOT NULL,
    matcher_version TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
