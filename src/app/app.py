import os

import gradio as gr
import pandas as pd
from databricks import sql
from databricks.sdk.core import Config

CATALOG = os.environ.get("THEATER_CATALOG", "workspace")
SCHEMA = os.environ.get("THEATER_SCHEMA", "fde_theater")
TABLE = "denormalized_theater_sales"


def quote_identifier(identifier: str) -> str:
    if not identifier or "\x00" in identifier:
        raise ValueError(f"Invalid Unity Catalog identifier: {identifier!r}")
    return f"`{identifier.replace('`', '``')}`"


TABLE_NAME = ".".join(
    quote_identifier(part) for part in (CATALOG, SCHEMA, TABLE)
)


def get_connection():
    warehouse_id = os.environ.get("DATABRICKS_WAREHOUSE_ID")
    if not warehouse_id:
        raise RuntimeError(
            "DATABRICKS_WAREHOUSE_ID is missing. Add a SQL warehouse resource "
            "to the app deployment."
        )

    config = Config()
    hostname = config.host.removeprefix("https://").rstrip("/")
    return sql.connect(
        server_hostname=hostname,
        http_path=f"/sql/1.0/warehouses/{warehouse_id}",
        credentials_provider=lambda: config.authenticate,
    )


def fetch_rows(cursor, query: str, theater_id: str | None = None) -> list[dict]:
    if theater_id:
        cursor.execute(query, parameters={"theater_id": theater_id})
    else:
        cursor.execute(query)
    column_names = [description[0] for description in cursor.description]
    return [
        dict(zip(column_names, row, strict=True))
        for row in cursor.fetchall()
    ]


def to_dataframe(rows: list[dict], numeric_column: str) -> pd.DataFrame:
    dataframe = pd.DataFrame(rows)
    if numeric_column in dataframe:
        dataframe[numeric_column] = pd.to_numeric(
            dataframe[numeric_column], errors="coerce"
        )
    return dataframe


def update_dashboard(theater_id: str | None):
    selected_theater = theater_id or None
    with get_connection() as connection, connection.cursor() as cursor:
        theaters = fetch_rows(
            cursor,
            f"""
                SELECT theater_id, MAX(theater_name) AS theater_name,
                       MAX(city) AS city, MAX(state) AS state
                FROM {TABLE_NAME}
                GROUP BY theater_id
                ORDER BY theater_name, theater_id
                """,
        )

        theater_filter = (
            "WHERE theater_id = :theater_id" if selected_theater else ""
        )
        metrics = fetch_rows(
            cursor,
            f"""
                SELECT
                    COUNT(*) AS transaction_rows,
                    COUNT(DISTINCT transaction_id) AS unique_transactions,
                    COALESCE(SUM(seats_sold), 0) AS tickets_sold,
                    COALESCE(SUM(gross_revenue), 0) AS gross_revenue,
                    AVG(ticket_price) AS avg_ticket_price,
                    COUNT_IF(showtime IS NULL) AS missing_showtimes,
                    MIN(TRY_CAST(showtime AS DATE)) AS first_show_date,
                    MAX(TRY_CAST(showtime AS DATE)) AS last_show_date
                FROM {TABLE_NAME}
                {theater_filter}
                """,
            selected_theater,
        )[0]

        daily_revenue = fetch_rows(
            cursor,
            f"""
                SELECT TRY_CAST(showtime AS DATE) AS show_date,
                       SUM(gross_revenue) AS gross_revenue
                FROM {TABLE_NAME}
                {theater_filter}
                {"AND" if selected_theater else "WHERE"} showtime IS NOT NULL
                GROUP BY TRY_CAST(showtime AS DATE)
                ORDER BY show_date
                """,
            selected_theater,
        )
        genre_revenue = fetch_rows(
            cursor,
            f"""
                SELECT genre, SUM(gross_revenue) AS gross_revenue
                FROM {TABLE_NAME}
                {theater_filter}
                GROUP BY genre
                ORDER BY gross_revenue DESC
                """,
            selected_theater,
        )
        movie_revenue = fetch_rows(
            cursor,
            f"""
                SELECT movie_title, SUM(gross_revenue) AS gross_revenue
                FROM {TABLE_NAME}
                {theater_filter}
                GROUP BY movie_title
                ORDER BY gross_revenue DESC
                LIMIT 10
                """,
            selected_theater,
        )

    choices = [("All theaters", "")]
    choices.extend(
        (
            f"{row['theater_name']} — {row['city']}, {row['state']} ({row['theater_id']})",
            row["theater_id"],
        )
        for row in theaters
    )

    first_date = metrics["first_show_date"]
    last_date = metrics["last_show_date"]
    date_range = (
        f"{first_date} to {last_date}"
        if first_date and last_date
        else "No show dates available"
    )
    data_note = (
        "No sales rows found. Run the seed and transform jobs, then refresh."
        if not metrics["transaction_rows"]
        else (
            "Metrics reflect the denormalized source as-is, including seeded "
            "duplicates and invalid prices; orphan records dropped by the "
            "upstream inner joins are not included. "
            f"{metrics['missing_showtimes']} rows with missing showtime are "
            "excluded from the daily chart."
        )
    )
    freshness = (
        f"**Data source:** `{CATALOG}.{SCHEMA}.{TABLE}`  \n"
        f"**Show-date range:** {date_range}  \n"
        f"**Data note:** {data_note}"
    )

    return (
        gr.update(choices=choices, value=theater_id or ""),
        float(metrics["gross_revenue"] or 0),
        int(metrics["tickets_sold"] or 0),
        int(metrics["unique_transactions"] or 0),
        int(metrics["transaction_rows"] or 0),
        float(metrics["avg_ticket_price"] or 0),
        freshness,
        to_dataframe(daily_revenue, "gross_revenue"),
        to_dataframe(genre_revenue, "gross_revenue"),
        to_dataframe(movie_revenue, "gross_revenue"),
    )


empty_daily = pd.DataFrame(columns=["show_date", "gross_revenue"])
empty_genres = pd.DataFrame(columns=["genre", "gross_revenue"])
empty_movies = pd.DataFrame(columns=["movie_title", "gross_revenue"])

with gr.Blocks(title="Reel Insights") as gradio_app:
    gr.Markdown(
        "# Reel Insights\n"
        "Explore theater sales performance from the denormalized sales table."
    )
    with gr.Row():
        theater_filter = gr.Dropdown(
            choices=[("All theaters", "")],
            value="",
            label="Theater",
            interactive=True,
        )
        refresh_button = gr.Button("Refresh metrics", variant="primary")

    with gr.Row():
        gross_revenue = gr.Number(label="Gross revenue (USD)", precision=2)
        tickets_sold = gr.Number(label="Tickets sold", precision=0)
        unique_transactions = gr.Number(label="Unique transactions", precision=0)
        transaction_rows = gr.Number(label="Transaction rows", precision=0)
        avg_ticket_price = gr.Number(label="Average ticket price (USD)", precision=2)

    data_context = gr.Markdown()

    with gr.Row():
        daily_chart = gr.LinePlot(
            value=empty_daily,
            x="show_date",
            y="gross_revenue",
            title="Gross revenue by show date",
            x_title="Show date",
            y_title="Revenue (USD)",
        )
        genre_chart = gr.BarPlot(
            value=empty_genres,
            x="genre",
            y="gross_revenue",
            title="Gross revenue by genre",
            x_title="Genre",
            y_title="Revenue (USD)",
            vertical=False,
        )

    movie_chart = gr.BarPlot(
        value=empty_movies,
        x="movie_title",
        y="gross_revenue",
        title="Top 10 movies by gross revenue",
        x_title="Movie",
        y_title="Revenue (USD)",
        vertical=False,
    )

    dashboard_outputs = [
        theater_filter,
        gross_revenue,
        tickets_sold,
        unique_transactions,
        transaction_rows,
        avg_ticket_price,
        data_context,
        daily_chart,
        genre_chart,
        movie_chart,
    ]
    theater_filter.change(
        fn=update_dashboard,
        inputs=theater_filter,
        outputs=dashboard_outputs,
    )
    refresh_button.click(
        fn=update_dashboard,
        inputs=theater_filter,
        outputs=dashboard_outputs,
    )
    gradio_app.load(
        fn=update_dashboard,
        inputs=theater_filter,
        outputs=dashboard_outputs,
    )


if __name__ == "__main__":
    port = int(os.environ.get("DATABRICKS_APP_PORT", "8000"))
    gradio_app.queue(default_concurrency_limit=8).launch(
        server_name="0.0.0.0",
        server_port=port,
    )
