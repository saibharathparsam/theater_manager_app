"""Operator-authorized correction requests; the app never writes Silver sales."""

import json
import math
import os
from uuid import UUID

import sales

QUARANTINE_TABLE = "theater_analytics.silver.pos_ticket_sales_quarantine"
REQUEST_TABLE = "theater_analytics.silver.quarantine_recovery_requests"
EDITABLE_FIELDS = (
    "transaction_id", "pos_source", "purchase_timestamp", "ticket_price",
    "theater_id", "screen_id", "movie_id", "showtime", "seats_sold",
    "format", "ticket_type", "payment_method",
)
OPERATORS = {
    email.strip().casefold()
    for email in os.environ.get("QUARANTINE_OPERATORS", "").split(",")
    if email.strip()
}
OPEN_SQL = f"""
SELECT q.* FROM {QUARANTINE_TABLE} q
WHERE NOT EXISTS (
    SELECT 1 FROM {REQUEST_TABLE} r
    WHERE r.quarantine_hash = q.source_row_hash
      AND r.status IN ('SUBMITTED', 'APPLIED', 'COMPLETED')
)
  AND (:pos_source = '' OR q.pos_source = :pos_source)
  AND (:issue = '' OR array_contains(q.data_quality_issues, :issue))
ORDER BY q.ingestion_timestamp DESC, q.source_row_hash
LIMIT 100
"""


def require_operator(email: str) -> str:
    email = email.strip().casefold()
    if not email or email not in OPERATORS:
        raise PermissionError("Quarantine access is restricted to configured Operations users.")
    return email


def validate_patch(value: dict) -> dict:
    if not isinstance(value, dict) or not value:
        raise ValueError("Corrections must be a nonempty JSON object.")
    if set(value) - set(EDITABLE_FIELDS):
        raise ValueError("Only the displayed sales fields may be corrected; ingestion/audit fields are immutable.")
    result = {}
    for key, item in value.items():
        if key == "ticket_price":
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise ValueError("ticket_price must be a positive finite number.")
            try:
                item = float(item)
            except OverflowError as error:
                raise ValueError("ticket_price exceeds the supported numeric range.") from error
            if not math.isfinite(item) or item <= 0:
                raise ValueError("ticket_price must be a positive finite number.")
        elif key == "seats_sold":
            if isinstance(item, bool) or not isinstance(item, int) or not 0 < item <= 2147483647:
                raise ValueError("seats_sold must be a positive 32-bit integer.")
        elif not isinstance(item, str) or not item.strip() or len(item) > 500:
            raise ValueError(f"{key} must be a nonempty string of at most 500 characters.")
        result[key] = item
    return result


def serialize_record(record: dict) -> str:
    def safe(value):
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        if isinstance(value, dict):
            return {key: safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [safe(item) for item in value]
        return value

    return json.dumps(safe(record), indent=2, sort_keys=True, default=str, allow_nan=False)


def load_queue(email: str, pos_source: str = "", issue: str = "") -> tuple[list[dict], list[dict]]:
    require_operator(email)
    with sales.get_connection() as connection, connection.cursor() as cursor:
        rows = sales.fetch_rows(cursor, OPEN_SQL, {"pos_source": pos_source, "issue": issue})
        history = sales.fetch_rows(cursor, f"""
            SELECT request_id, quarantine_hash, status, submitted_by, submitted_at,
                   reason, last_error, restored_transaction_id, restored_pos_source,
                   completed_at, job_run_id
            FROM {REQUEST_TABLE}
            ORDER BY submitted_at DESC, request_id DESC LIMIT 50
        """)
    return rows, history


def load_record(email: str, quarantine_hash: str) -> dict:
    require_operator(email)
    with sales.get_connection() as connection, connection.cursor() as cursor:
        rows = sales.fetch_rows(
            cursor, f"SELECT * FROM {QUARANTINE_TABLE} WHERE source_row_hash = :hash",
            {"hash": quarantine_hash},
        )
    if len(rows) != 1:
        raise ValueError("Selected quarantine version is missing or ambiguous. Refresh the queue.")
    return rows[0]


def submit_request(
    email: str, request_id: str, quarantine_hash: str, patch_text: str,
    reason: str, confirmed: bool,
) -> int:
    email = require_operator(email)
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("Select a quarantine version before submitting.")
    UUID(request_id)
    if not confirmed:
        raise ValueError("Confirm that the correction was checked against the source system.")
    reason = reason.strip()
    if not reason or len(reason) > 2000:
        raise ValueError("A correction reason of 1-2,000 characters is required.")
    if len(patch_text) > 10000:
        raise ValueError("Correction JSON is too large.")
    patch = validate_patch(json.loads(patch_text))
    job_id = os.environ.get("QUARANTINE_RECOVERY_JOB_ID")
    if not job_id:
        raise RuntimeError("QUARANTINE_RECOVERY_JOB_ID is missing; no request was submitted.")
    parameters = {
        "request_id": request_id, "quarantine_hash": quarantine_hash,
        "corrections_json": json.dumps(patch, sort_keys=True, separators=(",", ":"), allow_nan=False),
        "submitted_by": email, "reason": reason,
    }
    with sales.get_connection() as connection, connection.cursor() as cursor:
        original = sales.fetch_rows(
            cursor, f"SELECT source_row_hash FROM {QUARANTINE_TABLE} WHERE source_row_hash = :quarantine_hash",
            {"quarantine_hash": quarantine_hash},
        )
        if len(original) != 1:
            raise ValueError("The selected quarantine version is missing or ambiguous. Refresh the queue.")
        cursor.execute(f"""
            MERGE INTO {REQUEST_TABLE} t
            USING (SELECT :request_id AS request_id) s ON t.request_id = s.request_id
            WHEN NOT MATCHED THEN INSERT (
                request_id, quarantine_hash, corrections_json, submitted_by,
                submitted_at, reason, status
            ) VALUES (
                :request_id, :quarantine_hash, :corrections_json, :submitted_by,
                current_timestamp(), :reason, 'SUBMITTED'
            )
        """, parameters=parameters)
        saved = sales.fetch_rows(
            cursor,
            f"""SELECT request_id, quarantine_hash, corrections_json, submitted_by, reason
                FROM {REQUEST_TABLE} WHERE request_id = :request_id""",
            {"request_id": request_id},
        )
        if len(saved) != 1 or any(saved[0][key] != value for key, value in parameters.items()):
            raise RuntimeError("Request could not be verified. Do not change the selection; retry or contact the owner.")
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.errors import DatabricksError

    try:
        run = WorkspaceClient().jobs.run_now(
            job_id=int(job_id), idempotency_token=f"quarantine-{request_id}",
        )
    except DatabricksError as error:
        raise RuntimeError(
            f"Request {request_id} was saved, but job submission failed. Retry the same "
            "unchanged form to start it safely, or ask Operations to run Build Silver Theater Data."
        ) from error
    run_id = run.response.run_id
    if run_id is None:
        raise RuntimeError(f"Request {request_id} was saved, but the job did not return a run ID. Retry unchanged.")
    with sales.get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {REQUEST_TABLE} SET job_run_id = :run_id WHERE request_id = :request_id",
            parameters={"run_id": run_id, "request_id": request_id},
        )
    return run_id
