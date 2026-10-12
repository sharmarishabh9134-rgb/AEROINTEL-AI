-- Preserve exact-ID constraints and also prevent case/whitespace variants
-- from creating duplicate aircraft or crew business identifiers.
create unique index if not exists uq_aircraft_resource_id_normalized
  on aircraft (lower(trim(resource_id)));

create unique index if not exists uq_crew_display_id_normalized
  on crew (lower(trim(display_id)));
