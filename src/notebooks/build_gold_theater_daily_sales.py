# Databricks notebook source
# MAGIC %md
# MAGIC # Gold: Daily theater sales
# MAGIC
# MAGIC Read the current `theater_analytics.silver.pos_ticket_sales` snapshot.
# MAGIC Aggregate FIRST by `(theater_id, screen_id, movie_id, format, DATE(showtime))`,
# MAGIC summing `revenue` and `seats_sold` across transactions and POS sources.
# MAGIC Then LEFT JOIN Silver theater layout and IMDb metadata, preserving unmatched
# MAGIC groups. Null reference text becomes `Unknown`; other types remain nullable.
# MAGIC Manager/zone-manager emails come from the matched theater/screen, not a guess.
# MAGIC `open_time` and `close_time` are native TIME(0), not strings or dated timestamps.
# MAGIC Unmatched times remain null; midnight is `00:00:00`. Invalid source times fail.
# MAGIC Requires TIME-capable compute (Databricks Runtime 18 LTS+ or supported serverless).
# MAGIC
# MAGIC Full-refresh `theater_analytics.gold.theater_daily_sales` so corrected POS
# MAGIC transactions and reference changes are reflected without double-counting.
# MAGIC Transaction IDs, POS sources, payment fields, ingestion metadata, raw timestamps,
# MAGIC and transaction-level DQ fields are deliberately excluded from this daily grain.
# MAGIC Retain all Silver rows, including flagged/historical invalid-price rows; do not
# MAGIC read quarantine. SQL SUM ignores null values and returns null for all-null groups.
# MAGIC Null showtimes form a null `show_date` group instead of being silently dropped.
# MAGIC Dates use UTC, consistent with Silver; this is not a screening schedule or downtime metric.
# MAGIC
# MAGIC Run after all three Silver sources finish, with no concurrent source writers.
# MAGIC Duplicate source keys fail before the target is overwritten.
# MAGIC Manager columns do not enforce RLS.

# COMMAND ----------

from pyspark.sql import SparkSession
from pyspark.sql import functions as F

TARGET_TABLE = "theater_analytics.gold.theater_daily_sales"

DAILY_SALES_QUERY = """
WITH daily_sales AS (
    SELECT theater_id, screen_id, movie_id, format,
        CAST(showtime AS DATE) AS show_date,
        SUM(revenue) AS revenue,
        SUM(seats_sold) AS seats_sold
    FROM theater_analytics.silver.pos_ticket_sales
    GROUP BY theater_id, screen_id, movie_id, format, CAST(showtime AS DATE)
)
SELECT
    s.*,
    COALESCE(l.theater_name, 'Unknown') AS theater_name,
    COALESCE(l.city, 'Unknown') AS city,
    COALESCE(l.state, 'Unknown') AS state,
    COALESCE(l.manager_email, 'Unknown') AS manager_email,
    COALESCE(l.zone_manager_email, 'Unknown') AS zone_manager_email,
    CAST(l.open_time AS TIME(0)) AS open_time,
    CAST(l.close_time AS TIME(0)) AS close_time,
    l.screen_number,
    COALESCE(l.screen_type, 'Unknown') AS screen_type,
    l.imax_capable,
    l.row_count,
    l.seats_per_row,
    l.total_seats,
    COALESCE(m.title, 'Unknown') AS movie_title,
    COALESCE(m.genre, 'Unknown') AS genre,
    m.runtime_minutes,
    COALESCE(m.mpaa_rating, 'Unknown') AS mpaa_rating,
    m.release_date,
    m.imdb_rating,
    m.metascore,
    (l.theater_id IS NOT NULL) AS has_layout_match,
    (m.imdb_id IS NOT NULL) AS has_movie_match
FROM daily_sales s
LEFT JOIN theater_analytics.silver.theater_layout l
    ON s.theater_id = l.theater_id AND s.screen_id = l.screen_id
LEFT JOIN theater_analytics.silver.imdb_movie_metadata m
    ON s.movie_id = m.imdb_id
"""


def run_load(spark: SparkSession) -> None:
    spark.conf.set("spark.sql.session.timeZone", "UTC")
    sources = {
        "theater_analytics.silver.pos_ticket_sales": ["transaction_id", "pos_source"],
        "theater_analytics.silver.theater_layout": ["theater_id", "screen_id"],
        "theater_analytics.silver.imdb_movie_metadata": ["imdb_id"],
    }
    for table, keys in sources.items():
        duplicates = (
            spark.table(table).groupBy(*keys).count().where(F.col("count") > 1)
        )
        if duplicates.take(1):
            raise ValueError(f"{table} contains duplicate keys {keys}; resolve before loading Gold.")

    gold = spark.sql(DAILY_SALES_QUERY)
    spark.sql("CREATE SCHEMA IF NOT EXISTS theater_analytics.gold")
    if spark.catalog.tableExists(TARGET_TABLE):
        spark.sql(
            f"ALTER TABLE {TARGET_TABLE} SET TBLPROPERTIES "
            "('delta.feature.timeType' = 'supported')"
        )
    (
        gold.write.format("delta")
        .mode("overwrite")
        .option("overwriteSchema", "true")
        .saveAsTable(TARGET_TABLE)
    )
    print(f"Refreshed {TARGET_TABLE}; sales aggregated before reference enrichment.")


if __name__ == "__main__":
    run_load(SparkSession.builder.getOrCreate())
