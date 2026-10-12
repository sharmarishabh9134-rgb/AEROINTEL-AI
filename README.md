# AEROINTEL AI

The existing responsive dashboard is served by a FastAPI backend. Read-only dashboard data is fictional and marked as demo data. The default backend stores records in a local SQLite file so the app works without external accounts. Configure `DATABASE_URL` with a Supabase PostgreSQL connection string to persist records there.

The current frontend is a single `index.html` served by the same FastAPI service. Its shared API client reads `window.AEROINTEL_CONFIG.apiBaseUrl`; `/api/client-config.js` supplies the server-configured `PUBLIC_API_BASE_URL`. Leave it blank for same-origin hosting. If you deploy `index.html` on a separate static host, provide `window.AEROINTEL_CONFIG = { apiBaseUrl: "https://your-api-host" }` in that host's deployment configuration before the dashboard's API client starts, and set `FRONTEND_URL` on the API to the static site's origin. Keep credentials out of this browser configuration.

## Run locally

1. Use Python 3.11 or newer.
2. Create and activate a virtual environment, then install dependencies:

   ```powershell
   py -3.11 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   Copy-Item .env.example .env
   ```

3. Keep `DEMO_MODE=true` for the fictional sample dataset. Do not put real secrets in source control. Set `JWT_SECRET_KEY` to a random value of at least 32 characters before deploying.
4. Start the dashboard and API:

   ```powershell
   uvicorn app.main:app --reload
   ```

5. Open <http://127.0.0.1:8000>. API documentation is at <http://127.0.0.1:8000/docs> in development. Health is at `/api/health`.

The demo seed inserts into empty tables only; it never deletes or replaces rows. To initialize/seed explicitly, run `python -m scripts.seed_demo` after setting `.env`.

## Public Indian military aviation inventory estimates

The dashboard's **Public Fleet Estimates** view uses the supplied October 10, 2026 open-source snapshot in `data/india_military_aircraft_2026_10_10.json`. It is an imported reference dataset, not a live feed or official readiness report. It stays separate from the existing fictional `aircraft` table. The eight MQ-9B SkyGuardian aircraft on order are recorded as procurement and excluded from active inventory totals. The API exposes read-only public records at `/api/public-inventory` with optional `organization`, `category`, and `model` filters.

For local SQLite, the table is created by the app and import can be repeated safely:

```powershell
python -m scripts.import_public_inventory
```

For PostgreSQL/Supabase, inspect the schema and apply reviewed additive migration [`migrations/003_public_inventory_estimates.sql`](migrations/003_public_inventory_estimates.sql) first, grant the backend role select/insert/update on `public_inventory_records` plus sequence access, and add the dedicated-role RLS policy. Then run the same importer. It upserts by the stable model/service/dataset-version key; it does not delete or replace records. The importer requires the intended database configured in `DATABASE_URL` and never creates application tables in PostgreSQL automatically.

The supplied row list sums to 1,699 Air Force aircraft versus the approximate headline of 1,700, 367 Army Aviation aircraft, and 287 Navy aircraft. Across services the rows sum to 2,353 versus the approximate headline of 2,350. These differences are reported by the API/UI rather than silently changing the source rows. The last two differences are ordinary rounding/approximation at this precision. Inventory estimates do not establish serviceability, availability, maintenance, crew, or live aircraft location; those values are unavailable in the supplied source and are not inferred from inventory.

The referenced GlobalMilitary.net page may publish later snapshots and, when checked on October 10, 2026, its headline described 2,353 aircraft as the Indian Air Force inventory. That conflicts materially with the supplied PDF's 1,700 approximate IAF figure and supplied per-service model rows. The imported version intentionally preserves the supplied dated values, and the inventory screen surfaces this source-scope conflict. The reference currently behaves as a website page; no permitted public API was documented in the provided material. Do not scrape or automatically refresh it: the site's terms prohibit systematic downloads without permission. Update the versioned dataset only after reviewing a new source snapshot and its scope.

## Supabase PostgreSQL

1. Set `DATABASE_URL` locally to the PostgreSQL connection URI from the Supabase **Connect** panel. This is the database URI, not the Supabase project URL or a publishable/secret API key. The app accepts `postgresql://`, `postgres://`, and `postgresql+psycopg://` forms and uses Psycopg 3. URL-encode reserved characters in credentials. Keep the URI in the ignored local `.env` file or environment, never in source control.
2. Before changing an existing database, run the read-only inspector:

   ```powershell
   python -m scripts.inspect_database
   ```

   It reports tables, columns, types, nullability, primary/foreign/unique keys, indexes, row counts (without reading row contents), model drift, and PostgreSQL RLS policies. It never creates, updates, or deletes data. Review this output against the application models before applying any schema change. The existing operational tables may have columns different from this application's model, so do not run the fresh-database migration against the existing project without that review.
3. For a **completely fresh, empty database only**, apply these reviewed SQL files in order: [`migrations/001_initial_schema.sql`](migrations/001_initial_schema.sql), [`migrations/003_public_inventory_estimates.sql`](migrations/003_public_inventory_estimates.sql), [`migrations/004_weather_provenance.sql`](migrations/004_weather_provenance.sql), then [`migrations/005_normalized_identifier_uniqueness.sql`](migrations/005_normalized_identifier_uniqueness.sql). Migration `001` already creates `profiles`, `audit_logs`, and `analysis_reports.generated_by`, so `002` is not needed for a fresh database. `003` creates the tenth app table. Before `005`, verify there are no duplicate aircraft or crew IDs after trimming and case folding.
4. For an **existing database**, never run `001`. Inspect it first and compare the actual schema with the models. Apply only reviewed missing pieces: use `002` only for absent auth/audit support or the nullable report ownership column; `003` only if the inventory table is absent; `004` only if weather provenance columns/index are missing and after reviewing its `data_type` backfill and duplicate weather keys; and `005` only after checking normalized aircraft/crew IDs for duplicates. These files do not provide a migration ledger, and `CREATE TABLE IF NOT EXISTS` does not repair a pre-existing table with a different layout. Do not delete records to make a migration succeed.
5. Create a dedicated Postgres login for the API with `NOBYPASSRLS` (do not use the Supabase `service_role` key or database owner for routine API traffic). In a privileged SQL session, create the role and grant only the needed access:

   ```sql
   create role aerointel_backend login nobypassrls;
   grant connect on database postgres to aerointel_backend;
   grant usage on schema public to aerointel_backend;
   grant select, insert, update, delete on profiles, aircraft, crew, maintenance_records,
     weather_observations, schedule_records, alerts, analysis_reports, audit_logs, public_inventory_records to aerointel_backend;
   grant usage, select on all sequences in schema public to aerointel_backend;
   ```

   Set its password through the Supabase dashboard/secure shell, not in this repository. Use your actual database name if it differs from `postgres`.
6. In SQL Editor, create an RLS policy for that trusted API role on each table. Example after creating the role `aerointel_backend`:

   ```sql
   create policy api_profiles on profiles to aerointel_backend using (true) with check (true);
   create policy api_aircraft on aircraft to aerointel_backend using (true) with check (true);
   create policy api_crew on crew to aerointel_backend using (true) with check (true);
   create policy api_maintenance on maintenance_records to aerointel_backend using (true) with check (true);
   create policy api_weather on weather_observations to aerointel_backend using (true) with check (true);
   create policy api_schedules on schedule_records to aerointel_backend using (true) with check (true);
   create policy api_alerts on alerts to aerointel_backend using (true) with check (true);
   create policy api_reports on analysis_reports to aerointel_backend using (true) with check (true);
   create policy api_audit on audit_logs to aerointel_backend using (true) with check (true);
   create policy api_public_inventory on public_inventory_records to aerointel_backend using (true) with check (true);
   ```

   These policies authorize only the trusted backend database login. The API applies user roles and authorization before writes. Never expose the database URL or any Supabase key to the browser.
7. The dedicated login's PostgreSQL URL is the `DATABASE_URL` used by the backend; SQLAlchemy selects the psycopg 3 driver. Optional `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, and `SUPABASE_SECRET_KEY` are placeholders for future Supabase Auth/client use; the backend database connection uses `DATABASE_URL`.
8. Do not run the demo seed script against production. It is only for a local fictional dataset.

Production startup requires `APP_ENV=production`, `DEMO_MODE=false`, a strong `JWT_SECRET_KEY`, PostgreSQL, and all ten tables used by the app. Production startup does not run `create_all` or seed data; it checks required tables, columns, and the weather/normalized-ID uniqueness indexes and exits with a clear error when setup is incomplete. The inspector also checks model index names; `001` currently names the schedule composite index `ix_schedule_starts_status` while the model expects `ix_schedule_records_starts_status`, so review this reported naming drift rather than assuming the indexes match by name. Apply only reviewed migrations before deployment.

After the schema matches and the runtime role has the required grants and RLS policies, run `python -m scripts.test_postgres_connection`. It verifies required tables/columns and runs a `SELECT 1`, then inserts and reads a marker only in a PostgreSQL temporary table inside a transaction that it rolls back. It refuses to run on SQLite or in demo mode and does not touch application rows. Restart the API separately and verify `/api/health` plus an authenticated dashboard request to check persistence end to end.

## Environment settings

See `.env.example`. `GEMINI_API_KEY` is server-only. `GEMINI_MODEL` selects the model. Without a key or if Gemini fails, analysis uses an explicitly labeled rule-based fallback. `OPEN_METEO_LATITUDE`, `OPEN_METEO_LONGITUDE`, and `OPEN_METEO_LOCATION` set the configurable weather location. Weather requests use Open-Meteo with timeout and in-memory caching. A provider outage shows saved Open-Meteo records as explicitly stale when available, or an unavailable state; it never substitutes demo readings. The default graph filters to `source=open-meteo`; demo observations are available only through the explicit `mode=demo` graph API. Each graph record includes its source, observation timestamp, data type, location coordinates, and available fetch/creation timestamps. Forecast values are not physical sensor or aircraft measurements.

The shared API client connects the dashboard's summary, aircraft, crew, maintenance, schedule, alerts, activity, analytics, weather, AI analysis, authentication, and report screens to the `/api` routes. Write operations are persisted through the configured SQLAlchemy database. Role checks remain enforced by the API. Schedule creation and updates reject overlapping reservations with HTTP 409 and return conflict details to the dashboard.

The AI Decision Analyzer routes greetings, thanks, empty input, and unrelated questions before loading the dashboard snapshot or calling Gemini. Relevant questions use a fresh backend-generated snapshot; client-provided metrics are ignored. Responses separate calculated fields and timestamps from optional interpretation and recommendations. Each response labels its source (`Gemini AI`, `Rule-based fallback`, or `Intent handler`) and reports whether Gemini was configured and actually used; no confidence score is generated.

## Dashboard metric definitions

Dashboard metrics are calculated from stored administrative records. Local seeded aircraft and crew are fictional demonstration records and do not represent verified real-world readiness.

- Aircraft readiness confirmation = records whose `readiness_confirmation` is true / unique aircraft records with an explicit boolean confirmation. The summary keeps this separate from availability.
- Aircraft availability = unique aircraft with `availability_status=available` / unique aircraft with one of the recognized availability statuses (`available`, `maintenance`, `standby`, `unavailable`).
- Aircraft status shares = count in each recognized availability state / that same recognized-status denominator. “Not standby” is a status complement (available + maintenance + unavailable), not resource utilization.
- Crew readiness = unique crew with `readiness_status=ready` / unique crew with a recognized readiness status (`ready`, `review`, `unavailable`).
- Resource utilization is unavailable because the database does not store flight hours, sortie counts, or another usage measure; it is not inferred from standby status.
- Open maintenance counts `open`, `in_progress`, and `overdue` maintenance records. Schedule activity counts `scheduled` and `in_progress` schedule records. Open alerts count alerts whose status is `open`; severity totals use those same open alerts.

Aircraft and crew duplicates are normalized by trimmed, case-insensitive business ID and the latest `updated_at` / `confirmation_at` row is counted. Missing IDs are counted separately as individual records. Unknown or missing statuses do not enter status percentage denominators and are surfaced in data-quality counts. Percentages round to the nearest whole percent, with exact halves rounded up. Empty denominators return unavailable (`null`), not zero.

SQLite creates required tables and these indexes on startup. Before PostgreSQL deployment, inspect duplicates and apply the reviewed SQL migrations in order, including `004_weather_provenance.sql` and `005_normalized_identifier_uniqueness.sql`. This project currently has no migration ledger, so schema checks cannot prove which historical SQL files were applied; verify required columns and indexes directly. Never delete duplicate or legacy audit rows automatically to make a unique index apply.

Self-registration creates viewer accounts only. Assign `admin` or `analyst` to a profile through a trusted administrator-controlled database action; never accept a role from the registration request. Access tokens are short lived. Logout is stateless: the client discards its bearer token. Operational database reads require authentication whenever demo mode is off; they remain public only for the explicitly fictional local demo. Elevated writes require analyst/admin. Reports are visible to their creator; admins can review all reports.

Sensitive-route throttling is in-process and protects against simple repeated attempts. For a multi-instance production deployment, also apply edge/API rate limits at the hosting layer.

## Tests

```powershell
pytest -q
```

The suite uses an isolated temporary SQLite database for API, auth/role, source separation, provenance, and provider failure behavior checks. To test Open-Meteo or Gemini against the real services, configure the corresponding environment secret and exercise `/api/weather/forecast` or log in and call `/api/ai/analyze` from Swagger; external API calls are not required by the local test suite.

## Render deployment

- Build/install command: `pip install -r requirements.txt`
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Set `APP_ENV=production`, `DEMO_MODE=false`, `DATABASE_URL`, `JWT_SECRET_KEY`, and `FRONTEND_URL` in Render's environment settings. Set Gemini variables if needed. Never commit `.env` or credentials.
- The same service serves `index.html` and `/api`, so the dashboard and API are same-origin. `FRONTEND_URL` adds the configured CORS origin for a separately hosted frontend.
- Configure Render health check path as `/api/health`. Free plans may sleep when idle and can have cold starts or resource limits.

## Vercel deployment

The app is a single FastAPI service, not Flask: `app/main.py` exports the `app` instance and serves both the dashboard and `/api` routes. Vercel's Python runtime supports FastAPI and recognizes this entry point. There is no separate frontend build command or output directory. `vercel.json` explicitly bundles the root `index.html` and versioned `data/**/*.json` files because the app reads them at runtime. Vercel's Python runtime is currently documented as beta.

Vercel Functions do not provide persistent local storage. Production must use PostgreSQL; the default `aerointel_demo.db` SQLite database is for local development only. Before deployment, configure a persistent PostgreSQL database, review its current schema with `python -m scripts.inspect_database`, and apply only the migrations required for that schema. Production startup checks tables, columns, and indexes and does not run migrations or seed demo data. Follow the Supabase PostgreSQL procedure above if Supabase is the chosen database; no Supabase connection is currently configured in this project folder.

From this project directory, install and link the Vercel CLI, then configure environment variables in the Vercel project settings before the first deployment:

```powershell
npm install --global vercel
vercel login
vercel link
```

Set `APP_ENV=production`, `DEMO_MODE=false`, `DATABASE_URL` to the persistent PostgreSQL connection string, and `JWT_SECRET_KEY` to a random secret of at least 32 characters. Use a connection URL and database role appropriate for application traffic; do not use a Supabase service-role key as the database password. `FRONTEND_URL` is only needed when the dashboard is hosted on a different origin; the bundled dashboard and API are same-origin. Set `GEMINI_API_KEY` only if Gemini model responses are required. Configure the Open-Meteo coordinates and location for the intended region; no Open-Meteo API key is required. Never add secrets to browser configuration or commit them.

Use the linked project to create and inspect a preview deployment, then deploy to production only after checking `/`, `/api/health`, registration/login, protected APIs, reports, the public inventory screen, and live weather. The CLI commands are `vercel` for a preview and `vercel --prod` for production. This local mirror has no `.git` directory, so CLI upload from this folder is the available deployment route unless it is first placed in a Git repository. Do not run `vercel --prod` until the database and environment variables are ready and the preview checks pass.

## Credential rotation

If a credential was previously exposed, revoke/rotate it in its provider dashboard, update the deployment environment, and remove it from Git history as appropriate. This repository contains placeholders only.
