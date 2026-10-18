-- Additive lineage marker so imported crew can be distinguished from manual
-- entries and explicitly synthetic demo rows without creating another table.
ALTER TABLE crew ADD COLUMN IF NOT EXISTS data_origin varchar(24) NOT NULL DEFAULT 'manual';

UPDATE crew SET data_origin = 'synthetic_demo' WHERE is_demo = true AND data_origin = 'manual';
