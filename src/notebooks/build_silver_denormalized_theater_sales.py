# Databricks notebook source
# MAGIC %md
# MAGIC # Silver: Combined theater sales for Gold
# MAGIC
# MAGIC **Sources:** `theater_analytics.silver.pos_ticket_sales`, `theater_layout`,
# MAGIC and `imdb_movie_metadata`.
# MAGIC **Target:** `theater_analytics.silver.denormalized_theater_sales`.
# MAGIC
# MAGIC Preserve one row per `(transaction_id, pos_source)` from Silver POS.
# MAGIC LEFT JOIN layout on `(theater_id, screen_id)` and movies on `movie_id = imdb_id`;
# MAGIC unmatched transactions remain available for Gold instead of disappearing.
# MAGIC Null reference STRING attributes become `Unknown`; numeric, Boolean, DATE,
# MAGIC and TIMESTAMP attributes remain typed nulls. POS values and DQ fields are unchanged.
# MAGIC Match flags separately identify missing references, including nonempty orphan IDs.
# MAGIC
# MAGIC Preserve POS ingestion metadata and prefix reference ingestion metadata.
# MAGIC Manager emails describe the matched theater/screen; no screen match means
# MAGIC `Unknown` managers, not a guessed assignment. These fields do not enforce RLS.
# MAGIC
# MAGIC Run after all three Silver loads complete, without concurrent source writers.
# MAGIC Replay each POS source from its latest target ingestion date, inclusive,
# MAGIC before joining references. New sources and missing/empty targets load in full.
# MAGIC MERGE on `(transaction_id, pos_source)`; update only for a newer POS ingestion
# MAGIC timestamp and a different POS source-row hash, matching the Silver POS policy.
# MAGIC Cluster the target by `(transaction_id, ingestion_date)`.
# MAGIC Reference-only changes, earlier late arrivals, and source deletions need a
# MAGIC separate refresh/backfill; same-timestamp replay does not refresh enrichment.
# MAGIC Source key validation fails before writing if joins could multiply transactions.
# MAGIC This notebook does not read quarantine, cleanse POS again, or create Gold tables.

# COMMAND ----------

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

TARGET_TABLE = "theater_analytics.silver.denormalized_theater_sales"
SOURCE_TABLE = "theater_analytics.silver.pos_ticket_sales"
MERGE_KEYS = ["transaction_id", "pos_source"]
MERGE_UPDATE_CONDITION = """
    source.ingestion_timestamp > target.ingestion_timestamp
    AND source.source_row_hash <> target.source_row_hash
"""

DENORMALIZED_QUERY = """
SELECT
    s.*,
    s.revenue AS gross_revenue,
    COALESCE(l.theater_name, 'Unknown') AS theater_name,
    COALESCE(l.city, 'Unknown') AS city,
    COALESCE(l.state, 'Unknown') AS state,
    COALESCE(l.manager_email, 'Unknown') AS manager_email,
    COALESCE(l.zone_manager_email, 'Unknown') AS zone_manager_email,
    COALESCE(l.open_time, 'Unknown') AS open_time,
    COALESCE(l.close_time, 'Unknown') AS close_time,
    l.screen_number,
    COALESCE(l.screen_type, 'Unknown') AS screen_type,
    l.imax_capable,
    l.row_count,
    l.seats_per_row,
    l.total_seats,
    l.ingestion_timestamp AS layout_ingestion_timestamp,
    l.ingestion_date AS layout_ingestion_date,
    COALESCE(m.title, 'Unknown') AS movie_title,
    COALESCE(m.genre, 'Unknown') AS genre,
    m.runtime_minutes,
    COALESCE(m.mpaa_rating, 'Unknown') AS mpaa_rating,
    m.release_date,
    m.imdb_rating,
    m.metascore,
    m.ingestion_timestamp AS movie_ingestion_timestamp,
    m.ingestion_date AS movie_ingestion_date,
    (l.theater_id IS NOT NULL) AS has_layout_match,
    (m.imdb_id IS NOT NULL) AS has_movie_match
FROM pos_sales_incremental s
LEFT JOIN theater_analytics.silver.theater_layout l
    ON s.theater_id = l.theater_id AND s.screen_id = l.screen_id
LEFT JOIN theater_analytics.silver.imdb_movie_metadata m
    ON s.movie_id = m.imdb_id
"""


def validate_keys(data: DataFrame, keys: list[str], table: str) -> None:
    """Reject ambiguous or missing source keys instead of choosing arbitrary rows."""
    invalid_key = F.lit(False)
    for key in keys:
        invalid_key = (
            invalid_key | F.col(key).isNull() | (F.trim(F.col(key)) == "")
        )
    violations = (
        data.groupBy(*keys)
        .agg(F.count("*").alias("_count"))
        .where((F.col("_count") > 1) | invalid_key)
        .take(1)
    )
    if violations:
        raise ValueError(f"{table} has duplicate or missing keys {keys}: {violations}")


def run_load(spark: SparkSession) -> None:
    spark.conf.set("spark.sql.session.timeZone", "UTC")
    target_exists = spark.catalog.tableExists(TARGET_TABLE)
    target = spark.table(TARGET_TABLE) if target_exists else None
    if target is not None:
        validate_keys(target, MERGE_KEYS, TARGET_TABLE)

    sales = spark.table(SOURCE_TABLE)
    if target is not None:
        watermarks = target.groupBy("pos_source").agg(
            F.max("ingestion_date").alias("_max_ingestion_date")
        )
        sales = (
            sales.join(watermarks, on="pos_source", how="left")
            .where(
                F.col("_max_ingestion_date").isNull()
                | (F.col("ingestion_date") >= F.col("_max_ingestion_date"))
            )
            .drop("_max_ingestion_date")
        )
    validate_keys(sales, MERGE_KEYS, SOURCE_TABLE)
    sources = {
        "theater_analytics.silver.theater_layout": ["theater_id", "screen_id"],
        "theater_analytics.silver.imdb_movie_metadata": ["imdb_id"],
    }
    for table, keys in sources.items():
        validate_keys(spark.table(table), keys, table)

    sales.createOrReplaceTempView("pos_sales_incremental")
    try:
        combined = spark.sql(DENORMALIZED_QUERY)
        combined.createOrReplaceTempView("denormalized_sales_batch")
        spark.sql(f"""
            CREATE TABLE IF NOT EXISTS {TARGET_TABLE}
            USING DELTA
            CLUSTER BY (transaction_id, ingestion_date)
            AS SELECT * FROM denormalized_sales_batch LIMIT 0
        """)
        if target_exists:
            spark.sql(
                f"ALTER TABLE {TARGET_TABLE} CLUSTER BY (transaction_id, ingestion_date)"
            )
        spark.sql(f"""
            MERGE INTO {TARGET_TABLE} AS target
            USING denormalized_sales_batch AS source
            ON target.transaction_id = source.transaction_id
                AND target.pos_source = source.pos_source
            WHEN MATCHED AND ({MERGE_UPDATE_CONDITION}) THEN UPDATE SET *
            WHEN NOT MATCHED THEN INSERT *
        """)
    finally:
        spark.catalog.dropTempView("denormalized_sales_batch")
        spark.catalog.dropTempView("pos_sales_incremental")

    validate_keys(spark.table(TARGET_TABLE), MERGE_KEYS, TARGET_TABLE)
    print(f"Incrementally merged {TARGET_TABLE}; unmatched POS rows are retained.")
    print("Match coverage (POS data-quality flags remain unchanged):")
    spark.table(TARGET_TABLE).groupBy(
        "has_layout_match", "has_movie_match"
    ).count().orderBy("has_layout_match", "has_movie_match").show(truncate=False)


if __name__ == "__main__":
    run_load(SparkSession.builder.getOrCreate())
