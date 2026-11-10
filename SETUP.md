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
| 2 jobs | Workflows | Listed as `seed_theater_data` and `build_denormalized_theater_sales` |
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

## Troubleshooting

See `FAQ.md`.
