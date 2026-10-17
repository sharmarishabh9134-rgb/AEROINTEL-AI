-- Additive provenance and display fields for explicitly labelled demo records.
-- Apply this migration before deploying the matching ORM models to PostgreSQL.
ALTER TABLE aircraft ADD COLUMN IF NOT EXISTS registration varchar(24);
ALTER TABLE aircraft ADD COLUMN IF NOT EXISTS aircraft_type varchar(140);
ALTER TABLE aircraft ADD COLUMN IF NOT EXISTS base_airport varchar(4);
ALTER TABLE aircraft ADD COLUMN IF NOT EXISTS next_maintenance_due timestamptz;
ALTER TABLE aircraft ADD COLUMN IF NOT EXISTS hours_since_check double precision;
ALTER TABLE aircraft ADD COLUMN IF NOT EXISTS is_demo boolean NOT NULL DEFAULT false;

ALTER TABLE crew ADD COLUMN IF NOT EXISTS display_name varchar(100);
ALTER TABLE crew ADD COLUMN IF NOT EXISTS role varchar(60);
ALTER TABLE crew ADD COLUMN IF NOT EXISTS license_rating varchar(120);
ALTER TABLE crew ADD COLUMN IF NOT EXISTS duty_hours double precision;
ALTER TABLE crew ADD COLUMN IF NOT EXISTS rest_hours double precision;
ALTER TABLE crew ADD COLUMN IF NOT EXISTS medical_expiry timestamptz;
ALTER TABLE crew ADD COLUMN IF NOT EXISTS currency_expiry timestamptz;
ALTER TABLE crew ADD COLUMN IF NOT EXISTS is_demo boolean NOT NULL DEFAULT false;

ALTER TABLE maintenance_records ADD COLUMN IF NOT EXISTS is_demo boolean NOT NULL DEFAULT false;

ALTER TABLE schedule_records ADD COLUMN IF NOT EXISTS aircraft_id integer REFERENCES aircraft(id) ON DELETE SET NULL;
ALTER TABLE schedule_records ADD COLUMN IF NOT EXISTS priority varchar(20) NOT NULL DEFAULT 'normal';
ALTER TABLE schedule_records ADD COLUMN IF NOT EXISTS assignee varchar(100);
ALTER TABLE schedule_records ADD COLUMN IF NOT EXISTS origin_icao varchar(4);
ALTER TABLE schedule_records ADD COLUMN IF NOT EXISTS destination_icao varchar(4);
ALTER TABLE schedule_records ADD COLUMN IF NOT EXISTS is_demo boolean NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS ix_schedule_records_aircraft_id ON schedule_records(aircraft_id);

ALTER TABLE alerts ADD COLUMN IF NOT EXISTS is_read boolean NOT NULL DEFAULT false;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS is_demo boolean NOT NULL DEFAULT false;
ALTER TABLE analysis_reports ADD COLUMN IF NOT EXISTS is_demo boolean NOT NULL DEFAULT false;
