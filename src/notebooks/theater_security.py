"""Shared owner provisioning SQL and read-only Gold policy verification."""

from uuid import UUID

SALES_TABLE = "theater_analytics.gold.theater_daily_sales"
LEADERSHIP_TABLE = "theater_analytics.gold.leadership_users"
FEEDBACK_TABLE = "theater_analytics.gold.decision_feedback"
QUARANTINE_TABLE = "theater_analytics.silver.pos_ticket_sales_quarantine"
REQUEST_TABLE = "theater_analytics.silver.quarantine_recovery_requests"

LEADERSHIP_DDL = f"""
CREATE TABLE IF NOT EXISTS {LEADERSHIP_TABLE} (
    user_email STRING NOT NULL
) USING DELTA
"""
FEEDBACK_DDL = f"""
CREATE TABLE IF NOT EXISTS {FEEDBACK_TABLE} (
    decision_id STRING NOT NULL,
    movie_id STRING NOT NULL,
    movie_title STRING NOT NULL,
    action STRING NOT NULL,
    theater_id STRING NOT NULL,
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    approver_email STRING NOT NULL,
    approved_at TIMESTAMP NOT NULL,
    approval_note STRING NOT NULL,
    source_table STRING NOT NULL,
    evidence_json STRING NOT NULL
) USING DELTA
"""
RECOVERY_DDL = f"""
CREATE TABLE IF NOT EXISTS {REQUEST_TABLE} (
    request_id STRING NOT NULL,
    quarantine_hash STRING NOT NULL,
    corrections_json STRING NOT NULL,
    submitted_by STRING NOT NULL,
    submitted_at TIMESTAMP NOT NULL,
    reason STRING NOT NULL,
    status STRING NOT NULL,
    original_record_json STRING,
    last_error STRING,
    processed_at TIMESTAMP,
    restored_transaction_id STRING,
    restored_pos_source STRING,
    restored_source_row_hash STRING,
    completed_at TIMESTAMP,
    job_run_id BIGINT
) USING DELTA
COMMENT 'Audited quarantine corrections: SUBMITTED -> APPLIED -> COMPLETED, or REJECTED'
"""
ROW_FILTER_BODY = f"""
    COALESCE(is_account_group_member('theater_leadership'), FALSE)
    OR EXISTS (
        SELECT 1 FROM {LEADERSHIP_TABLE} l
        WHERE lower(trim(l.user_email)) = lower(current_user())
    )
    OR COALESCE(lower(trim(manager_email)) = lower(current_user()), FALSE)
    OR COALESCE(lower(trim(zone_manager_email)) = lower(current_user()), FALSE)
"""
ROW_FILTER_DDL = f"""
CREATE OR REPLACE FUNCTION theater_analytics.gold.sales_row_filter(
    manager_email STRING, zone_manager_email STRING
) RETURNS BOOLEAN
RETURN {ROW_FILTER_BODY}
"""
ATTACH_FILTER_SQL = f"""
ALTER TABLE {SALES_TABLE}
SET ROW FILTER theater_analytics.gold.sales_row_filter ON (manager_email, zone_manager_email)
"""
ROW_FILTER_QUERY = """
SELECT filter_catalog, filter_schema, filter_name, filter_col_usage
FROM theater_analytics.information_schema.row_filters
WHERE catalog_name = 'theater_analytics'
  AND schema_name = 'gold' AND table_name = 'theater_daily_sales'
"""


def quote_principal(value: str) -> str:
    value = value.strip()
    if not value or any(ord(char) < 32 for char in value):
        raise ValueError("A nonempty principal without control characters is required.")
    return f"`{value.replace('`', '``')}`"


def app_principal(client_id: str) -> str:
    if not client_id.strip():
        raise ValueError("Set app_service_principal_client_id to the app's client UUID.")
    return str(UUID(client_id.strip()))


def gold_grants(principal: str) -> list[str]:
    principal = quote_principal(principal)
    return [
        f"GRANT USE CATALOG ON CATALOG theater_analytics TO {principal}",
        f"GRANT USE SCHEMA ON SCHEMA theater_analytics.gold TO {principal}",
        f"GRANT SELECT ON TABLE {SALES_TABLE} TO {principal}",
        f"GRANT SELECT, MODIFY ON TABLE {FEEDBACK_TABLE} TO {principal}",
    ]


def quarantine_grants(principal: str) -> list[str]:
    principal = quote_principal(principal)
    return [
        f"GRANT USE CATALOG ON CATALOG theater_analytics TO {principal}",
        f"GRANT USE SCHEMA ON SCHEMA theater_analytics.silver TO {principal}",
        f"GRANT SELECT ON TABLE {QUARANTINE_TABLE} TO {principal}",
        f"GRANT SELECT, MODIFY ON TABLE {REQUEST_TABLE} TO {principal}",
    ]


def viewer_grants(principal: str) -> list[str]:
    principal = quote_principal(principal)
    return [
        f"GRANT USE CATALOG ON CATALOG theater_analytics TO {principal}",
        f"GRANT USE SCHEMA ON SCHEMA theater_analytics.gold TO {principal}",
        f"GRANT SELECT ON TABLE {SALES_TABLE} TO {principal}",
    ]


def verify_row_filter(spark) -> None:
    rows = spark.sql(ROW_FILTER_QUERY).collect()
    if len(rows) != 1:
        raise RuntimeError("Expected one Gold sales row filter. Apply owner security setup.")
    policy = rows[0]
    columns = policy.filter_col_usage.replace("`", "").replace(" ", "").casefold()
    if (
        (policy.filter_catalog, policy.filter_schema, policy.filter_name)
        != ("theater_analytics", "gold", "sales_row_filter")
        or columns != "manager_email,zone_manager_email"
    ):
        raise RuntimeError("Gold sales has an unexpected filter or column binding; owner review required.")


def configure_security(spark, mode="inspect", client_id="", viewer_principal="") -> None:
    if mode not in {"inspect", "apply"}:
        raise ValueError("mode must be inspect or apply.")
    principal = app_principal(client_id) if client_id.strip() or mode == "apply" else "<app-client-uuid>"
    statements = [
        LEADERSHIP_DDL, FEEDBACK_DDL, RECOVERY_DDL, ROW_FILTER_DDL, ATTACH_FILTER_SQL,
        *gold_grants(principal), *quarantine_grants(principal),
    ]
    if viewer_principal.strip():
        statements.extend(viewer_grants(viewer_principal))
    tables = [SALES_TABLE, QUARANTINE_TABLE, LEADERSHIP_TABLE, FEEDBACK_TABLE, REQUEST_TABLE]
    existing = {table: spark.catalog.tableExists(table) for table in tables}
    for table, exists in existing.items():
        print(f"{'EXISTS' if exists else 'MISSING'}: {table}")
    print("Proposed SQL (membership and existing ledger records are not replaced):")
    for statement in statements:
        print(statement.strip() + ";")
    if mode == "apply":
        missing = [table for table in (SALES_TABLE, QUARANTINE_TABLE) if not existing[table]]
        if missing:
            raise RuntimeError(f"Build Silver and Gold before security apply: {missing}")
        for statement in statements:
            spark.sql(statement)
        verify_row_filter(spark)
        print("Security applied. Verify real viewer identities before sharing the app.")
    for table in tables:
        if existing[table] or mode == "apply":
            spark.sql(f"SHOW GRANTS ON TABLE {table}").show(truncate=False)
    if existing[SALES_TABLE] or mode == "apply":
        spark.sql(ROW_FILTER_QUERY).show(truncate=False)
