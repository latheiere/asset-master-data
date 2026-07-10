ALTER TABLE market_observations
ADD COLUMN raw_retained INTEGER NOT NULL DEFAULT 1 CHECK(raw_retained IN (0, 1));

ALTER TABLE financing_observations
ADD COLUMN raw_retained INTEGER NOT NULL DEFAULT 1 CHECK(raw_retained IN (0, 1));

WITH observation_states AS (
    SELECT
        run_id,
        market_id,
        status,
        active,
        LAG(status) OVER (
            PARTITION BY market_id ORDER BY observed_at, run_id
        ) AS prior_status,
        LAG(active) OVER (
            PARTITION BY market_id ORDER BY observed_at, run_id
        ) AS prior_active
    FROM market_observations
)
UPDATE market_observations
SET raw_json = '{}', raw_retained = 0
WHERE (run_id, market_id) IN (
    SELECT run_id, market_id
    FROM observation_states
    WHERE status = prior_status AND active = prior_active
);

WITH observation_states AS (
    SELECT
        run_id,
        financing_id,
        eligible,
        status,
        LAG(eligible) OVER (
            PARTITION BY financing_id ORDER BY observed_at, run_id
        ) AS prior_eligible,
        LAG(status) OVER (
            PARTITION BY financing_id ORDER BY observed_at, run_id
        ) AS prior_status
    FROM financing_observations
)
UPDATE financing_observations
SET
    rates_json = '[]',
    terms_json = '[]',
    limits_json = '{}',
    pair_symbols_json = '[]',
    raw_json = '{}',
    raw_retained = 0
WHERE (run_id, financing_id) IN (
    SELECT run_id, financing_id
    FROM observation_states
    WHERE eligible = prior_eligible AND status = prior_status
);
