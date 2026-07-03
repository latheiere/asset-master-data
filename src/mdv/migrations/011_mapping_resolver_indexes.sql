CREATE INDEX idx_markets_mapping_source
ON markets(venue, active, base_symbol, market_id);

CREATE INDEX idx_markets_mapping_target
ON markets(
    venue, active, market_type, product, contract_type, status,
    quote_symbol, settle_symbol, contract_direction, expiry_cycle, market_id
);

CREATE INDEX idx_markets_mapping_venue_product
ON markets(venue, active, venue_product, market_type, product, market_id);
