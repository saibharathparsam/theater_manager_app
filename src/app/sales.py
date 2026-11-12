"""Gold analytics and server-validated leadership approvals."""

import json
import os
from datetime import date
from decimal import Decimal
from uuid import uuid4

CATALOG = os.environ.get("THEATER_CATALOG", "theater_analytics")
SCHEMA = os.environ.get("THEATER_SCHEMA", "gold")


def quote_identifier(value: str) -> str:
    if not value or "\x00" in value:
        raise ValueError(f"Invalid Unity Catalog identifier: {value!r}")
    return f"`{value.replace('`', '``')}`"


TABLE_NAME = ".".join(
    quote_identifier(part) for part in (CATALOG, SCHEMA, "theater_daily_sales")
)
FEEDBACK_TABLE = ".".join(
    quote_identifier(part)
    for part in (
        CATALOG,
        SCHEMA,
        os.environ.get("THEATER_FEEDBACK_TABLE", "decision_feedback"),
    )
)
APPROVERS = {
    email.strip().casefold()
    for email in os.environ.get("LEADERSHIP_APPROVERS", "").split(",")
    if email.strip()
}

# Bound against all Gold rows visible to the querying user, not each theater's last sale.
PERIOD_CTE = f"""
WITH bounds AS (
    SELECT DATE_SUB(MAX(show_date), 6) AS period_start,
           MAX(show_date) AS period_end FROM {TABLE_NAME}
), selected AS (
    SELECT g.* FROM {TABLE_NAME} g CROSS JOIN bounds b
    WHERE g.show_date BETWEEN b.period_start AND b.period_end
      AND (:theater_id = '' OR g.theater_id = :theater_id)
      AND (:state = '' OR g.state = :state)
)
"""
METRICS_SQL = PERIOD_CTE + """
SELECT COUNT(*) AS daily_groups,
       COALESCE(SUM(revenue), 0) AS revenue,
       COALESCE(SUM(seats_sold), 0) AS tickets_sold,
       COUNT(DISTINCT theater_id) AS theaters,
       COUNT(DISTINCT movie_id) AS movies,
       COUNT_IF(NOT has_movie_match OR NOT has_layout_match) AS unmatched_groups,
       COUNT_IF(revenue IS NULL OR seats_sold IS NULL
                OR revenue < 0 OR seats_sold < 0) AS suspect_groups,
       (SELECT period_start FROM bounds) AS period_start,
       (SELECT period_end FROM bounds) AS period_end
FROM selected
"""
MOVIES_SQL = PERIOD_CTE + """
SELECT movie_id, MAX(movie_title) AS movie_title,
       SUM(revenue) AS revenue, SUM(seats_sold) AS tickets_sold,
       COUNT(DISTINCT STRUCT(theater_id, screen_id, show_date)) AS active_screen_days
FROM selected
WHERE has_movie_match AND has_layout_match
  AND movie_id IS NOT NULL AND TRIM(movie_id) NOT IN ('', 'Unknown')
  AND theater_id IS NOT NULL AND TRIM(theater_id) NOT IN ('', 'Unknown')
  AND screen_id IS NOT NULL AND TRIM(screen_id) NOT IN ('', 'Unknown')
GROUP BY movie_id
HAVING COUNT_IF(revenue IS NULL OR seats_sold IS NULL
                OR revenue < 0 OR seats_sold < 0) = 0
ORDER BY movie_id
"""


def get_connection():
    from databricks import sql
    from databricks.sdk.core import Config

    warehouse = os.environ.get("DATABRICKS_WAREHOUSE_ID")
    if not warehouse:
        raise RuntimeError("DATABRICKS_WAREHOUSE_ID is missing from app configuration.")
    config = Config()
    return sql.connect(
        server_hostname=config.host.removeprefix("https://").rstrip("/"),
        http_path=f"/sql/1.0/warehouses/{warehouse}",
        credentials_provider=lambda: config.authenticate,
    )


def get_user_connection(user_token: str):
    from databricks import sql
    from databricks.sdk.core import Config

    if not user_token:
        raise RuntimeError(
            "User SQL authorization is missing. Enable the app's sql user scope "
            "and reopen the app to authorize access."
        )
    warehouse = os.environ.get("DATABRICKS_WAREHOUSE_ID")
    if not warehouse:
        raise RuntimeError("DATABRICKS_WAREHOUSE_ID is missing from app configuration.")
    config = Config()
    return sql.connect(
        server_hostname=config.host.removeprefix("https://").rstrip("/"),
        http_path=f"/sql/1.0/warehouses/{warehouse}",
        access_token=user_token,
    )


def fetch_rows(cursor, query: str, parameters: dict | None = None) -> list[dict]:
    cursor.execute(query, parameters=parameters or {})
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def load_recent_decisions() -> list[dict]:
    with get_connection() as connection, connection.cursor() as cursor:
        return fetch_rows(
            cursor,
            f"""SELECT decision_id, approved_at, approver_email, action, movie_title,
                       theater_id, period_start, period_end, approval_note,
                       COALESCE(get_json_object(evidence_json, '$.state'), '') AS state
                FROM {FEEDBACK_TABLE}
                ORDER BY approved_at DESC, decision_id DESC LIMIT 20""",
        )


def read_movies(cursor, theater_id: str, state: str = "") -> list[dict]:
    rows = fetch_rows(cursor, MOVIES_SQL, {"theater_id": theater_id, "state": state})
    for row in rows:
        row["score"] = Decimal(row["tickets_sold"]) / Decimal(row["active_screen_days"])
    return rows


def rank_movies(rows: list[dict]) -> list[dict]:
    """Split small catalogs to avoid recommending both actions for the same movie."""
    ordered = sorted(rows, key=lambda row: (-row["score"], row["movie_id"]))
    expand = ordered[: min(5, (len(ordered) + 1) // 2)]
    expand_ids = {row["movie_id"] for row in expand}
    discontinue = sorted(
        (row for row in rows if row["movie_id"] not in expand_ids),
        key=lambda row: (row["score"], row["movie_id"]),
    )[:5]
    return [
        {**row, "action": action}
        for action, movies in (("Increase screens", expand), ("Discontinue", discontinue))
        for row in movies
    ]


def evidence_fingerprint(movie: dict, start: date, end: date, state: str = "") -> str:
    return json.dumps(
        {
            "movie_id": movie["movie_id"],
            "movie_title": movie["movie_title"],
            "action": movie["action"],
            "state": state,
            "period_start": str(start),
            "period_end": str(end),
            "tickets_sold": str(movie["tickets_sold"]),
            "revenue": str(movie["revenue"]),
            "active_screen_days": movie["active_screen_days"],
            "tickets_per_active_screen_day": str(movie["score"]),
        },
        sort_keys=True,
    )


def load_dashboard(theater_id: str, user_token: str, state: str = "") -> dict:
    with get_user_connection(user_token) as connection, connection.cursor() as cursor:
        theaters = fetch_rows(
            cursor,
            f"""SELECT theater_id, MAX(theater_name) AS theater_name, state
                FROM {TABLE_NAME}
                WHERE theater_id IS NOT NULL
                GROUP BY theater_id, state ORDER BY theater_name, theater_id""",
        )
        states = sorted({row["state"] for row in theaters if row.get("state")})
        theaters = [row for row in theaters if not state or row.get("state") == state]
        if theater_id and theater_id not in {row["theater_id"] for row in theaters}:
            theater_id = ""
        parameters = {"theater_id": theater_id, "state": state}
        metrics = fetch_rows(cursor, METRICS_SQL, parameters)[0]
        movies = rank_movies(read_movies(cursor, theater_id, state))
        daily = fetch_rows(
            cursor,
            PERIOD_CTE + """
            SELECT show_date, SUM(revenue) AS revenue
            FROM selected GROUP BY show_date ORDER BY show_date
            """,
            parameters,
        )
    with get_connection() as connection, connection.cursor() as cursor:
        history = fetch_rows(
            cursor,
            f"""SELECT approved_at, approver_email, action, movie_title,
                       theater_id, period_start, period_end, approval_note,
                       COALESCE(get_json_object(evidence_json, '$.state'), '') AS state
                FROM {FEEDBACK_TABLE}
                WHERE (:theater_id = '' OR theater_id = :theater_id)
                  AND (:state = '' OR get_json_object(evidence_json, '$.state') = :state)
                ORDER BY approved_at DESC, decision_id DESC LIMIT 20""",
            parameters,
        )
    recommendations = {}
    for movie in movies:
        decision_id = str(uuid4())
        recommendations[decision_id] = {
            **movie,
            "decision_id": decision_id,
            "theater_id": theater_id,
            "state": state,
            "period_start": metrics["period_start"],
            "period_end": metrics["period_end"],
            "evidence": evidence_fingerprint(
                movie, metrics["period_start"], metrics["period_end"], state
            ),
        }
    return {
        "theaters": theaters,
        "metrics": metrics,
        "daily": daily,
        "recommendations": recommendations,
        "history": history,
        "states": states,
        "theater_id": theater_id,
    }


def approve_decision(
    decision_id: str,
    recommendations: dict,
    theater_id: str,
    confirmed: bool,
    note: str,
    email: str,
    user_token: str,
    state: str = "",
) -> str:
    """Never trust a client-supplied movie/action/score or a manually entered identity."""
    email = email.strip().casefold()
    if not email or email not in APPROVERS:
        raise PermissionError("Only configured leadership approvers may record decisions.")
    if not confirmed:
        raise ValueError("Confirm the selected decision before approving.")
    if decision_id not in recommendations:
        raise ValueError("Select a recommendation from the current review.")
    candidate = recommendations[decision_id]
    if candidate["theater_id"] != theater_id or candidate.get("state", "") != state:
        raise ValueError("State or theater scope changed. Refresh recommendations before approving.")
    note = note.strip()
    if not note or len(note) > 2000:
        raise ValueError("Enter an approval note between 1 and 2,000 characters.")
    with get_connection() as connection, connection.cursor() as cursor:
        existing = fetch_rows(
            cursor,
            f"SELECT decision_id FROM {FEEDBACK_TABLE} WHERE decision_id = :decision_id",
            {"decision_id": decision_id},
        )
        if existing:
            return f"Decision {decision_id} was already recorded. No duplicate was added."
        with get_user_connection(user_token) as user_connection, user_connection.cursor() as user_cursor:
            metrics = fetch_rows(
                user_cursor, METRICS_SQL, {"theater_id": theater_id, "state": state}
            )[0]
            current = rank_movies(read_movies(user_cursor, theater_id, state))
        matching = next(
            (
                movie for movie in current
                if movie["movie_id"] == candidate["movie_id"]
                and movie["action"] == candidate["action"]
            ),
            None,
        )
        if matching is None or evidence_fingerprint(
            matching, metrics["period_start"], metrics["period_end"], state
        ) != candidate["evidence"]:
            raise ValueError("Gold data or ranking changed. Refresh and review before approving.")
        parameters = {
            "decision_id": decision_id,
            "movie_id": candidate["movie_id"],
            "movie_title": candidate["movie_title"],
            "action": candidate["action"],
            "theater_id": theater_id,
            "period_start": candidate["period_start"],
            "period_end": candidate["period_end"],
            "approver_email": email,
            "approval_note": note,
            "source_table": f"{CATALOG}.{SCHEMA}.theater_daily_sales",
            "evidence_json": candidate["evidence"],
        }
        # A retry of the same review uses its original server-generated decision ID.
        cursor.execute(
            f"""MERGE INTO {FEEDBACK_TABLE} target
                USING (SELECT :decision_id AS decision_id) source
                ON target.decision_id = source.decision_id
                WHEN NOT MATCHED THEN INSERT (
                    decision_id, movie_id, movie_title, action, theater_id,
                    period_start, period_end, approver_email, approved_at,
                    approval_note, source_table, evidence_json
                ) VALUES (
                    :decision_id, :movie_id, :movie_title, :action, :theater_id,
                    :period_start, :period_end, :approver_email, CURRENT_TIMESTAMP(),
                    :approval_note, :source_table, :evidence_json
                )""",
            parameters=parameters,
        )
        saved = fetch_rows(
            cursor,
            f"SELECT decision_id FROM {FEEDBACK_TABLE} WHERE decision_id = :decision_id",
            {"decision_id": decision_id},
        )
        if not saved:
            raise RuntimeError("Approval write could not be verified. Refresh decision history.")
    return f"Approved and recorded decision {decision_id}. Screening schedules were not changed."
