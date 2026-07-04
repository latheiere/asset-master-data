CREATE TABLE financing_products (
    financing_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    venue TEXT NOT NULL REFERENCES venues(venue),
    product TEXT NOT NULL CHECK(product IN ('CROSS_MARGIN', 'CRYPTO_LOAN')),
    asset_role TEXT NOT NULL CHECK(asset_role IN ('BORROWABLE', 'COLLATERAL')),
    raw_asset_symbol TEXT NOT NULL,
    eligible INTEGER NOT NULL,
    status TEXT NOT NULL,
    active INTEGER NOT NULL,
    regular_user_tier TEXT,
    rates_json TEXT NOT NULL,
    terms_json TEXT NOT NULL,
    limits_json TEXT NOT NULL,
    pair_symbols_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    UNIQUE(source, product, asset_role, raw_asset_symbol)
);

CREATE INDEX idx_financing_venue_product_active
ON financing_products(venue, product, asset_role, eligible, active);

CREATE INDEX idx_financing_symbol
ON financing_products(venue, raw_asset_symbol, active);

CREATE TABLE financing_observations (
    run_id TEXT NOT NULL REFERENCES ingest_runs(run_id),
    financing_id TEXT NOT NULL REFERENCES financing_products(financing_id),
    observed_at TEXT NOT NULL,
    eligible INTEGER NOT NULL,
    status TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    rates_json TEXT NOT NULL,
    terms_json TEXT NOT NULL,
    limits_json TEXT NOT NULL,
    pair_symbols_json TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    PRIMARY KEY(run_id, financing_id)
);

CREATE TABLE financing_lifecycle_events (
    event_id TEXT PRIMARY KEY,
    financing_id TEXT NOT NULL REFERENCES financing_products(financing_id),
    run_id TEXT NOT NULL REFERENCES ingest_runs(run_id),
    event_type TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    observed_at TEXT NOT NULL
);

CREATE INDEX idx_financing_lifecycle_time
ON financing_lifecycle_events(financing_id, observed_at DESC);

CREATE TABLE financing_asset_mappings (
    financing_id TEXT PRIMARY KEY REFERENCES financing_products(financing_id),
    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
    venue_symbol TEXT NOT NULL,
    normalized_symbol TEXT NOT NULL,
    method TEXT NOT NULL,
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    matcher_version TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_financing_asset_mappings_asset
ON financing_asset_mappings(asset_id, financing_id);

CREATE TABLE financing_asset_mapping_revisions (
    revision_id TEXT PRIMARY KEY,
    financing_id TEXT NOT NULL REFERENCES financing_products(financing_id),
    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
    venue_symbol TEXT NOT NULL,
    normalized_symbol TEXT NOT NULL,
    method TEXT NOT NULL,
    confidence REAL NOT NULL,
    matcher_version TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
