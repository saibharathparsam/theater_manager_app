# Setup and deployment

This guide covers the legacy theater-sales pipeline and its modernized Silver,
Gold, dashboard, app, and quarantine-recovery workflows. Deploying a bundle
registers resources; it does not by itself seed data, grant Unity Catalog access,
or publish a new app deployment.

## Deployment overview

1. Configure a workspace, CLI profile, SQL warehouse, and the required permissions.
2. Deploy the bundle and seed Bronze. Seeding **overwrites** source tables.
3. Build Silver and Gold; optionally build the legacy denormalized table.
4. Create the feedback and recovery ledgers, apply owner-run grants, and configure
   leadership and Operations allowlists.
5. Deploy and start the app, authorize user SQL access, and verify the dashboards
   and workflows.

The modernized Silver and Gold notebooks use fixed `theater_analytics` table
paths. Bundle `catalog` and `schema` variables apply to the seed and legacy
transform, not those notebook paths. The development workspace folder and job
run-as identity resolve to the authenticated deploying user. Before reusing an
existing deployment owned by someone else, confirm its bundle root and execution
identity rather than deploying a second copy over shared resources.
All example commands use `my-profile`; replace it with your authenticated profile.
The runtime `src/app/app.yaml` is the single source of app environment configuration.
Its approval/operator allowlists are empty by default, denying both actions.
There are no `leadership_approvers` or `quarantine_operators` bundle variables.
Configure an untracked runtime copy as described in section 5.

## Prerequisites

- Databricks CLI >= 1.0.0; see the [installation guide](https://docs.databricks.com/aws/en/dev-tools/cli/install).
- A Databricks workspace (Free Edition or higher)
- A CLI profile configured for the target workspace, e.g. `databricks auth login --host <your-workspace-host> --profile=my-profile`
- A SQL warehouse — the bundle looks up `Serverless Starter Warehouse` by default (present on fresh Free Edition workspaces); override with `--var="warehouse_id=<id>"` if it has been renamed or deleted
- Permissions to deploy resources and run jobs, plus an authorized owner to create
  tables and apply Unity Catalog grants. App viewers also need the permissions
  described in sections 5 and 6.
- Serverless compute with Unity Catalog row-filter support and real workspace
  users for multi-role verification. A single-user workspace cannot prove
  cross-user isolation; do not treat owner-only testing as a pass.

Run from the repository root in PowerShell. Confirm your profile deliberately:

```powershell
databricks --version
databricks auth profiles
databricks current-user me --profile my-profile
```

Keep the same explicit profile, target and any `--var` overrides on subsequent
validate/deploy/run commands. Do not copy tokens into source files.

## 1. Deploy the bundle

This registers four jobs, three dashboards, and the app resource, and uploads source
files to the configured workspace folder. The catalog and schema are created by
the seed job. Registration does not launch a new app deployment.

```powershell
databricks bundle validate --strict --target dev --profile my-profile
databricks bundle deploy -t dev --profile=my-profile
```

Override catalog/schema for the seed and legacy jobs if needed. These overrides
do not change the fixed Silver/Gold paths:

```powershell
databricks bundle deploy -t dev --profile=my-profile --var="catalog=my_catalog" --var="schema=my_schema"
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
attributes only: seeding does not install a row filter, and example identities must be
mapped to real workspace principals before enforcing access. The Gold app reads
as the signed-in user so an installed Gold row filter evaluates that viewer.

```powershell
databricks bundle run -t dev seed_theater_data --profile=my-profile --params="catalog=theater_analytics,schema=bronze"
```

This explicitly seeds the fixed Bronze path used by Silver and Gold, not the
legacy bundle defaults (`workspace.fde_theater`). Run it only once during initial
setup, or when intentionally regenerating Bronze. It replaces any manually
maintained manager assignments, so remap test identities before rebuilding.

Verify:

```sql
SHOW TABLES IN theater_analytics.bronze;
-- Expect: theater_layout, imdb_movie_metadata, pos_ticket_sales

SELECT COUNT(*) FROM theater_analytics.bronze.pos_ticket_sales;
-- Expect: roughly 150k–200k transactions
```

## 3. Build the denormalized table

This step is optional for the Gold app. The legacy dashboard's SQL parameters
default to `workspace.fde_theater`, independently of the fixed modernized paths.
To initialize that separate comparison, deliberately seed the legacy location
and run its inner-join transform:

```powershell
databricks bundle run -t dev seed_theater_data --profile=my-profile --params="catalog=workspace,schema=fde_theater"
databricks bundle run -t dev build_denormalized_theater_sales --profile=my-profile --params="catalog=workspace,schema=fde_theater"
```

Verify:

```sql
SELECT COUNT(*) FROM workspace.fde_theater.denormalized_theater_sales;
```

For a different legacy location, change its dashboard catalog/schema parameters
as well as the seed/transform parameters. Bundle variables do not rewrite those
dashboard SQL parameter defaults.

## 4. Verify the workspace

After initial deployment/seeding (and optional section 3), verify:

| Asset | Location | How to verify |
|-------|----------|---------------|
| 3 source tables | `theater_analytics.bronze` | `SHOW TABLES` returns `theater_layout`, `imdb_movie_metadata`, `pos_ticket_sales` |
| Optional legacy table | `workspace.fde_theater.denormalized_theater_sales` | `DESCRIBE` returns the joined columns |
| 4 jobs | Workflows | `seed_theater_data`, `build_silver_theater_data`, `build_gold_theater_data`, and `build_denormalized_theater_sales` |
| 3 Lakeview dashboards | `/Workspace/Users/<user name>/fde_interview/` | `Theater Performance`, `Theater Data Quality` (requires Silver), and `Theater Revenue Insights` (requires Gold) |
| 1 Databricks App | Compute > Apps > `reel-insights-app` | Resource exists; deployment follows section 5 |

Dashboards may fail to query until their Silver/Gold/feedback tables exist.
If `reel-insights-app` already belongs to another bundle/user in the workspace,
stop and coordinate ownership; do not overwrite another deployment.

## 5. Deploy and start the Reel Insights application

The Gradio application reads `theater_analytics.gold.theater_daily_sales`.
After seeding Bronze in section 2, run Silver and `build_gold_theater_data`. As table owner, use
the consolidated security notebook in section 6 to create
`theater_analytics.gold.decision_feedback` without replacing existing approvals.
The feedback table is not recreated/overwritten by the Gold pipeline.
The app's warehouse resource grants its service principal `CAN_USE`, but Unity
Catalog grants must be applied separately (section 6). Deploy and start only
after the tables and grants are ready. Unlike the legacy dashboard, the app
requires the fixed Bronze path, regardless of the bundle defaults:

```powershell
# Bronze must already exist from section 2; do not reseed for app-only edits.
databricks bundle run --target dev --profile my-profile build_silver_theater_data
databricks bundle run --target dev --profile my-profile build_gold_theater_data
databricks apps get reel-insights-app --profile my-profile --output json
```

Use the returned `service_principal_client_id` in section 6. Complete that
owner-run security setup and section 6a's data-access checks before sharing.
The bundle only registers the app and its warehouse/job resources; the runtime
manifest is not duplicated in `resources/apps.yml`.

Prepare a clean **untracked copy** of the flat `src/app` source directory:

```powershell
$appSource = Join-Path $env:TEMP ("reel-insights-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $appSource | Out-Null
Get-ChildItem .\src\app -File | Copy-Item -Destination $appSource
Write-Output $appSource
```

In the copied `app.yaml`, replace the empty values of the two existing entries
with actual authorized, comma-separated email addresses; do not append duplicate
entries. Leave all `valueFrom` resource bindings unchanged. An empty/missing
list denies the action. Do not commit the configured copy.

```yaml
  - name: LEADERSHIP_APPROVERS
    value: "<approver-email>"
  - name: QUARANTINE_OPERATORS
    value: "<operator-email>"
```

Keep those entries under `env:`. Upload and publish that exact copy (replace
`<workspace-user>` with `userName` from `current-user me`):

```powershell
databricks workspace import-dir "$appSource" "/Workspace/Users/<workspace-user>/reel-insights-configured" --overwrite --profile my-profile
databricks apps deploy reel-insights-app --source-code-path "/Workspace/Users/<workspace-user>/reel-insights-configured" --profile my-profile
```

Check each command succeeds before continuing. For later app-only edits, create
a fresh configured copy of the current source, then upload and deploy again.
If files were removed/renamed, upload to a fresh workspace source directory so
stale files cannot remain. Starting compute with
`databricks apps start reel-insights-app` does not publish changed source/config.
Confirm the active deployment succeeds with:

```powershell
databricks apps get reel-insights-app --profile my-profile --output json
```

The runtime manifest defaults to `theater_analytics.gold`.
The bundle app resource requests `user_api_scopes: [sql]`. After deploying,
reopen the app and authorize SQL access. Each viewer needs warehouse CAN_USE,
USE CATALOG on `theater_analytics`, USE SCHEMA on `theater_analytics.gold`,
and SELECT on `theater_daily_sales`. If a Gold row filter is installed, it must
also recognize their manager/zone-manager identity or leadership membership.
Do not add the app service principal to leadership to work around viewer RLS.
Missing user authorization produces an error, never shared-principal Gold reads.
`THEATER_FEEDBACK_TABLE` defaults to `decision_feedback`. `LEADERSHIP_APPROVERS`
contains the explicitly configured approval email(s), comma-separated.
Do not pass undeclared allowlist `--var` flags or rely on `bundle run` to retain
your private configuration: it republishes the repository's deny-by-default copy.
The runtime `app.yaml` uses `valueFrom` for resources.
Only authenticated viewers whose proxy-supplied email is allowlisted can approve;
others can read metrics. Missing identity disables approval. Do not expose this
Gradio server directly outside the authenticated Databricks Apps reverse proxy.

Additional commands for managing the lifecycle of your application can be found at https://docs.databricks.com/aws/en/dev-tools/cli/reference/apps-commands

## 6. Configure Gold row security and app access

The notebook is uploaded by bundle deploy under
`<workspace-root>/files/src/notebooks/configure_theater_security`.
Run it interactively on serverless compute as the Gold table owner (with Bronze/
Silver access and privilege to create functions/tables and grant their use).
Keep the sibling `theater_security.py` workspace file beside the notebook; it
shares the DDL/grants with the legacy creation/grant entry points.

Retrieve `service_principal_client_id` using the `apps get` command above (not
the numeric `service_principal_id`). After Gold sales and Silver quarantine
exist, open [configure_theater_security.py](src/notebooks/configure_theater_security.py)
as an authorized owner. Its default `mode=inspect` is read-only and reports
missing objects explicitly. Review the SQL and existing permissions, then set
`mode=apply` and `app_service_principal_client_id` to the retrieved client ID.
Apply creates missing leadership, feedback, and recovery tables; replaces the
sales row-filter function and attachment; and grants the app the Gold and Silver
privileges shown in the notebook. It neither inserts leadership users nor
revokes prior permissions. Existing membership is preserved.
The leadership lookup schema is `gold.leadership_users(user_email STRING NOT NULL)`.
If an existing lookup has a different schema, stop and migrate it explicitly;
the notebook does not overwrite or guess existing membership columns.
Optionally set `viewer_principal` to grant a specified user or group catalog and
Gold schema use plus sales SELECT; warehouse CAN_USE remains a separate grant.
Leave it blank to make no viewer grant. Review any existing row filter before
apply: replacing the function may affect every table that references it.

The function installs this policy: `theater_leadership` account-group
members and entries in `gold.leadership_users` can see all sales, while other
viewers see matching manager/zone-manager rows. Confirm the group exists and the
table owner can read the lookup table. Maintain membership separately after
approval; membership is not the same as the app's approval allowlist. Test leadership, manager, zone manager, and unrelated users with their own identities.
The sales filter does **not** apply to `gold.decision_feedback`; the app shares
that feedback with its viewers.

The Gold app grants are:

```sql
GRANT USE CATALOG ON CATALOG theater_analytics TO `<app-service-principal-id>`;
GRANT USE SCHEMA ON SCHEMA theater_analytics.gold TO `<app-service-principal-id>`;
GRANT SELECT ON TABLE theater_analytics.gold.theater_daily_sales TO `<app-service-principal-id>`;
GRANT SELECT, MODIFY ON TABLE theater_analytics.gold.decision_feedback TO `<app-service-principal-id>`;
```

`MODIFY` is limited to the feedback and recovery-request tables, not sales,
quarantine, or the entire schema.
The application inserts approval events via an insert-only MERGE; the
underlying `MODIFY` privilege is broader and must be restricted to trusted app
administrators. No grants are performed by app startup. The shared grant helpers do not grant schema-wide SELECT/MODIFY. Existing permissions are not
revoked automatically. The individual grant notebooks remain available and
require the client ID widget; the consolidated notebook covers both grant sets.

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

## 6a. Verify role-based access before sharing

### Prepare real test identities and a fixed snapshot

Use distinct workspace users for leadership, theater manager, zone manager and
an unrelated viewer. Grant them app `CAN_USE`, warehouse `CAN_USE`, and the
viewer grants via section 6's `viewer_principal` widget (repeat apply for each
user or grant a dedicated viewer group). Do not grant the unrelated viewer
leadership membership, direct Bronze/Silver access, or permission to edit policy
tables/functions. Keep approval/operator allowlists separate from sales visibility.

As an authorized owner, explicitly enroll the approved test leader, or arrange
membership in the `theater_leadership` **account** group with an administrator.
Do not enroll the app service principal. For the lookup-table option, replace
the placeholder with the approved real user's identity and execute:

```sql
MERGE INTO theater_analytics.gold.leadership_users t
USING (SELECT lower('<leader-email>') AS user_email) s
ON lower(trim(t.user_email)) = s.user_email
WHEN NOT MATCHED THEN INSERT (user_email) VALUES (s.user_email);
```

In the synthetic demo workspace only, map real manager identities into the
Bronze reference source, then rebuild Silver and Gold. Do not edit Gold directly
(the next refresh replaces it), and do not reseed after mapping:

```sql
UPDATE theater_analytics.bronze.theater_layout
SET manager_email = '<manager-email>'
WHERE theater_id = 'T001';

UPDATE theater_analytics.bronze.theater_layout
SET zone_manager_email = '<zone-manager-email>'
WHERE state = 'NY';
```

```powershell
databricks bundle run --target dev --profile my-profile build_silver_theater_data
databricks bundle run --target dev --profile my-profile build_gold_theater_data
```

Stop other writers during verification. As the explicitly enrolled leader,
capture the exact expected visible row counts and distinct theater IDs using
the full Gold snapshot, not the app's seven-day window:

```sql
SELECT current_user(), is_account_group_member('theater_leadership');
SELECT COUNT(*) AS all_rows,
       sort_array(collect_set(COALESCE(theater_id, '<NULL>'))) AS theater_ids
FROM theater_analytics.gold.theater_daily_sales;

SELECT theater_id, state, manager_email, zone_manager_email, COUNT(*) AS rows
FROM theater_analytics.gold.theater_daily_sales
GROUP BY theater_id, state, manager_email, zone_manager_email
ORDER BY state, theater_id;
```

Require a nonempty baseline with multiple theaters/regions. Sum only the groups
matching the manager/zone manager's real identity to calculate their expected
counts; include the union if a user holds both roles. Rows without a matching
layout are visible only to leadership. A null theater ID is represented as
`<NULL>` in verification. Never invent expected counts or accept zero for an
authorized role to make a test pass.

### Run the actual-viewer check

Each user opens [verify_theater_access.py](src/notebooks/verify_theater_access.py)
in their own session and runs it **interactively**, not through a job with the
owner's run-as identity. Set:

- `expected_user`: that user's real workspace login.
- `expected_theater_ids_json`: e.g. `["T001"]`, the NY theater list, the full
  leadership list, or `[]` for the unrelated user.
- `expected_row_count`: the exact owner-baseline count for that role, or `0`
  for the unrelated user.

The notebook checks `current_user()`, the installed filter and column binding,
and exact row count/theater set. Wrong identity, missing/misbound filter,
missing/extra rows, or permission errors fail explicitly. It does not impersonate
an email or evaluate the policy using caller-supplied identity strings.

| Test identity | Expected Gold access | Expected application behavior |
|---|---|---|
| Leader | All baseline rows/theaters, including unmatched groups | Can approve only if also explicitly allowlisted |
| Theater manager | Exact assigned theater rows | Cannot approve unless separately allowlisted; Operations hidden |
| Zone manager | Exact assigned-zone theater rows | No data from other zones; Operations hidden unless separately allowlisted |
| Unrelated viewer | Query succeeds with zero rows | No sales, approvals denied, Operations hidden |
| Operations user | Sales still follows their own role | Full quarantine queue; can submit/track a verified correction |

After publishing the configured app, repeat the matrix in separate signed-in
browser sessions and authorize SQL access. State/theater filters must never
expand visibility. Check decision feed visibility is intentionally global,
while Operations is not accessible to a non-operator. Confirm approval and
Operations callbacks deny unauthorized actions (local callback regression
tests cover this too). Repeat the SQL checks after a Gold refresh and a recovery
to prove policy attachment/grants survive the operational path.

For the Gold dashboard, publish using **viewer credentials**, not embedded owner
credentials, and test with the same users. Its feedback page reads the unfiltered
audit table: only grant feedback SELECT to explicitly approved audit viewers.
Users without that separate permission should not be given owner credentials to
make the page work. Keep the legacy Bronze and DQ dashboards owner/operator-only;
the Gold sales filter does not secure those datasets.

Record each actual identity, expected/observed count, policy metadata, timestamp
and app result in the interview evidence. A screenshot from the owner alone is
not proof. Local mocked tests are regression checks, not certification of live RLS.

## 7. Build Silver POS ticket sales (required for the Gold app)

After loading `theater_analytics.bronze.pos_ticket_sales`, open and run all cells
in `src/notebooks/build_silver_pos_ticket_sales.py` on Databricks Runtime 14.3 LTS
or later (or serverless). The runner needs access to Bronze and permission to
create/write the Silver schema and tables. The `build_silver_theater_data` job
runs this notebook alongside both Silver reference notebooks. The app
requires the separate Gold job before it can serve the daily-sales metrics.

Deploy the bundle, then run the Silver job after Bronze has been seeded into
`theater_analytics.bronze`:

```powershell
databricks bundle deploy --target dev --profile my-profile
databricks bundle run --target dev --profile my-profile build_silver_theater_data
```

The theater and movie reference tasks run in parallel on serverless compute.
The POS task runs after both references, then `build_silver_denormalized_theater_sales` left-joins
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

## 8. Build Silver reference datasets

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

## 9. Enable Operations quarantine recovery

This feature requires coordinated provisioning, Silver-job deployment, and app
deployment. Do not enable the Operations page before provisioning the ledger.
Without that ledger the Silver job logs that recovery is disabled and retains
normal ingestion behavior.

1. As owner, after creating the Silver/quarantine and Gold sales tables, use
   the consolidated security notebook in section 6 to create the request ledger
   and apply the app grants. It does **not** grant app MODIFY on Silver sales or
   quarantine. Review older broad grants separately.
2. Configure the runtime `QUARANTINE_OPERATORS` allowlist in the untracked app
   copy as described in section 5. Operator permission
   is distinct from leadership approval and grants access to the full queue.
3. Deploy the Silver job and app resources, then publish the configured app copy
   using section 5:

   ```powershell
   databricks bundle validate --strict --target dev --profile my-profile
   databricks bundle deploy --target dev --profile my-profile --select jobs.build_silver_theater_data --select apps.reel_insights_app
   ```

   The app resource binds `QUARANTINE_RECOVERY_JOB_ID` to the existing Silver job
   with `CAN_MANAGE_RUN`. It does not need additional user OAuth scopes for the
   job, because request reads/writes and job submission use the app identity.
   The job runs as the bundle execution identity, which needs Silver writes,
   reference reads, ledger writes, and Gold refresh permission.
4. Open Operations as an authorized user, inspect a version, enter a JSON patch,
   give a reason and confirm source verification, then submit. Submission means
   **queued**, not restored. Refresh Operations to see `REJECTED`, `APPLIED`, or
   `COMPLETED`. The response and history include the job run ID.

The updated `build_silver_pos_ticket_sales` task uses
[`process_quarantine_recovery.py`](src/notebooks/process_quarantine_recovery.py),
which first executes the existing POS notebook, then processes corrections with
its exact normalizer. Reference tasks finish before this task. All runs of the
Silver job are serialized/queued (`max_concurrent_runs: 1`); do not run standalone
writers concurrently. Combined Silver bypasses its ingestion-date window for
APPLIED hashes, so old quarantined data is not skipped and original ingestion
timestamps do not advance replay watermarks. Conditional downstream tasks refresh
Gold and verify Silver/combined Silver before marking the ledger complete.
Processing and downstream tasks retry failures twice before requiring intervention.
Without APPLIED recoveries, the job retains its normal Silver-only behavior.

Rejected requests preserve their error and leave their quarantine version open.
Correct and reselect to submit a new request. For infrastructure/downstream
failures, inspect job logs. Once the underlying issue is resolved, repair the
failed task and its dependent tasks in the original run, or rerun **the full
Silver job**:

```powershell
databricks bundle run --target dev --profile my-profile build_silver_theater_data
```

Retries recognize a previously inserted recovery by its deterministic request/data
hash. They do not overwrite an existing different Silver version. The original
quarantine row and request history are retained; historical quarantine dashboard
counts do not shrink. Gold is a full snapshot refresh after recovery, not an
incremental addition to already aggregated revenue. Avoid concurrent standalone
Gold runs. Provisioning notebooks and these deployment commands must be executed
explicitly; implementing the feature locally does not move production data.

## Local regression checks

Use Python 3.11 or 3.12 in an isolated environment. No Databricks credentials
are needed; SQL/SDK calls are mocked. These checks cover dashboard parsing,
workflow/resource bindings, owner provisioning, policy decisions and viewer
verification failures, not execution on live Spark/Unity Catalog:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

Before deploying, also run `databricks bundle validate --strict --target dev
--profile my-profile` against the deliberately selected workspace. A local
JSON/YAML parse is not a replacement for workspace validation.

## Troubleshooting

- **App cannot load Gold:** confirm user SQL authorization, warehouse access,
  Gold grants, and any configured row filter; see sections 5 and 6.
- **Operations cannot load quarantine:** verify ledger provisioning, app
  service-principal grants, and the configured operator allowlist in section 9.
- **Recovery remains APPLIED:** inspect the job run and repair or rerun the
  downstream work as described in section 9; do not mark it complete manually.
- **Other setup questions:** see [FAQ.md](FAQ.md).
