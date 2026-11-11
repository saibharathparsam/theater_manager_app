# Databricks notebook source
# MAGIC %md
# MAGIC # Bronze to Silver: POS ticket sales
# MAGIC
# MAGIC **Source:** `theater_analytics.bronze.pos_ticket_sales`
# MAGIC **Target:** `theater_analytics.silver.pos_ticket_sales`
# MAGIC
# MAGIC ## Data flow
# MAGIC 1. Filter Bronze from each POS source's latest Silver ingestion date, inclusive.
# MAGIC    Load full history for new sources or a missing/empty Silver table.
# MAGIC 2. Parse modern and legacy timestamps, derive `purchase_date`, and calculate
# MAGIC    `revenue = seats_sold * ticket_price`. Preserve Bronze ingestion metadata.
# MAGIC 3. Quarantine missing/blank transaction IDs or POS sources and missing/invalid
# MAGIC    purchase timestamps, plus invalid ticket prices (null, NaN, infinite, or
# MAGIC    nonpositive), in `theater_analytics.silver.pos_ticket_sales_quarantine`.
# MAGIC    Retain other issues in Silver with a Boolean flag and an array of reasons.
# MAGIC    Successfully normalized legacy formats are also flagged.
# MAGIC 4. Keep one row per `(transaction_id, pos_source)`: latest ingestion timestamp,
# MAGIC    then greatest source-row hash for ties. MERGE on both columns; update only
# MAGIC    when the ingestion timestamp is newer and the hash differs.
# MAGIC 5. Before writing either output, replace null non-key STRING values with
# MAGIC    `Unknown`. Preserve transaction ID/POS source and all non-STRING nulls.
# MAGIC    DQ reasons and source hashes reflect the values before replacement.
# MAGIC
# MAGIC ## Operating notes
# MAGIC - Use Databricks Runtime 14.3 LTS+ or serverless with Bronze read access and
# MAGIC   Silver schema/table create and write permissions. Run one writer at a time.
# MAGIC - Interpret timezone-free timestamps as UTC; trust Bronze ingestion metadata.
# MAGIC - Cluster Silver by `(transaction_id, ingestion_date)`.
# MAGIC - Quarantine is insert-only history, not an open-issue queue.
# MAGIC - Price rejection applies to incoming/replayed Bronze only. Existing Silver
# MAGIC   rows are not removed or migrated, even if a replay also quarantines them.
# MAGIC - Rows before the replay date and source deletions need separate backfill/CDC.
# MAGIC   Changes to either key column insert new rows; equal-timestamp changes do not update.
# MAGIC - Earlier transaction-ID-only loads may have collapsed different POS sources.
# MAGIC   Full Bronze replay/backfill is needed to recover rows outside the replay window
# MAGIC   and recompute historical duplicate flags; this notebook does not migrate them.

# COMMAND ----------

from pyspark.sql import Column, DataFrame, SparkSession, Window
from pyspark.sql import functions as F

SOURCE_TABLE = "theater_analytics.bronze.pos_ticket_sales"
TARGET_TABLE = "theater_analytics.silver.pos_ticket_sales"
QUARANTINE_TABLE = "theater_analytics.silver.pos_ticket_sales_quarantine"
MERGE_KEYS = ["transaction_id", "pos_source"]
LEGACY_TIMESTAMP_FORMAT = "MM/dd/yyyy hh:mm a"
MERGE_UPDATE_CONDITION = """
    source.ingestion_timestamp > target.ingestion_timestamp
    AND source.source_row_hash <> target.source_row_hash
"""


def parse_timestamp(column: str) -> Column:
    """Build a native Spark expression; malformed values become null for DQ checks."""
    text = F.trim(F.col(column))
    return F.when(
        text.contains("/"),
        F.try_to_timestamp(text, F.lit(LEGACY_TIMESTAMP_FORMAT)),
    ).otherwise(
        F.coalesce(
            F.try_to_timestamp(text, F.lit("yyyy-MM-dd'T'HH:mm:ss[.SSSSSSSSS][XXX]")),
            F.try_to_timestamp(text, F.lit("yyyy-MM-dd HH:mm:ss[.SSSSSSSSS][XXX]")),
        )
    )


def missing(column: str) -> Column:
    return F.col(column).isNull() | (F.trim(F.col(column)) == "")


def normalize_sales(bronze: DataFrame) -> DataFrame:
    """Keep raw timestamps and ingestion metadata alongside normalized sales values."""
    required = {
        "transaction_id",
        "theater_id",
        "screen_id",
        "movie_id",
        "pos_source",
        "purchase_timestamp",
        "showtime",
        "seats_sold",
        "ticket_price",
    }
    absent = required - set(bronze.columns)
    if absent:
        raise ValueError(f"Bronze table is missing required columns: {sorted(absent)}")

    # Hash the original row in a fixed column order, before changing timestamps.
    # Ingestion metadata is intentionally included: a reingested row is a new
    # source version even when its business values have not changed.
    sales = (
        bronze.withColumn(
            "source_row_hash",
            F.sha2(
                F.to_json(
                    F.struct(*[F.col(name) for name in sorted(bronze.columns)]),
                    options={"ignoreNullFields": "false"},
                ),
                256,
            ),
        )
        .withColumn("purchase_timestamp_raw", F.col("purchase_timestamp"))
        .withColumn("showtime_raw", F.col("showtime"))
        .withColumn("purchase_timestamp", parse_timestamp("purchase_timestamp"))
        .withColumn("showtime", parse_timestamp("showtime"))
        .withColumn("purchase_date", F.to_date("purchase_timestamp"))
        .withColumn(
            "revenue",
            F.expr("try_cast(seats_sold * ticket_price AS DECIMAL(18,2))"),
        )
    )

    rules = {
        "missing_transaction_id": missing("transaction_id"),
        "missing_pos_source": missing("pos_source"),
        "missing_theater_id": missing("theater_id"),
        "missing_movie_id": missing("movie_id"),
        "missing_screen_id": missing("screen_id"),
        "invalid_purchase_timestamp": F.col("purchase_timestamp").isNull(),
        "missing_showtime": missing("showtime_raw"),
        "invalid_showtime": ~missing("showtime_raw") & F.col("showtime").isNull(),
        "invalid_ticket_price": (
            F.col("ticket_price").isNull()
            | F.isnan("ticket_price")
            | (F.abs(F.col("ticket_price")) == float("inf"))
            | (F.col("ticket_price") <= 0)
        ),
        "invalid_seats_sold": F.col("seats_sold").isNull() | (F.col("seats_sold") <= 0),
        "legacy_purchase_timestamp_format": F.col("purchase_timestamp_raw").contains(
            "/"
        ),
        "legacy_showtime_format": F.col("showtime_raw").contains("/"),
        "invalid_revenue": F.col("revenue").isNull(),
    }
    # This lambda constructs a Spark higher-order expression, not a Python UDF.
    # Failed rules produce reason strings; null entries for passing rules are removed.
    return sales.withColumn(
        "data_quality_issues",
        F.filter(
            F.array(
                *[
                    F.when(condition, F.lit(reason))
                    for reason, condition in rules.items()
                ]
            ),
            lambda reason: reason.isNotNull(),
        ),
    ).withColumn(
        "has_data_quality_issue",
        F.size("data_quality_issues") > 0,
    )


def invalid_merge_key() -> Column:
    """Identify rows excluded from Silver under the current quality policy.

    Both transaction_id and pos_source form the MERGE key. Parsed purchase time
    is also required for the serving dataset; rejected rows go to quarantine.
    """
    return (
        missing("transaction_id")
        | missing("pos_source")
        | F.col("purchase_timestamp").isNull()
    )


def should_quarantine() -> Column:
    """Reject required-value failures and prices already classified by the DQ rules."""
    return invalid_merge_key() | F.array_contains(
        F.col("data_quality_issues"), "invalid_ticket_price"
    )


def fill_unknown_strings(sales: DataFrame) -> DataFrame:
    """Fill display values only after quality checks, routing, and deduplication."""
    columns = [
        name
        for name, dtype in sales.dtypes
        if dtype == "string" and name not in MERGE_KEYS
    ]
    return sales.fillna("Unknown", subset=columns)


def select_incremental(sales: DataFrame, silver: DataFrame | None) -> DataFrame:
    """Replay the latest ingestion day per source, including new sources in full."""
    if silver is None:
        return sales
    watermarks = silver.groupBy("pos_source").agg(
        F.max("ingestion_date").alias("_max_ingestion_date")
    )
    # A left join keeps new POS sources whose watermark does not exist yet.
    # Use >= on the date, not > on the timestamp, to replay same-day arrivals.
    return (
        sales.join(watermarks, on="pos_source", how="left")
        .where(
            F.col("_max_ingestion_date").isNull()
            | (F.col("ingestion_date") >= F.col("_max_ingestion_date"))
        )
        .drop("_max_ingestion_date")
    )


def deduplicate_sales(sales: DataFrame) -> DataFrame:
    """Choose one version per transaction/POS source pair before MERGE."""
    latest = Window.partitionBy(*MERGE_KEYS).orderBy(
        F.col("ingestion_timestamp").desc(), F.col("source_row_hash").desc()
    )
    # Both windows share partitioning and ordering. The full frame counts all
    # versions, not just those preceding the winning row. Hash ordering breaks
    # ingestion-time ties deterministically; it does not imply business recency.
    all_duplicates = latest.rowsBetween(
        Window.unboundedPreceding, Window.unboundedFollowing
    )
    return (
        sales.withColumn("_duplicate_count", F.count("*").over(all_duplicates))
        .withColumn("_rank", F.row_number().over(latest))
        .where(F.col("_rank") == 1)
        .withColumn(
            "data_quality_issues",
            F.when(
                F.col("_duplicate_count") > 1,
                F.array_union(
                    "data_quality_issues", F.array(F.lit("duplicate_merge_key"))
                ),
            ).otherwise(F.col("data_quality_issues")),
        )
        .withColumn(
            "has_data_quality_issue",
            F.size("data_quality_issues") > 0,
        )
        .drop("_duplicate_count", "_rank")
    )


# COMMAND ----------

# MAGIC %md
# MAGIC ## Incremental load and validation
# MAGIC The existing Silver table is never overwritten. The quarantine MERGE is
# MAGIC insert-only by source-row hash; the Silver MERGE updates/inserts by the
# MAGIC `(transaction_id, pos_source)` pair. Updates require a newer ingestion timestamp and a different
# MAGIC source-row hash; equal-timestamp candidates do not overwrite Silver rows.
# MAGIC `CLUSTER BY (transaction_id, ingestion_date)` configures liquid clustering,
# MAGIC not uniqueness. Existing old-schema tables require an explicit migration.
# MAGIC Duplicate and null-key checks below enforce the logical merge-key invariant.

# COMMAND ----------


def validate_silver(silver: DataFrame) -> None:
    """Check required values and uniqueness in one distributed aggregation/action."""
    violations = (
        silver.groupBy(*MERGE_KEYS)
        .agg(
            F.count("*").alias("_key_count"),
            F.max(
                F.when(
                    invalid_merge_key() | F.col("purchase_date").isNull(), 1
                ).otherwise(0)
            ).alias("_invalid"),
        )
        .where((F.col("_key_count") > 1) | (F.col("_invalid") == 1))
        .take(1)
    )
    if violations:
        raise ValueError(
            "Silver contains invalid required values or duplicate "
            "(transaction_id, pos_source) keys; resolve them before loading."
        )


def run_load(spark: SparkSession) -> None:
    spark.conf.set("spark.sql.session.timeZone", "UTC")

    silver_exists = spark.catalog.tableExists(TARGET_TABLE)
    silver = spark.table(TARGET_TABLE) if silver_exists else None
    if silver is not None:
        validate_silver(silver)
        print("Replaying each POS source from its latest Silver ingestion date.")
    else:
        print("Silver does not exist: loading all Bronze rows.")

    # Filter raw Bronze by source/ingestion date before parsing or hashing.
    # Both output branches use this replay window, including rejected records.
    incremental_bronze = select_incremental(spark.table(SOURCE_TABLE), silver)
    normalized = normalize_sales(incremental_bronze)
    quarantined = fill_unknown_strings(
        normalized.where(should_quarantine()).dropDuplicates(["source_row_hash"])
    )
    candidates = normalized.where(~should_quarantine())
    batch = fill_unknown_strings(deduplicate_sales(candidates))

    spark.sql("CREATE SCHEMA IF NOT EXISTS theater_analytics.silver")
    quarantined.createOrReplaceTempView("pos_sales_quarantine_batch")
    spark.sql(f"""
        CREATE TABLE IF NOT EXISTS {QUARANTINE_TABLE}
        USING DELTA AS SELECT * FROM pos_sales_quarantine_batch LIMIT 0
    """)
    batch.createOrReplaceTempView("pos_sales_silver_batch")
    spark.sql(f"""
        CREATE TABLE IF NOT EXISTS {TARGET_TABLE}
        USING DELTA
        CLUSTER BY (transaction_id, ingestion_date)
        AS SELECT * FROM pos_sales_silver_batch LIMIT 0
    """)
    if silver_exists:
        spark.sql(
            f"ALTER TABLE {TARGET_TABLE} CLUSTER BY (transaction_id, ingestion_date)"
        )
    # Each MERGE is atomic, but the pair is not one transaction. Insert-only
    # quarantine and deterministic Silver keys make retrying the pair safe.
    # Do not add ingestion_date to ON: later ingestions update the same key pair.
    try:
        spark.sql(f"""
            MERGE INTO {QUARANTINE_TABLE} AS target
            USING pos_sales_quarantine_batch AS source
            ON target.source_row_hash = source.source_row_hash
            WHEN NOT MATCHED THEN INSERT *
        """)
        spark.sql(f"""
            MERGE INTO {TARGET_TABLE} AS target
            USING pos_sales_silver_batch AS source
            ON target.transaction_id = source.transaction_id
                AND target.pos_source = source.pos_source
            WHEN MATCHED AND ({MERGE_UPDATE_CONDITION}) THEN UPDATE SET *
            WHEN NOT MATCHED THEN INSERT *
        """)
    finally:
        # Cleanup does not swallow errors: failed writes still fail the notebook.
        spark.catalog.dropTempView("pos_sales_silver_batch")
        spark.catalog.dropTempView("pos_sales_quarantine_batch")

    result = spark.table(TARGET_TABLE)
    validate_silver(result)
    print("Silver row counts by POS source and data-quality flag:")
    result.groupBy("pos_source", "has_data_quality_issue").count().orderBy(
        "pos_source", "has_data_quality_issue"
    ).show(truncate=False)


if __name__ == "__main__":
    run_load(SparkSession.builder.getOrCreate())
