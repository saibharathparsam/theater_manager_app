# Databricks notebook source
# MAGIC %md
# MAGIC # Provision the Operations correction ledger
# MAGIC Run once as owner before granting app access. Originals remain in quarantine.
# MAGIC The app may submit requests; only the job identity may restore Silver records.

# COMMAND ----------

from pyspark.sql import SparkSession

spark = SparkSession.builder.getOrCreate()
spark.sql("""
    CREATE TABLE IF NOT EXISTS theater_analytics.silver.quarantine_recovery_requests (
        request_id STRING NOT NULL,
        quarantine_hash STRING NOT NULL,
        corrections_json STRING NOT NULL,
        submitted_by STRING NOT NULL,
        submitted_at TIMESTAMP NOT NULL,
        reason STRING NOT NULL,
        status STRING NOT NULL,
        original_record_json STRING,
        last_error STRING,
        processed_at TIMESTAMP,
        restored_transaction_id STRING,
        restored_pos_source STRING,
        restored_source_row_hash STRING,
        completed_at TIMESTAMP,
        job_run_id BIGINT
    ) USING DELTA
    COMMENT 'Audited quarantine corrections: SUBMITTED -> APPLIED -> COMPLETED, or REJECTED'
""")
