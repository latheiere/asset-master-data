ALTER TABLE markets ADD COLUMN venue_product TEXT;
ALTER TABLE markets ADD COLUMN venue_status TEXT;
ALTER TABLE markets ADD COLUMN contract_direction TEXT;
ALTER TABLE markets ADD COLUMN expiry_cycle TEXT;

UPDATE markets
SET venue_product = product,
    venue_status = CASE
        WHEN status = 'MISSING_FROM_COMPLETE_SNAPSHOT' THEN NULL
        ELSE status
    END;

UPDATE markets
SET expiry_cycle = CASE
        WHEN source = 'GATE_USDT_DELIVERY' AND json_extract(raw_json, '$.cycle') = 'WEEKLY' THEN 'W'
        WHEN source = 'GATE_USDT_DELIVERY' AND json_extract(raw_json, '$.cycle') = 'BI-WEEKLY' THEN 'BW'
        WHEN source = 'GATE_USDT_DELIVERY' AND json_extract(raw_json, '$.cycle') = 'QUARTERLY' THEN 'Q'
        WHEN source = 'GATE_USDT_DELIVERY' AND json_extract(raw_json, '$.cycle') = 'BI-QUARTERLY' THEN 'BQ'
        WHEN contract_type = 'CQ' THEN 'Q'
        WHEN contract_type = 'NQ' THEN 'BQ'
        ELSE NULL
    END,
    contract_direction = CASE
        WHEN market_type = 'SPOT' THEN NULL
        WHEN settle_symbol = base_symbol THEN 'INVERSE'
        WHEN settle_symbol = quote_symbol THEN 'LINEAR'
        ELSE 'QUANTO'
    END;

UPDATE markets
SET contract_type = CASE
        WHEN market_type = 'SPOT' THEN 'SPOT'
        WHEN contract_type = 'PERP' THEN 'PERP'
        ELSE 'DATED'
    END;

UPDATE markets
SET product = CASE
        WHEN market_type = 'SPOT' THEN 'SPOT'
        WHEN contract_type = 'PERP' THEN 'PERP'
        ELSE 'DATED'
    END;

UPDATE markets
SET status = CASE status
        WHEN 'PENDING_TRADING' THEN 'PRELAUNCH'
        WHEN 'TRADING' THEN 'TRADING'
        WHEN 'TRADABLE' THEN 'TRADING'
        WHEN 'ENABLED' THEN 'TRADING'
        WHEN 'ONLINE' THEN 'TRADING'
        WHEN 'NORMAL' THEN 'TRADING'
        WHEN 'BREAK' THEN 'PAUSED'
        WHEN 'HALT' THEN 'PAUSED'
        WHEN 'UNTRADABLE' THEN 'PAUSED'
        WHEN 'SETTLING' THEN 'SETTLING'
        WHEN 'MISSING_FROM_COMPLETE_SNAPSHOT' THEN 'MISSING'
        ELSE 'UNKNOWN'
    END;

CREATE INDEX idx_markets_normalized_dimensions
ON markets(product, expiry_cycle, contract_direction, status, active);
