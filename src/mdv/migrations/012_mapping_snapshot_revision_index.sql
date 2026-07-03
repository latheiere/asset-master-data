CREATE INDEX idx_markets_snapshot_revision
ON markets(active, last_seen_at DESC);
