ALTER TABLE markets ADD COLUMN trading_schedule_json TEXT;

CREATE TABLE data_backfills (
    name TEXT PRIMARY KEY,
    completed_at TEXT NOT NULL
);
