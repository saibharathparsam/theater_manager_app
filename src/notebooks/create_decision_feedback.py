# Databricks notebook source
# MAGIC %md
# MAGIC # Leadership decision feedback
# MAGIC Run once as the table owner before starting the Gold app.
# MAGIC This audit table is separate from the full-refresh Gold sales table and must
# MAGIC never be overwritten by the Gold pipeline. Approval inserts only; decision IDs
# MAGIC make retries of the same app review idempotent. A new review is a new decision.
# MAGIC `theater_id = ''` means all theaters, not an individual theater.
# MAGIC Evidence stores the exact displayed period, aggregates, score, movie, and action.

# COMMAND ----------

from pyspark.sql import SparkSession

spark = SparkSession.builder.getOrCreate()
spark.sql("CREATE SCHEMA IF NOT EXISTS theater_analytics.gold")
spark.sql("""
    CREATE TABLE IF NOT EXISTS theater_analytics.gold.decision_feedback (
        decision_id STRING NOT NULL,
        movie_id STRING NOT NULL,
        movie_title STRING NOT NULL,
        action STRING NOT NULL,
        theater_id STRING NOT NULL,
        period_start DATE NOT NULL,
        period_end DATE NOT NULL,
        approver_email STRING NOT NULL,
        approved_at TIMESTAMP NOT NULL,
        approval_note STRING NOT NULL,
        source_table STRING NOT NULL,
        evidence_json STRING NOT NULL
    ) USING DELTA
""")
print("Ready: theater_analytics.gold.decision_feedback (existing records preserved).")
