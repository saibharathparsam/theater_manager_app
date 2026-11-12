# Databricks notebook source
# MAGIC %md
# MAGIC # Bronze to Silver: IMDb movie metadata
# MAGIC
# MAGIC **Source:** `theater_analytics.bronze.imdb_movie_metadata`
# MAGIC **Target:** `theater_analytics.silver.imdb_movie_metadata`
# MAGIC
# MAGIC Convert `release_date` from ISO `yyyy-MM-dd` STRING to DATE.
# MAGIC IDs, titles, genres, and MPAA ratings stay STRING; numeric fields already have
# MAGIC appropriate INT/DOUBLE types. Preserve ingestion metadata unchanged.
# MAGIC
# MAGIC Bronze is a full-refresh snapshot, so replace Silver with the current
# MAGIC snapshot on every run. No deduplication, enrichment, or incremental logic.
# MAGIC Run with Bronze read and Silver schema/table create and write permissions.

# COMMAND ----------

from pyspark.sql import SparkSession
from pyspark.sql import functions as F

SOURCE_TABLE = "theater_analytics.bronze.imdb_movie_metadata"
TARGET_TABLE = "theater_analytics.silver.imdb_movie_metadata"


def run_load(spark: SparkSession) -> None:
    bronze = spark.table(SOURCE_TABLE)
    silver = bronze.withColumn("release_date", F.to_date("release_date", "yyyy-MM-dd"))

    # Fail explicitly if a permissive Spark session turns a non-null date into null.
    # Strict sessions already raise a parsing error for malformed dates.
    invalid_dates = bronze.where(
        F.col("release_date").isNotNull()
        & F.to_date("release_date", "yyyy-MM-dd").isNull()
    )
    if invalid_dates.take(1):
        raise ValueError("IMDb release_date contains values outside yyyy-MM-dd format.")

    spark.sql("CREATE SCHEMA IF NOT EXISTS theater_analytics.silver")
    (
        silver.write.format("delta")
        .mode("overwrite")
        .option("overwriteSchema", "true")
        .saveAsTable(TARGET_TABLE)
    )
    print(f"Refreshed {TARGET_TABLE}; release_date is DATE.")


if __name__ == "__main__":
    run_load(SparkSession.builder.getOrCreate())
