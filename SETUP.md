# Setup Instructions

This guide walks through deploying the FDE interview packet to a Databricks workspace and initializing the as-is theater sales pipeline and dashboard the candidate will work from.

## Prerequisites

- Databricks CLI >= 0.260.0 (`brew install databricks` or [docs](https://docs.databricks.com/aws/en/dev-tools/cli/install))
  - **HIGHLY RECOMMENDED** If you already have the Databrick CLI installed you should upgrade it.  [docs](https://docs.databricks.com/aws/en/dev-tools/cli/install#homebrew-update-for-linux-or-macos)
- A Databricks workspace (Free Edition or higher)
- A CLI profile configured for the target workspace, e.g. `databricks auth login --host <your-workspace-host> --profile=my-profile`
- A SQL warehouse — the bundle looks up `Serverless Starter Warehouse` by default (present on fresh Free Edition workspaces); override with `--var="warehouse_id=<id>"` if it has been renamed or deleted

## 1. Deploy the bundle

This registers the two jobs, uploads the dashboard definition, and creates the workspace folder under `/Workspace/Users/<you>/fde_interview/`. The catalog and schema are created by the seed job.

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
mapped to real workspace principals before enforcing access. The app currently
queries as its service principal, not the signed-in end user.

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
| 3 jobs | Workflows | `seed_theater_data`, `build_silver_theater_data`, and `build_denormalized_theater_sales` |
| 1 Lakeview dashboard | `/Workspace/Users/<user name>/fde_interview/` | Listed as `Theater Performance` |
| 1 Databricks App | `/Workspace/Users/<user name>/fde_interview/files/src/app` |

## 5. Deploy and start the provided application

The Gradio application displays aggregated metrics from `denormalized_theater_sales`. First run `databricks bundle deploy` to apply the app's SQL warehouse resource, then ensure the source and denormalized tables have been built (steps 2–3). The app resource uses the bundle's configured warehouse and grants its service principal `CAN_USE` access to that warehouse. Deploy and start the app with:

```bash
databricks apps deploy -t dev -p my-profile
```

Deploy the bundle and run both data jobs first, then run `src/notebooks/grant_app_catalog_access.py` to grant the app service principal access to the Unity Catalog data.

The app defaults to the `workspace.fde_theater` catalog and schema. If you deploy the tables to a different catalog or schema, update `THEATER_CATALOG` and `THEATER_SCHEMA` in both `src/app/app.yaml` and `resources/apps.yml`.

Additional commands for managing the lifecycle of your application can be found at https://docs.databricks.com/aws/en/dev-tools/cli/reference/apps-commands

## 6. Grant the app access to theater tables

After deploying the app and creating the schema, open and run all cells in `src/notebooks/grant_app_catalog_access.py` from the Databricks workspace. The bundle syncs this source file under the bundle's workspace `files/src/notebooks/` directory.

The notebook grants the app service principal catalog/schema traversal and `SELECT`, `INSERT`, and `UPDATE` on the schema. These schema-level data privileges apply to existing and future tables. `INSERT` and `UPDATE` are fine-grained DML privileges currently documented as Beta; if they are unavailable in your workspace, the notebook's grant cell will fail. The notebook intentionally does not substitute `MODIFY`, because `MODIFY` also permits deletes.

Run it as a user authorized to grant privileges on the catalog and schema. The app service principal must exist; deploy the app first. Review the final `SHOW GRANTS` cells to verify the resulting grants.

## 7. Build Silver POS ticket sales (optional)

After loading `theater_analytics.bronze.pos_ticket_sales`, open and run all cells
in `src/notebooks/build_silver_pos_ticket_sales.py` on Databricks Runtime 14.3 LTS
or later (or serverless). The runner needs access to Bronze and permission to
create/write the Silver schema and tables. The `build_silver_theater_data` job
runs this notebook alongside both Silver reference notebooks. The app still
reads the existing denormalized table.

Deploy the bundle, then run the Silver job after Bronze has been seeded into
`theater_analytics.bronze`:

```bash
databricks bundle deploy --target dev --profile saibharathparsam@outlook.com
databricks bundle run --target dev --profile saibharathparsam@outlook.com build_silver_theater_data
```

The three independent notebook tasks run in parallel on serverless compute.
Only one run of this job may execute at a time; avoid running its notebooks
manually while the job is active. The notebook table paths are fixed to
`theater_analytics.bronze` and `theater_analytics.silver`, independent of the
bundle's catalog/schema variables. The Silver job does not seed Bronze or run
the denormalized transform automatically.

The notebook creates `theater_analytics.silver.pos_ticket_sales`, normalizes ISO
and legacy timestamps (assuming UTC), derives `purchase_date`, and calculates
`revenue = seats_sold * ticket_price`. It deduplicates and merges on
`transaction_id` and configures liquid clustering on `(transaction_id, ingestion_date)`.
Bronze `ingestion_timestamp` and `ingestion_date` are retained in Silver.
Each POS source replays from its latest Silver ingestion date, inclusive; new
sources and missing/empty Silver tables load their full history. Earlier late
records, merge-key changes, and source deletions require a separate backfill/CDC
strategy. Run only one writer at a time.

Deduplication keeps the latest ingestion timestamp with a deterministic hash
tie-breaker. Matched rows update only when the ingestion timestamp is newer and
the source hash differs. Existing tables without ingestion metadata or with
duplicate transaction IDs must be explicitly migrated/rebuilt before running;
the notebook does not overwrite or automatically migrate them. Ingestion metadata
is assumed to be populated correctly by Bronze and is not explicitly validated.

Missing showtimes and other column-level issues are retained and flagged `true`
with reasons, including legacy timestamp formats that were normalized.
Invalid merge keys or missing POS sources are preserved in the insert-only
`theater_analytics.silver.pos_ticket_sales_quarantine` table rather than silently
dropped. Loads validate Silver merge-key uniqueness before and after loading.
The ingestion window is applied to raw Bronze before timestamp normalization and
hashing, for both Silver and quarantine. Rejected rows outside that window require
a deliberate backfill; quarantine no longer inspects all Bronze history. Required-value and
uniqueness checks share one aggregation per validation; transformations use
native Spark expressions, not Python UDFs. The two MERGEs are individually atomic,
not a multi-table transaction, so retry the notebook after a partial failure.
Same-timestamp source changes do not update an existing Silver row.
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
