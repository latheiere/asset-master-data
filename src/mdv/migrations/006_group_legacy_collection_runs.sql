CREATE TEMP TABLE legacy_collection_run_groups AS
WITH legacy_runs AS (
    SELECT
        cr.collection_run_id AS old_collection_run_id,
        cr.started_at,
        CASE
            WHEN LAG(cr.started_at) OVER (ORDER BY cr.started_at, cr.collection_run_id) IS NULL THEN 1
            WHEN (
                julianday(cr.started_at) -
                julianday(LAG(cr.started_at) OVER (ORDER BY cr.started_at, cr.collection_run_id))
            ) * 86400.0 > 2.0 THEN 1
            ELSE 0
        END AS starts_group
    FROM collection_runs cr
    JOIN ingest_runs ir
      ON ir.collection_run_id = cr.collection_run_id
     AND ir.run_id = cr.collection_run_id
    WHERE cr.universe_count = 1
), grouped_runs AS (
    SELECT
        old_collection_run_id,
        started_at,
        SUM(starts_group) OVER (ORDER BY started_at, old_collection_run_id) AS group_number
    FROM legacy_runs
)
SELECT
    old_collection_run_id,
    FIRST_VALUE(old_collection_run_id) OVER (
        PARTITION BY group_number
        ORDER BY started_at, old_collection_run_id
    ) AS keeper_collection_run_id
FROM grouped_runs;

UPDATE ingest_runs
SET collection_run_id = (
    SELECT groups.keeper_collection_run_id
    FROM legacy_collection_run_groups groups
    WHERE groups.old_collection_run_id = ingest_runs.collection_run_id
)
WHERE collection_run_id IN (
    SELECT old_collection_run_id FROM legacy_collection_run_groups
);

DELETE FROM collection_runs
WHERE collection_run_id IN (
    SELECT old_collection_run_id
    FROM legacy_collection_run_groups
    WHERE old_collection_run_id != keeper_collection_run_id
);

UPDATE collection_runs AS cr
SET
    scope = CASE
        WHEN (
            SELECT COUNT(DISTINCT ir.venue)
            FROM ingest_runs ir
            WHERE ir.collection_run_id = cr.collection_run_id
        ) > 1 THEN 'ALL'
        ELSE (
            SELECT MIN(ir.venue)
            FROM ingest_runs ir
            WHERE ir.collection_run_id = cr.collection_run_id
        )
    END,
    requested_venues_json = (
        SELECT '[' || GROUP_CONCAT('"' || venues.venue || '"', ',') || ']'
        FROM (
            SELECT DISTINCT ir.venue
            FROM ingest_runs ir
            WHERE ir.collection_run_id = cr.collection_run_id
            ORDER BY ir.venue
        ) AS venues
    ),
    started_at = (
        SELECT MIN(ir.started_at)
        FROM ingest_runs ir
        WHERE ir.collection_run_id = cr.collection_run_id
    ),
    completed_at = (
        SELECT MAX(ir.completed_at)
        FROM ingest_runs ir
        WHERE ir.collection_run_id = cr.collection_run_id
    ),
    status = CASE
        WHEN EXISTS (
            SELECT 1 FROM ingest_runs ir
            WHERE ir.collection_run_id = cr.collection_run_id AND ir.status = 'FAILED'
        ) AND EXISTS (
            SELECT 1 FROM ingest_runs ir
            WHERE ir.collection_run_id = cr.collection_run_id AND ir.status = 'SUCCEEDED'
        ) THEN 'PARTIAL'
        WHEN EXISTS (
            SELECT 1 FROM ingest_runs ir
            WHERE ir.collection_run_id = cr.collection_run_id AND ir.status = 'FAILED'
        ) THEN 'FAILED'
        ELSE 'SUCCEEDED'
    END,
    universe_count = (
        SELECT COUNT(*) FROM ingest_runs ir
        WHERE ir.collection_run_id = cr.collection_run_id
    ),
    succeeded_count = (
        SELECT COUNT(*) FROM ingest_runs ir
        WHERE ir.collection_run_id = cr.collection_run_id AND ir.status = 'SUCCEEDED'
    ),
    failed_count = (
        SELECT COUNT(*) FROM ingest_runs ir
        WHERE ir.collection_run_id = cr.collection_run_id AND ir.status = 'FAILED'
    ),
    record_count = (
        SELECT COALESCE(SUM(ir.record_count), 0) FROM ingest_runs ir
        WHERE ir.collection_run_id = cr.collection_run_id
    ),
    error = (
        SELECT GROUP_CONCAT(ir.error, char(10)) FROM ingest_runs ir
        WHERE ir.collection_run_id = cr.collection_run_id
          AND ir.error IS NOT NULL AND ir.error != ''
    )
WHERE cr.collection_run_id IN (
    SELECT keeper_collection_run_id FROM legacy_collection_run_groups
);

DROP TABLE legacy_collection_run_groups;
