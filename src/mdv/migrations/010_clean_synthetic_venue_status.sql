UPDATE markets
SET venue_status = NULL
WHERE venue_status = 'MISSING_FROM_COMPLETE_SNAPSHOT';
