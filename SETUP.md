# Setup Instructions

This guide walks through deploying the FDE interview packet to a Databricks workspace and initializing the as-is theater sales pipeline and dashboard the candidate will work from.

## Prerequisites

- Databricks CLI >= 0.260.0 (`brew install databricks` or [docs](https://docs.databricks.com/aws/en/dev-tools/cli/install))
  - **HIGHLY RECOMMENDED** If you already have the Databrick CLI installed you should upgrade it.  [docs](https://docs.databricks.com/aws/en/dev-tools/cli/install#homebrew-update-for-linux-or-macos)
- A Databricks workspace (Free Edition or higher)
- A CLI profile configured for the target workspace, e.g. `databricks auth login --host <your-workspace-host> --profile=my-profile`
- A SQL warehouse — the bundle looks up `Serverless Starter Warehouse` by default (present on fresh Free Edition workspaces); override with `--var="warehouse_id=<id>"` if it has been renamed or deleted

## 1. Deploy the bundle

This registers four jobs, three dashboards, and the app resource, and uploads source
files to the configured workspace folder. The catalog and schema are created by
the seed job. Registration does not launch a new app deployment.

```bash
databricks bundle deploy -t dev --profile=my-profile
```

Override catalog/schema if needed:

```bash
databricks bundle deploy -t dev --profile=my-profile \
  --var="catalog=my_catalog" \
  --var="schema=my_schema"
```

## 2. Seed source tables

The `seed_theater_data` job runs three ingest tasks: `ingest_theater_layout` and `ingest_imdb_metadata` in parallel, followed by `ingest_pos_ticket_sales` (which reads the first two for FK-consistent transactions). All data is generated deterministically (`seed=42`).

Each ingest task also writes `ingestion_timestamp` (TIMESTAMP) and
`ingestion_date` (DATE), captured once at the start of that task in UTC.
Every row within a task run receives the same values; separate tasks can have
different timestamps. Generated business data remains deterministic, while
ingestion metadata changes on each run.

Theater layout includes deterministic synthetic `manager_email` and
`zone_manager_email` identities: `manager.t001@example.com` for theater `T001`,
and `zone_manager.ny@example.com` for all theaters in state `NY`. Every screen
in a theater shares its manager; state is the zone boundary. Assignments do not
consume random numbers, so existing seeded business values remain unchanged.
The denormalized transform carries both columns into its output.
Rerun the seed job and then the transform to populate them. These are RLS-ready
attributes only: no row filter is installed, and example identities must be
mapped to real workspace principals before enforcing access. The Gold app reads
as the signed-in user so an installed Gold row filter evaluates that viewer.

```bash
databricks bundle run -t dev seed_theater_data --profile=my-profile
```

Verify (adjust catalog/schema to match your vars):

```sql
SHOW TABLES IN <catalog>.<schema>;
-- Expect: theater_layout, imdb_movie_metadata, pos_ticket_sales

SELECT COUNT(*) FROM <catalog>.<schema>.pos_ticket_sales;
-- Expect: roughly 150k–200k transactions
```

## 3. Build the denormalized table

The `build_denormalized_theater_sales` job inner-joins the three source tables into the wide table that powers the dashboard. Run after `seed_theater_data`.

```bash
databricks bundle run -t dev build_denormalized_theater_sales --profile=my-profile
```

Verify (adjust catalog/schema to match your vars):

```sql
SELECT COUNT(*) FROM <catalog>.<schema>.denormalized_theater_sales;
```

## 4. Verify the workspace

After both jobs complete, the workspace should have:

| Asset | Location | How to verify |
|-------|----------|---------------|
| 3 source tables | `<catalog>.<schema>` | `SHOW TABLES` returns `theater_layout`, `imdb_movie_metadata`, `pos_ticket_sales` |
| 1 denormalized table | `<catalog>.<schema>.denormalized_theater_sales` | `DESCRIBE <catalog>.<schema>.denormalized_theater_sales` |
| 4 jobs | Workflows | `seed_theater_data`, `build_silver_theater_data`, `build_gold_theater_data`, and `build_denormalized_theater_sales` |
| 3 Lakeview dashboards | `/Workspace/Users/<user name>/fde_interview/` | `Theater Performance`, `Theater Data Quality` (requires Silver), and `Theater Revenue Insights` (requires Gold) |
| 1 Databricks App | `/Workspace/Users/<user name>/fde_interview/files/src/app` |

## 5. Deploy and start the provided application

The Gradio application reads `theater_analytics.gold.theater_daily_sales`.
Run Bronze, Silver, and `build_gold_theater_data` first. Run
`src/notebooks/create_decision_feedback.py` once as the table owner to create
`theater_analytics.gold.decision_feedback` without replacing existing approvals.
The feedback table is not recreated/overwritten by the Gold pipeline.
The app's warehouse resource grants its service principal `CAN_USE`, but Unity
Catalog grants must be applied separately (section 6). Deploy and start only
after the tables and grants are ready. Unlike the legacy dashboard, the app
requires seeding the fixed Bronze path, regardless of the bundle defaults:

```powershell
# Only seed on initial setup or intentional regeneration: overwrites Bronze.
databricks bundle run --target dev --profile my-profile seed_theater_data --params="catalog=theater_analytics,schema=bronze"
databricks bundle run --target dev --profile my-profile build_silver_theater_data
databricks bundle run --target dev --profile my-profile build_gold_theater_data

# After the owner-run feedback/grant notebooks (section 6), deploy only the app.
databricks bundle deploy --target dev --profile my-profile --select apps.reel_insights_app
databricks bundle run --target dev --profile my-profile reel_insights_app
```

For later app-only edits, run just the final two commands. Starting compute with
`databricks apps start reel-insights-app` does not publish changed source/config.
Use the bundle resource key `reel_insights_app` for `bundle run`, not the app
name `reel-insights-app`. Confirm the active deployment succeeds with:

```powershell
databricks apps get reel-insights-app --profile my-profile --output json
```

Both app configuration surfaces now default to `theater_analytics.gold`.
The bundle app resource requests `user_api_scopes: [sql]`. After deploying,
reopen the app and authorize SQL access. Each viewer needs warehouse CAN_USE,
USE CATALOG on `theater_analytics`, USE SCHEMA on `theater_analytics.gold`,
and SELECT on `theater_daily_sales`. Gold's existing row filter must also
recognize their manager/zone-manager identity or leadership membership.
Do not add the app service principal to leadership to work around viewer RLS.
Missing user authorization produces an error, never shared-principal Gold reads.
`THEATER_FEEDBACK_TABLE` defaults to `decision_feedback`. `LEADERSHIP_APPROVERS`
contains the explicitly configured approval email(s), comma-separated. Keep
`src/app/app.yaml` and `resources/apps.yml` aligned if changing these settings.
Only authenticated viewers whose proxy-supplied email is allowlisted can approve;
others can read metrics. Missing identity disables approval. Do not expose this
Gradio server directly outside the authenticated Databricks Apps reverse proxy.

Additional commands for managing the lifecycle of your application can be found at https://docs.databricks.com/aws/en/dev-tools/cli/reference/apps-commands

## 6. Grant the app access to theater tables

Retrieve `service_principal_client_id` using the `apps get` command above (not
the numeric `service_principal_id`). As an authorized owner, run
`src/notebooks/create_decision_feedback.py` first, then
`src/notebooks/grant_app_catalog_access.py`. Its `APP_SERVICE_PRINCIPAL` is
hardcoded to the current app client ID, `e44e0047-b327-4a29-bcf2-809a85ff2811`.
Update that constant if the app is recreated or moved to another workspace.
The grant notebook applies only:

```sql
GRANT USE CATALOG ON CATALOG theater_analytics TO `<app-service-principal-id>`;
GRANT USE SCHEMA ON SCHEMA theater_analytics.gold TO `<app-service-principal-id>`;
GRANT SELECT ON TABLE theater_analytics.gold.theater_daily_sales TO `<app-service-principal-id>`;
GRANT SELECT, MODIFY ON TABLE theater_analytics.gold.decision_feedback TO `<app-service-principal-id>`;
```

`MODIFY` is limited to the feedback table, not sales or the entire schema.
The application only inserts approval events via an insert-only MERGE; the
underlying `MODIFY` privilege is broader and must be restricted to trusted app
administrators. No grants are performed by app startup. The updated grant
notebook no longer grants schema-wide access or touches `workspace.fde_theater`.
Existing legacy permissions are not revoked automatically.

Gold SQL and approval evidence checks run as the signed-in user using the
proxy-forwarded token, preserving installed Gold row filters. Feedback SQL
runs as the app service principal; feedback history remains shared with app
viewers and does not inherit Gold sales RLS. Theater filters narrow visible
Gold data but do not grant additional access. The leadership allowlist protects
the approval callback server-side; editing a visible control cannot grant access.
Every approval persists movie/action, theater scope (empty ID = all theaters),
period, exact metric evidence, authenticated email, rationale, and timestamp.
The same selection can be retried without another event; refreshing creates a
new review/decision ID. Stale recommendations are rejected if period, evidence,
or top/bottom eligibility changes. Refresh history after approval to see it.
This workflow records decisions only; it does not schedule or remove screenings.

## 7. Build Silver POS ticket sales (required for the Gold app)

After loading `theater_analytics.bronze.pos_ticket_sales`, open and run all cells
in `src/notebooks/build_silver_pos_ticket_sales.py` on Databricks Runtime 14.3 LTS
or later (or serverless). The runner needs access to Bronze and permission to
create/write the Silver schema and tables. The `build_silver_theater_data` job
runs this notebook alongside both Silver reference notebooks. The app
requires the separate Gold job before it can serve the daily-sales metrics.

Deploy the bundle, then run the Silver job after Bronze has been seeded into
`theater_analytics.bronze`:

```bash
databricks bundle deploy --target dev --profile saibharathparsam@outlook.com
databricks bundle run --target dev --profile saibharathparsam@outlook.com build_silver_theater_data
```

The three independent notebook tasks run in parallel on serverless compute.
After all three succeed, `build_silver_denormalized_theater_sales` left-joins
their outputs and incrementally merges
`theater_analytics.silver.denormalized_theater_sales` for downstream Gold datasets.
Only one run of this job may execute at a time; avoid running its notebooks
manually while the job is active. The notebook table paths are fixed to
`theater_analytics.bronze` and `theater_analytics.silver`, independent of the
bundle's catalog/schema variables. The Silver job does not seed Bronze or run
the existing Bronze-based denormalized transform automatically.

The notebook creates `theater_analytics.silver.pos_ticket_sales`, normalizes ISO
and legacy timestamps (assuming UTC), derives `purchase_date`, and calculates
`revenue = seats_sold * ticket_price`. It deduplicates and merges on
`(transaction_id, pos_source)` and configures liquid clustering on `(transaction_id, ingestion_date)`.
Bronze `ingestion_timestamp` and `ingestion_date` are retained in Silver.
Each POS source replays from its latest Silver ingestion date, inclusive; new
sources and missing/empty Silver tables load their full history. Earlier late
records, merge-key changes, and source deletions require a separate backfill/CDC
strategy. Run only one writer at a time.

Deduplication keeps the latest ingestion timestamp with a deterministic hash
tie-breaker. Matched rows update only when the ingestion timestamp is newer and
the source hash differs. Existing tables without ingestion metadata or with
duplicate `(transaction_id, pos_source)` pairs must be explicitly migrated/rebuilt before running;
the notebook does not overwrite or automatically migrate them. Ingestion metadata
is assumed to be populated correctly by Bronze and is not explicitly validated.
Transaction IDs shared across different POS sources are separate transactions,
not duplicates. Earlier transaction-ID-only loads may have collapsed these rows.
A full Bronze replay/backfill is required to recover rows outside the incremental
window and recompute historical duplicate flags; no automatic migration occurs.

Missing showtimes and other column-level issues are retained and flagged `true`
with reasons, including legacy timestamp formats that were normalized.
Missing/blank transaction IDs or POS sources, missing/unparseable purchase
timestamps, and invalid ticket prices (null, NaN, infinite, zero, or negative)
are preserved in the insert-only
`theater_analytics.silver.pos_ticket_sales_quarantine` table rather than silently
dropped. Loads validate Silver merge-key uniqueness before and after loading.
The ingestion window is applied to raw Bronze before timestamp normalization and
hashing, for both Silver and quarantine. Rejected rows outside that window require
a deliberate backfill; quarantine no longer inspects all Bronze history. Required-value and
uniqueness checks share one aggregation per validation; transformations use
native Spark expressions, not Python UDFs. The two MERGEs are individually atomic,
not a multi-table transaction, so retry the notebook after a partial failure.
Same-timestamp source changes do not update an existing Silver row.
Price rejection applies only to incoming/replayed Bronze rows. Existing Silver
rows are not removed or migrated; an invalid-price version can remain in Silver
and also appear in quarantine after replay. Historical cleanup requires a separate
explicit migration.
Before writing Silver or quarantine, null non-key STRING fields (including raw
timestamp strings) become `Unknown`, after DQ classification, routing, hashing,
and deduplication. Null transaction IDs/POS sources, numeric/date/timestamp values,
and arrays remain unchanged. Empty strings are not replaced. DQ reasons and source
hashes still describe the original values. Existing rows are not backfilled;
the existing newer-timestamp/hash MERGE rules still apply.
The `has_data_quality_issue` column is BOOLEAN. Existing Silver and quarantine
tables with the old STRING flag require an explicit type migration/rebuild
before running this version; the notebook does not migrate existing schemas.

## 8. Build Silver reference datasets (optional)

After seeding `theater_analytics.bronze`, run `build_silver_theater_data` or run
these source notebooks manually:

- `src/notebooks/build_silver_imdb_metadata.py` writes
  `theater_analytics.silver.imdb_movie_metadata`, converting only `release_date`
  from ISO STRING to DATE. Malformed non-null dates fail the load.
- `src/notebooks/build_silver_theater_layout.py` writes
  `theater_analytics.silver.theater_layout` without type changes. Opening/closing
  hours stay as recurring `HH:MM` strings, not timestamps with an invented date.

Both replace their Silver Delta table from the complete Bronze snapshot, matching
the reference datasets' full-refresh ingestion. All other columns, including
manager emails and ingestion metadata, are preserved. They do not change the
existing denormalized transform or app to read Silver.
The runner needs Bronze read and Silver schema/table create and write permissions.

## Troubleshooting

See `FAQ.md`.
