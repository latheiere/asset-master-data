CREATE TABLE manual_asset_actions (
    action_id TEXT PRIMARY KEY,
    action_type TEXT NOT NULL CHECK(action_type IN ('MAP_SYMBOL', 'RENAME_ASSET', 'OTHER')),
    venue TEXT,
    source_symbol TEXT,
    target_symbol TEXT,
    note TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    origin TEXT NOT NULL CHECK(origin IN ('DELIVERY', 'LOCAL')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_manual_asset_actions_enabled
ON manual_asset_actions(enabled, action_type, venue, source_symbol);

CREATE TABLE manual_asset_action_tombstones (
    action_id TEXT PRIMARY KEY,
    deleted_at TEXT NOT NULL
);
