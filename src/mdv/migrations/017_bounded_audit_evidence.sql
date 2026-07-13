ALTER TABLE market_observations
ADD COLUMN payload_compacted INTEGER NOT NULL DEFAULT 0
CHECK(payload_compacted IN (0, 1));

ALTER TABLE financing_observations
ADD COLUMN payload_compacted INTEGER NOT NULL DEFAULT 0
CHECK(payload_compacted IN (0, 1));

CREATE INDEX idx_market_observations_evidence_retention
ON market_observations(raw_retained, payload_compacted, observed_at);

CREATE INDEX idx_financing_observations_evidence_retention
ON financing_observations(raw_retained, payload_compacted, observed_at);

CREATE TABLE audit_compaction_stats (
    observation_table TEXT PRIMARY KEY
        CHECK(observation_table IN ('market_observations', 'financing_observations')),
    payloads_compacted INTEGER NOT NULL DEFAULT 0,
    evidence_rows_pruned INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);

INSERT INTO audit_compaction_stats(observation_table)
VALUES ('market_observations'), ('financing_observations');
