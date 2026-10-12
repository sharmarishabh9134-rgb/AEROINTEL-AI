-- Additive weather provenance migration. Apply before starting a configured
-- PostgreSQL database after upgrading the application. Existing rows stay put;
-- legacy fetch timestamps remain NULL when they were not recorded.
alter table weather_observations
  add column if not exists data_type varchar(20),
  add column if not exists latitude double precision,
  add column if not exists longitude double precision,
  add column if not exists fetched_at timestamptz,
  add column if not exists created_at timestamptz;

update weather_observations
set data_type = case
  when source = 'demo' then 'demo'
  when source = 'open-meteo' and observed_at > now() then 'forecast'
  when source = 'open-meteo' then 'historical'
  else 'unknown'
end
where data_type is null;

-- Do not invent fetch/creation timestamps for legacy data. Historical values
-- without recorded coordinates keep NULL coordinates until verified.

-- Prevent concurrent provider fetches from storing the same provider point.
-- Existing rows are preserved; review any pre-existing duplicate tuples before
-- applying this constraint because migration must not delete audit history.
create unique index if not exists uq_weather_source_location_observed_at
  on weather_observations (source, latitude, longitude, observed_at);
