# Databricks notebook source
# MAGIC %md
# MAGIC # Bronze to Silver: Theater layout
# MAGIC
# MAGIC **Source:** `theater_analytics.bronze.theater_layout`
# MAGIC **Target:** `theater_analytics.silver.theater_layout`
# MAGIC
# MAGIC No type conversions are required by the current Bronze schema:
# MAGIC - IDs, names, locations, and manager emails are STRING.
# MAGIC - Screen numbers, row counts, seat counts, and capacity are INT.
# MAGIC - `imax_capable` is BOOLEAN.
# MAGIC - `ingestion_timestamp` is TIMESTAMP; `ingestion_date` is DATE.
# MAGIC - `open_time` and `close_time` are recurring `HH:MM` strings, not dated events.
# MAGIC   Preserve `00:00` as midnight; do not attach an arbitrary date or timezone.
# MAGIC
# MAGIC Copy the full Bronze snapshot without deduplication, enrichment, or
# MAGIC incremental logic. Preserve manager assignments and ingestion metadata.
# MAGIC These columns support future RLS; this notebook does not install row filters.
# MAGIC Run with Bronze read and Silver schema/table create and write permissions.

# COMMAND ----------

from pyspark.sql import SparkSession

SOURCE_TABLE = "theater_analytics.bronze.theater_layout"
TARGET_TABLE = "theater_analytics.silver.theater_layout"


def run_load(spark: SparkSession) -> None:
    silver = spark.table(SOURCE_TABLE)
    spark.sql("CREATE SCHEMA IF NOT EXISTS theater_analytics.silver")
    (
        silver.write.format("delta")
        .mode("overwrite")
        .option("overwriteSchema", "true")
        .saveAsTable(TARGET_TABLE)
    )
    print(f"Refreshed {TARGET_TABLE}; Bronze values and types are unchanged.")


if __name__ == "__main__":
    run_load(SparkSession.builder.getOrCreate())
