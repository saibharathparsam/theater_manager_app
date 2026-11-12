import logging
import os

import gradio as gr
import pandas as pd
from databricks.sql.exc import Error as SqlError

import sales

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
                "See Recent decisions for the rationale and approver."
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


with gr.Blocks(title="Reel Insights | Leadership") as gradio_app:
    gr.Markdown(
        "# Reel Insights | Leadership\n"
        "Review Gold sales performance and approve evidence-based movie decisions."
    )
    gr.Markdown(
        "## Recent decisions | shared with all app users\n"
        "Independent of your sales filters and Gold row-level security. "
        "Open this app to see recent decisions; new decisions trigger a notification "
        "while it remains open. This records approvals, not schedule changes."
    )
    notification_status = gr.Markdown("Loading recent decisions...")
    notification_feed = gr.Dataframe(headers=HISTORY_COLUMNS, interactive=False, wrap=True)
    seen_decisions = gr.State(None)
    decision_timer = gr.Timer(60)
    notification_outputs = [notification_feed, seen_decisions, notification_status]
    for event in (gradio_app.load, decision_timer.tick):
        event(
            fn=refresh_decision_notifications, inputs=[seen_decisions],
            outputs=notification_outputs, api_name=False,
            concurrency_id="decision-notifications", concurrency_limit=1,
        )
    with gr.Row():
        state_filter = gr.Dropdown(
            choices=[("All states", "")], value="", label="State", interactive=True
        )
        theater_filter = gr.Dropdown(
            choices=[("All theaters", "")], value="", label="Theater", interactive=True
        )
        refresh_button = gr.Button("Refresh metrics and recommendations", variant="primary")
    with gr.Row():
        revenue = gr.Number(label="Revenue (USD) | selected 7 days", precision=2, interactive=False)
        tickets = gr.Number(label="Tickets | selected 7 days", precision=0, interactive=False)
        theaters = gr.Number(label="Active theaters | selected 7 days", precision=0, interactive=False)
        movies = gr.Number(label="Movies | selected 7 days", precision=0, interactive=False)
    context = gr.Markdown()
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
        "fewer than five per list; a movie never appears in both lists."
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
    recommendations = gr.State({})
    gr.Markdown(
        "## Leadership approval\n"
        "Select one candidate and provide a rationale. Approval records an audit event; "
        "it does **not** change schedules. Confirm the state and theater scope above before approving."
    )
    decision = gr.Dropdown(label="Movie and proposed action", interactive=False)
    note = gr.Textbox(label="Leadership rationale (required, max 2,000 characters)", interactive=False)
    confirm = gr.Checkbox(label="I approve this action for the displayed scope and period", interactive=False)
    approve_button = gr.Button("Approve and record feedback", interactive=False, variant="primary")
    status = gr.Markdown()
    gr.Markdown("## Latest 20 approved decisions\nRefresh to view newly recorded decisions.")
    history = gr.Dataframe(headers=HISTORY_COLUMNS, interactive=False, wrap=True)
    outputs = [
        theater_filter, revenue, tickets, theaters, movies, context, daily,
        discontinue, expand, recommendations, decision, confirm, note,
        approve_button, status, history, state_filter,
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


if __name__ == "__main__":
    gradio_app.queue(default_concurrency_limit=8).launch(
        server_name="0.0.0.0",
        server_port=int(os.environ.get("DATABRICKS_APP_PORT", "8000")),
        show_error=False,
    )
