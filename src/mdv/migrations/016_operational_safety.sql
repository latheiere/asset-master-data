ALTER TABLE assets
ADD COLUMN is_stock INTEGER NOT NULL DEFAULT 0 CHECK(is_stock IN (0, 1));

CREATE INDEX idx_collection_runs_status_started
ON collection_runs(status, started_at);

CREATE INDEX idx_market_observations_retention
ON market_observations(raw_retained, observed_at);

CREATE INDEX idx_financing_observations_retention
ON financing_observations(raw_retained, observed_at);
