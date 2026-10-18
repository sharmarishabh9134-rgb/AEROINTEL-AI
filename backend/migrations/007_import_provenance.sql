-- Additive lineage marker for operational rows. Existing demo rows are backfilled;
-- other existing rows are treated as manually entered records.
ALTER TABLE aircraft ADD COLUMN IF NOT EXISTS data_origin varchar(24) NOT NULL DEFAULT 'manual';
ALTER TABLE maintenance_records ADD COLUMN IF NOT EXISTS data_origin varchar(24) NOT NULL DEFAULT 'manual';
ALTER TABLE schedule_records ADD COLUMN IF NOT EXISTS data_origin varchar(24) NOT NULL DEFAULT 'manual';
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS data_origin varchar(24) NOT NULL DEFAULT 'manual';

UPDATE aircraft SET data_origin = 'synthetic_demo' WHERE is_demo = true AND data_origin = 'manual';
UPDATE maintenance_records SET data_origin = 'synthetic_demo' WHERE is_demo = true AND data_origin = 'manual';
UPDATE schedule_records SET data_origin = 'synthetic_demo' WHERE is_demo = true AND data_origin = 'manual';
UPDATE alerts SET data_origin = 'synthetic_demo' WHERE is_demo = true AND data_origin = 'manual';
