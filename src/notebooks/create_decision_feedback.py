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
from theater_security import FEEDBACK_DDL

spark = SparkSession.builder.getOrCreate()
spark.sql("CREATE SCHEMA IF NOT EXISTS theater_analytics.gold")
spark.sql(FEEDBACK_DDL)
print("Ready: theater_analytics.gold.decision_feedback (existing records preserved).")
