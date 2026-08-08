ALTER TABLE markets ADD COLUMN venue_base_multiplier TEXT;

UPDATE markets
SET venue_base_multiplier = COALESCE(NULLIF(underlying_multiplier, ''), '1')
WHERE market_type = 'FUTURE';

-- Convert the prior asset-symbol field into an explicit denomination. A
-- canonical suffix different from the venue base is safe only for approved
-- bundled-unit spellings already represented by underlying_multiplier.
UPDATE markets
SET contract_multiplier_unit = CASE
    WHEN contract_multiplier IS NULL THEN NULL
    WHEN contract_multiplier_unit IN (
        'VENUE_BASE', 'CANONICAL_BASE', 'QUOTE', 'SETTLEMENT'
    ) THEN contract_multiplier_unit
    WHEN venue = 'COINBASE' THEN 'CANONICAL_BASE'
    WHEN UPPER(contract_multiplier_unit) = UPPER(base_symbol) THEN 'VENUE_BASE'
    WHEN UPPER(contract_multiplier_unit) = UPPER(quote_symbol) THEN 'QUOTE'
    WHEN settle_symbol IS NOT NULL
         AND UPPER(contract_multiplier_unit) = UPPER(settle_symbol)
        THEN 'SETTLEMENT'
    WHEN underlying_multiplier <> '1'
         AND (
             UPPER(base_symbol) = '1000' || UPPER(contract_multiplier_unit)
             OR UPPER(base_symbol) = '10000' || UPPER(contract_multiplier_unit)
             OR UPPER(base_symbol) = '1000000' || UPPER(contract_multiplier_unit)
             OR UPPER(base_symbol) = '1M' || UPPER(contract_multiplier_unit)
         )
        THEN 'CANONICAL_BASE'
    ELSE NULL
END
WHERE market_type = 'FUTURE';

UPDATE markets
SET contract_multiplier = NULL,
    contract_value_currency = NULL,
    open_interest_unit = NULL,
    contract_metadata_reason = COALESCE(
        contract_metadata_reason,
        'RECOLLECTION_REQUIRED_FOR_EXPLICIT_CONTRACT_DENOMINATION'
    ),
    contract_metadata_normalization_version = 'derivative-contract-metadata-v2'
WHERE market_type = 'FUTURE'
  AND contract_multiplier IS NOT NULL
  AND contract_multiplier_unit IS NULL;

UPDATE markets
SET contract_metadata_normalization_version = 'derivative-contract-metadata-v2'
WHERE market_type = 'FUTURE'
  AND contract_multiplier IS NOT NULL
  AND contract_multiplier_unit IS NOT NULL;

CREATE INDEX idx_markets_canonical_base_contract_units
ON markets(
    market_type, product, contract_multiplier_unit, venue_base_multiplier
);
