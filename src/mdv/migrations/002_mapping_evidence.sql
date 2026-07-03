ALTER TABLE market_asset_mappings
ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '{}';

ALTER TABLE market_asset_mapping_revisions
ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '{}';

CREATE TABLE asset_match_candidates (
    candidate_id TEXT PRIMARY KEY,
    source_market_id TEXT NOT NULL REFERENCES markets(market_id),
    proposed_canonical_symbol TEXT NOT NULL,
    rule TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('PROPOSED', 'ACCEPTED', 'REJECTED')),
    score REAL NOT NULL CHECK (score >= 0 AND score <= 1),
    evidence_json TEXT NOT NULL,
    matcher_version TEXT NOT NULL,
    first_evaluated_at TEXT NOT NULL,
    last_evaluated_at TEXT NOT NULL,
    UNIQUE(source_market_id, proposed_canonical_symbol, rule)
);

CREATE INDEX idx_asset_match_candidates_decision
ON asset_match_candidates(decision, proposed_canonical_symbol);
