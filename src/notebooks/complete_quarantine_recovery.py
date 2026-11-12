# Databricks notebook source
# MAGIC %md
# MAGIC # Confirm downstream publication
# MAGIC Runs only after combined Silver and Gold succeed. A failed downstream task
# MAGIC leaves APPLIED requests visible in history; retry the job to finish publishing.

# COMMAND ----------

from pyspark.sql import SparkSession

spark = SparkSession.builder.getOrCreate()
requests = spark.sql("""
    SELECT request_id FROM theater_analytics.silver.quarantine_recovery_requests
    WHERE status = 'APPLIED' ORDER BY submitted_at, request_id
""").toLocalIterator()
for request in requests:
    request_id = request.request_id
    for table in (
        "theater_analytics.silver.pos_ticket_sales",
        "theater_analytics.silver.denormalized_theater_sales",
    ):
        verified = spark.sql(
            f"""SELECT s.source_row_hash
                FROM {table} s
                JOIN theater_analytics.silver.quarantine_recovery_requests r
                  ON s.transaction_id = r.restored_transaction_id
                 AND s.pos_source = r.restored_pos_source
                 AND s.source_row_hash = r.restored_source_row_hash
                WHERE r.request_id = :request_id AND r.status = 'APPLIED'""",
            args={"request_id": request_id},
        ).take(2)
        if len(verified) != 1:
            raise RuntimeError(f"Recovery {request_id} is not present exactly once in {table}.")
    spark.sql(
        """UPDATE theater_analytics.silver.quarantine_recovery_requests
           SET status = 'COMPLETED', completed_at = current_timestamp()
           WHERE request_id = :request_id AND status = 'APPLIED'""",
        args={"request_id": request_id},
    )
