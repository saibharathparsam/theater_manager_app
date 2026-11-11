# Databricks notebook source
# MAGIC %md
# MAGIC # Grant Reel Insights App access to theater tables
# MAGIC
# MAGIC Run after creating the app resource and both Gold tables, before starting the app.
# MAGIC Execute it as a user who is allowed to grant privileges on these Unity Catalog objects.
# MAGIC
# MAGIC The app's service-principal client ID is hardcoded below. Update it if
# MAGIC the app is recreated or deployed in another workspace.
# MAGIC The Gold sales table is read-only. SELECT/MODIFY is restricted to the feedback
# MAGIC table; MODIFY includes deletion, so app administrators must be trusted.
# MAGIC This notebook does not revoke legacy grants. Review those separately.

# COMMAND ----------

from pyspark.sql import SparkSession

spark = SparkSession.builder.getOrCreate()
APP_SERVICE_PRINCIPAL = "e44e0047-b327-4a29-bcf2-809a85ff2811"
quoted_principal = f"`{APP_SERVICE_PRINCIPAL}`"

for statement in (
    "GRANT USE CATALOG ON CATALOG theater_analytics",
    "GRANT USE SCHEMA ON SCHEMA theater_analytics.gold",
    "GRANT SELECT ON TABLE theater_analytics.gold.theater_daily_sales",
    "GRANT SELECT, MODIFY ON TABLE theater_analytics.gold.decision_feedback",
):
    spark.sql(f"{statement} TO {quoted_principal}")

for table in ("theater_daily_sales", "decision_feedback"):
    spark.sql(f"SHOW GRANTS ON TABLE theater_analytics.gold.{table}").show(truncate=False)
