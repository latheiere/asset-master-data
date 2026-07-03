UPDATE markets SET contract_type = 'PERP' WHERE contract_type = 'PERPETUAL';
UPDATE markets SET contract_type = 'PERP' WHERE contract_type = 'TRADIFI_PERPETUAL';
UPDATE markets SET contract_type = 'CQ' WHERE contract_type = 'CURRENT_QUARTER';
UPDATE markets SET contract_type = 'NQ' WHERE contract_type = 'NEXT_QUARTER';
UPDATE markets SET product = 'PERP' WHERE product = 'PERPETUAL';
