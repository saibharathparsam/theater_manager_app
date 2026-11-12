import logging
import os
from uuid import uuid4

import gradio as gr
import pandas as pd
from databricks.sql.exc import Error as SqlError

import sales
import quarantine

logger = logging.getLogger("reel_insights")
logging.basicConfig(level=logging.INFO)

MOVIE_COLUMNS = [
    "Movie ID", "Movie", "Tickets", "Active screen-days",
    "Tickets / active screen-day", "Revenue (USD)",
]
HISTORY_COLUMNS = [
    "Approved at", "Approver", "Action", "Movie", "Theater scope",
    "Period start", "Period end", "Approval note", "State scope",
]
QUARANTINE_COLUMNS = [
    "Quarantine version", "Transaction", "POS", "Theater", "Movie",
    "Ticket price", "DQ reasons", "Ingested",
]
RECOVERY_COLUMNS = [
    "request_id", "quarantine_hash", "status", "submitted_by", "submitted_at",
    "reason", "last_error", "restored_transaction_id", "restored_pos_source",
    "completed_at", "job_run_id",
]

APP_CSS = """
.gradio-container { max-width: 1440px !important; }
.app-header { padding: 1.25rem 1.5rem; border: 1px solid var(--border-color-primary);
  border-radius: 1rem; background: var(--background-fill-secondary); margin-bottom: 1rem; }
.app-header h1 { margin: 0 0 .35rem; }
.page-intro { padding: .5rem 0; }
.scope-card { padding: .85rem 1rem; border: 1px solid var(--border-color-primary);
  border-radius: .75rem; background: var(--background-fill-secondary); }
.step-card { border: 1px solid var(--border-color-primary); border-radius: .75rem;
  padding: 1rem; background: var(--background-fill-secondary); }
"""


def viewer_email(request: gr.Request) -> str:
    # Databricks Apps supplies this header at its authenticated reverse proxy.
    # Never accept an editable email field or expose this server outside that proxy.
    return (request.headers.get("x-forwarded-email", "") if request else "").strip().casefold()


def viewer_token(request: gr.Request) -> str:
    token = request.headers.get("x-forwarded-access-token", "") if request else ""
    if not token:
        raise RuntimeError(
            "User SQL authorization is missing. Enable the app's sql user scope "
            "and reopen the app to authorize access."
        )
    return token


def recommendation_table(recommendations: dict, action: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            [
                row["movie_id"], row["movie_title"], int(row["tickets_sold"]),
                row["active_screen_days"], round(float(row["score"]), 2),
                float(row["revenue"]),
            ]
            for row in recommendations.values() if row["action"] == action
        ],
        columns=MOVIE_COLUMNS,
    )


def decision_table(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            [
                row["approved_at"], row["approver_email"], row["action"],
                row["movie_title"], row["theater_id"] or "All theaters",
                row["period_start"], row["period_end"], row["approval_note"],
                row.get("state") or "All states",
            ]
            for row in rows
        ],
        columns=HISTORY_COLUMNS,
    )


def refresh_decision_notifications(seen_ids: set | None):
    try:
        rows = sales.load_recent_decisions()
    except (SqlError, RuntimeError):
        logger.exception("Shared decision notifications refresh failed")
        return (
            gr.skip(), seen_ids,
            "Decision notifications could not refresh. Displayed decisions may be stale; "
            "the app will retry in 60 seconds.",
        )
    current_ids = {row["decision_id"] for row in rows}
    if seen_ids is not None:
        new_rows = [row for row in rows if row["decision_id"] not in seen_ids]
        if new_rows:
            latest = new_rows[0]
            gr.Info(
                f"{len(new_rows)} new leadership decision(s). Latest: "
                f"{latest['action']} - {latest['movie_title']}; "
                f"state: {latest.get('state') or 'All states'}; "
                f"theater: {latest['theater_id'] or 'All theaters'}. "
                "See Decisions & feedback for the rationale and approver."
            )
    return (
        decision_table(rows),
        (seen_ids or set()) | current_ids,
        "Latest 20 decisions across all states/theaters. Refreshes every 60 seconds."
        if rows else "No leadership decisions have been recorded yet. Refreshes every 60 seconds.",
    )


def update_dashboard(theater_id: str | None, request: gr.Request, state: str = ""):
    try:
        data = sales.load_dashboard(theater_id or "", viewer_token(request), state or "")
    except (SqlError, RuntimeError) as error:
        logger.exception("Gold dashboard refresh failed")
        raise gr.Error(
            "Unable to load Gold metrics or decision history. Verify Gold/feedback "
            "tables, your SQL warehouse/table access, the app's sql user scope, "
            "and app service-principal feedback grants. Reopen the app after authorizing."
        ) from error
    metrics = data["metrics"]
    theater_id = data.get("theater_id", theater_id)
    recommendations = data["recommendations"]
    email = viewer_email(request)
    can_approve = email in sales.APPROVERS
    start, end = metrics["period_start"], metrics["period_end"]
    period = f"{start} to {end}" if end else "No dated Gold sales available"
    choices = [("All theaters", "")]
    choices.extend(
        (f"{row['theater_name']} ({row['theater_id']})", row["theater_id"])
        for row in data["theaters"]
    )
    selection_choices = [
        (f"{row['action']}: {row['movie_title']} ({row['movie_id']})", key)
        for key, row in recommendations.items()
    ]
    context = (
        f"**Source:** `{sales.CATALOG}.{sales.SCHEMA}.theater_daily_sales`  \n"
        f"**Period:** {period} (7 calendar days ending at your latest visible show date).  \n"
        f"**Scope:** State: {state or 'All states'} | Theater: {theater_id or 'All theaters in selected state'}.  \n"
        f"**Viewer:** {email or 'Not identified - approvals disabled'} | "
        f"**Approval access:** {'Leadership' if can_approve else 'Read-only'}  \n"
        "**Execution identity:** Gold reads and approval evidence checks run as you, "
        "with Gold row-level security. Feedback history/writes use the app service "
        "principal; feedback history is shared with app viewers and is not protected "
        "by the Gold sales row filter. Theater filtering only narrows your visible data.  \n"
        f"**Coverage:** {metrics['unmatched_groups']} unmatched daily groups and "
        f"{metrics['suspect_groups']} groups with missing/negative measures. "
        "KPIs include these groups; recommendation movies exclude unmatched groups "
        "and movies with suspect measures in matched groups. Null-date groups are "
        "outside this period. Gold has no transaction counts or screening schedule.  \n"
        "**Freshness:** This is the latest dated Gold snapshot, not necessarily recent "
        "activity or the last pipeline completion time."
    )
    if not metrics["daily_groups"]:
        context += (
            "  \n**No sales in this scope/period.** Select another theater or ask the "
            "Gold owner to verify your manager/zone-manager mapping or leadership membership."
        )
    elif not recommendations:
        context += "  \n**No eligible recommendations.** Check reference matches and measures."
    history = decision_table(data["history"])
    daily = pd.DataFrame(data["daily"], columns=["show_date", "revenue"])
    daily["show_date"] = pd.to_datetime(daily["show_date"]).dt.strftime("%Y-%m-%d")
    daily["revenue"] = pd.to_numeric(daily["revenue"])
    return (
        gr.update(choices=choices, value=theater_id or ""),
        float(metrics["revenue"]), int(metrics["tickets_sold"]),
        int(metrics["theaters"]), int(metrics["movies"]),
        context, daily,
        recommendation_table(recommendations, "Discontinue"),
        recommendation_table(recommendations, "Increase screens"),
        recommendations,
        gr.update(choices=selection_choices, value=None, interactive=can_approve),
        gr.update(value=False, interactive=can_approve),
        gr.update(value="", interactive=can_approve),
        gr.update(interactive=can_approve and bool(recommendations)),
        "", history,
        gr.update(choices=[("All states", "")] + [(s, s) for s in data.get("states", [])],
                  value=state or ""),
        (
            f"**Current review scope**  \nState: {state or 'All states'} | "
            f"Theater: {theater_id or 'All theaters in selected state'}  \n"
            f"Period: {period}  \nChange filters on Sales overview; changes reset the approval form."
        ),
    )


def approve(decision_id, recommendations, theater_id, confirmed, note, request: gr.Request, state: str = ""):
    try:
        result = sales.approve_decision(
            decision_id, recommendations, theater_id or "", confirmed, note,
            viewer_email(request),
            viewer_token(request),
            state or "",
        )
        gr.Info(result)
        return result
    except (ValueError, PermissionError) as error:
        logger.warning("Approval rejected: %s", error)
        raise gr.Error(str(error)) from error
    except (SqlError, RuntimeError) as error:
        logger.exception("Decision feedback write failed")
        raise gr.Error(
            "Approval could not be confirmed. Retry the same selection to avoid a "
            "duplicate; check SQL permissions and decision history."
        ) from error


def load_operations(request: gr.Request, pos_source: str = "", issue: str = ""):
    email = viewer_email(request)
    if email not in quarantine.OPERATORS:
        return gr.update(visible=False), pd.DataFrame(), gr.update(choices=[], value=None), pd.DataFrame(), ""
    try:
        rows, history = quarantine.load_queue(email, pos_source or "", issue or "")
    except (SqlError, RuntimeError) as error:
        logger.exception("Operations queue load failed")
        raise gr.Error("Unable to load quarantine. Verify ledger provisioning and app SELECT grants.") from error
    table = pd.DataFrame([
        {
            "Quarantine version": row["source_row_hash"],
            "Transaction": row["transaction_id"], "POS": row["pos_source"],
            "Theater": row["theater_id"], "Movie": row["movie_id"],
            "Ticket price": row["ticket_price"],
            "DQ reasons": ", ".join(row["data_quality_issues"]),
            "Ingested": row["ingestion_timestamp"],
        } for row in rows
    ], columns=QUARANTINE_COLUMNS)
    choices = [
        (f"{row['transaction_id'] or '(missing ID)'} | {row['pos_source'] or '(missing POS)'} | "
         f"{row['source_row_hash'][:12]}", row["source_row_hash"])
        for row in rows
    ]
    return (
        gr.update(visible=True), table, gr.update(choices=choices, value=None),
        pd.DataFrame(history, columns=RECOVERY_COLUMNS),
        "Showing up to 100 open versions and 50 recent requests. SUBMITTED = awaiting processing "
        "(inspect the job run ID if stuck); "
        "APPLIED = restored to Silver, downstream pending; COMPLETED = Gold refreshed; "
        "REJECTED = review the error and submit a new correction. Refresh to see progress."
        + (" No open versions match the current filters." if not rows else ""),
    )


def select_quarantine_record(quarantine_hash, request: gr.Request):
    try:
        row = quarantine.load_record(viewer_email(request), quarantine_hash)
    except (PermissionError, ValueError, RuntimeError, SqlError) as error:
        logger.exception("Quarantine selection failed")
        raise gr.Error("Cannot open this quarantine version. Verify Operations access and refresh.") from error
    details = {
        key: row.get({"purchase_timestamp": "purchase_timestamp_raw", "showtime": "showtime_raw"}.get(key, key))
        for key in quarantine.EDITABLE_FIELDS
    }
    return quarantine.serialize_record(details), str(uuid4()), "{}", "", False, ""


def submit_quarantine_correction(quarantine_hash, request_id, patch_text, reason, confirmed, request: gr.Request):
    try:
        run_id = quarantine.submit_request(
            viewer_email(request), request_id, quarantine_hash, patch_text, reason, confirmed,
        )
    except (ValueError, PermissionError, RuntimeError) as error:
        logger.warning("Quarantine correction rejected or not started: %s", error)
        raise gr.Error(str(error)) from error
    except SqlError as error:
        logger.exception("Quarantine correction request write failed")
        raise gr.Error(
            "Request could not be confirmed. Retry the same unchanged form; do not assume recovery succeeded."
        ) from error
    message = (
        f"Correction request {request_id} saved; job run {run_id} started or queued. "
        "This is not yet a successful restoration. Refresh Operations to check the outcome."
    )
    gr.Info(message)
    return message


with gr.Blocks(title="Reel Insights | Theater Operations", theme=gr.themes.Soft(), css=APP_CSS) as gradio_app:
    gr.Markdown(
        "# Reel Insights\n"
        "Sales intelligence, leadership decisions, and operational recovery in one workspace.",
        elem_classes=["app-header"],
    )
    with gr.Tabs():
        with gr.Tab("Sales overview", id="sales"):
            gr.Markdown(
                "## Sales overview\n"
                "Explore the latest visible seven-day Gold snapshot and review movie candidates. "
                "Your sales access is protected by Gold row-level security.",
                elem_classes=["page-intro"],
            )
            with gr.Row():
                state_filter = gr.Dropdown(
                    choices=[("All states", "")], value="", label="State", interactive=True
                )
                theater_filter = gr.Dropdown(
                    choices=[("All theaters", "")], value="", label="Theater", interactive=True
                )
                refresh_button = gr.Button("Refresh overview", variant="primary")
            with gr.Row():
                revenue = gr.Number(label="Revenue (USD) | 7 days", precision=2, interactive=False)
                tickets = gr.Number(label="Tickets | 7 days", precision=0, interactive=False)
                theaters = gr.Number(label="Active theaters | 7 days", precision=0, interactive=False)
                movies = gr.Number(label="Movies | 7 days", precision=0, interactive=False)
            context = gr.Markdown(elem_classes=["scope-card"])
            daily = gr.LinePlot(
                value=pd.DataFrame(columns=["show_date", "revenue"]),
                x="show_date", y="revenue", title="Daily Gold revenue",
                x_title="Show date", y_title="Revenue (USD)",
            )
            gr.Markdown(
                "## Movie review candidates\n"
                "**Ranking:** Tickets / distinct active theater-screen-days, summed across "
                "formats and POS sources. This is not occupancy or demand forecasting. "
                "Sales-free screen-days are not available. These relative rankings do not "
                "prove that a film should be discontinued or expanded. Small catalogs have "
                "fewer than five per list; a movie never appears in both lists. "
                "To record a decision, continue to **Decisions & feedback**."
            )
            with gr.Row():
                discontinue = gr.Dataframe(
                    headers=MOVIE_COLUMNS, label="Up to 5 movies to discontinue",
                    interactive=False, wrap=True,
                )
                expand = gr.Dataframe(
                    headers=MOVIE_COLUMNS, label="Up to 5 movies to increase screens",
                    interactive=False, wrap=True,
                )
        with gr.Tab("Decisions & feedback", id="feedback"):
            gr.Markdown(
                "## Decisions & feedback\n"
                "Review the current scope, record leadership feedback, and follow decisions "
                "shared across the organization.",
                elem_classes=["page-intro"],
            )
            feedback_scope = gr.Markdown("Loading current review scope...", elem_classes=["scope-card"])
            with gr.Group(elem_classes=["step-card"]):
                gr.Markdown(
                    "### Record a leadership decision\n"
                    "Choose a candidate from the Sales overview, add your rationale, and confirm the scope. "
                    "Approval records an audit event; it does **not** change schedules."
                )
                decision = gr.Dropdown(label="Movie and proposed action", interactive=False)
                note = gr.Textbox(label="Leadership rationale (required, max 2,000 characters)", interactive=False, lines=3)
                confirm = gr.Checkbox(label="I approve this action for the displayed scope and period", interactive=False)
                approve_button = gr.Button("Approve and record feedback", interactive=False, variant="primary")
                status = gr.Markdown()
            gr.Markdown(
                "### Recent decisions | shared with all app users\n"
                "This feed is independent of your sales filters and Gold row-level security. "
                "New decisions notify you while the app is open."
            )
            notification_status = gr.Markdown("Loading recent decisions...")
            notification_feed = gr.Dataframe(headers=HISTORY_COLUMNS, interactive=False, wrap=True)
            gr.Markdown("### Decisions in the current scope\nRefresh the Sales overview to update this history.")
            history = gr.Dataframe(headers=HISTORY_COLUMNS, interactive=False, wrap=True)
        with gr.Tab("Quarantine operations", id="quarantine", visible=False) as operations_panel:
            gr.Markdown(
                "## Quarantine operations\n"
                "Review rejected POS records, verify corrections against the source, and track recovery. "
                "This workspace is restricted to configured Operations users.",
                elem_classes=["page-intro"],
            )
            with gr.Row():
                operations_pos = gr.Textbox(label="POS source filter (blank = all)")
                operations_issue = gr.Dropdown(
                    choices=[
                        ("All rejection reasons", ""),
                        ("Invalid ticket price", "invalid_ticket_price"),
                        ("Invalid purchase timestamp", "invalid_purchase_timestamp"),
                        ("Missing transaction ID", "missing_transaction_id"),
                        ("Missing POS source", "missing_pos_source"),
                    ],
                    value="", label="Quarantine reason", interactive=True,
                )
                operations_refresh = gr.Button("Refresh queue & status", variant="primary")
            operations_help = gr.Markdown()
            gr.Markdown("### 1. Review the open queue")
            operations_queue = gr.Dataframe(
                headers=QUARANTINE_COLUMNS, interactive=False, wrap=True, label="Open quarantine versions",
            )
            operations_selection = gr.Dropdown(label="Quarantine version to review", interactive=True)
            with gr.Group(elem_classes=["step-card"]):
                gr.Markdown(
                    "### 2. Verify and correct\n"
                    "Compare the original with the source POS; never guess values. Originals are retained. "
                    "Use ISO timestamps with a timezone; timezone-free timestamps are interpreted as UTC."
                )
                with gr.Row():
                    operations_original = gr.Code(label="Original sales fields (read-only)", language="json", interactive=False)
                    operations_patch = gr.Code(
                        value="{}", label='Corrections (JSON), for example {"ticket_price": 14.5}',
                        language="json", interactive=True,
                    )
                operations_reason = gr.Textbox(label="Correction reason / source verification (required)", lines=2)
                operations_confirm = gr.Checkbox(label="I verified these corrections against the source system")
                operations_submit = gr.Button("Submit correction and start recovery job", variant="primary")
                operations_status = gr.Markdown()
            gr.Markdown(
                "### 3. Track recovery\n"
                "The job also runs normal Silver ingestion. Invalid corrections and existing-key conflicts "
                "are rejected; successful recoveries refresh combined Silver and Gold."
            )
            operations_history = gr.Dataframe(
                headers=RECOVERY_COLUMNS, interactive=False, wrap=True, label="Recent recovery requests",
            )
    seen_decisions = gr.State(None)
    decision_timer = gr.Timer(60)
    notification_outputs = [notification_feed, seen_decisions, notification_status]
    for event in (gradio_app.load, decision_timer.tick):
        event(
            fn=refresh_decision_notifications, inputs=[seen_decisions],
            outputs=notification_outputs, api_name=False,
            concurrency_id="decision-notifications", concurrency_limit=1,
        )
    recommendations = gr.State({})
    operations_request_id = gr.State("")
    outputs = [
        theater_filter, revenue, tickets, theaters, movies, context, daily,
        discontinue, expand, recommendations, decision, confirm, note,
        approve_button, status, history, state_filter, feedback_scope,
    ]
    for event in (state_filter.input, theater_filter.input, refresh_button.click, gradio_app.load):
        event(fn=update_dashboard, inputs=[theater_filter, state_filter], outputs=outputs)
    decision.input(fn=lambda: (False, ""), outputs=[confirm, status], api_name=False)
    approve_button.click(
        fn=approve,
        inputs=[decision, recommendations, theater_filter, confirm, note, state_filter],
        outputs=[status],
        concurrency_limit=1,
        concurrency_id="decision-approvals",
        api_name=False,
    )
    operations_outputs = [
        operations_panel, operations_queue, operations_selection, operations_history, operations_help,
    ]
    for event in (gradio_app.load, operations_refresh.click):
        event(
            fn=load_operations, inputs=[operations_pos, operations_issue],
            outputs=operations_outputs, api_name=False,
        )
    operations_selection.input(
        fn=select_quarantine_record, inputs=[operations_selection],
        outputs=[
            operations_original, operations_request_id, operations_patch,
            operations_reason, operations_confirm, operations_status,
        ], api_name=False,
    )
    operations_submit.click(
        fn=submit_quarantine_correction,
        inputs=[
            operations_selection, operations_request_id, operations_patch,
            operations_reason, operations_confirm,
        ],
        outputs=[operations_status], api_name=False,
        concurrency_id="quarantine-submissions", concurrency_limit=1,
    )


if __name__ == "__main__":
    gradio_app.queue(default_concurrency_limit=8).launch(
        server_name="0.0.0.0",
        server_port=int(os.environ.get("DATABRICKS_APP_PORT", "8000")),
        show_error=False,
    )
