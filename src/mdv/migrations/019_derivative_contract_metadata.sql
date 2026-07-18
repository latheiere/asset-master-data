ALTER TABLE markets ADD COLUMN contract_multiplier_unit TEXT;
ALTER TABLE markets ADD COLUMN contract_value_currency TEXT;
ALTER TABLE markets ADD COLUMN open_interest_unit TEXT;
ALTER TABLE markets ADD COLUMN contract_metadata_reason TEXT;
ALTER TABLE markets ADD COLUMN contract_metadata_source TEXT;
ALTER TABLE markets ADD COLUMN contract_metadata_observed_at TEXT;
ALTER TABLE markets ADD COLUMN contract_metadata_normalization_version TEXT;

-- Existing observations do not contain every authoritative cross-check used by
-- derivative-contract-metadata-v1. Preserve their current values and make the
-- need for a fresh collection explicit instead of manufacturing provenance.
UPDATE markets
SET contract_metadata_reason = 'RECOLLECTION_REQUIRED_FOR_AUTHORITATIVE_METADATA'
WHERE market_type = 'FUTURE'
  AND venue IN ('BITFINEX', 'BITGET', 'WHITEBIT', 'COINBASE')
  AND contract_metadata_reason IS NULL;

CREATE INDEX idx_markets_contract_metadata
ON markets(venue, market_type, active, open_interest_unit, contract_metadata_reason);
