# Databricks notebook source
# MAGIC %md
# MAGIC # Grant Reel Insights App access to theater tables
# MAGIC
# MAGIC Run after creating the app resource and both Gold tables, before starting the app.
# MAGIC Execute it as a user who is allowed to grant privileges on these Unity Catalog objects.
# MAGIC
# MAGIC Set the app_service_principal_client_id widget to the app's client UUID.
# MAGIC The Gold sales table is read-only. SELECT/MODIFY is restricted to the feedback
# MAGIC table; MODIFY includes deletion, so app administrators must be trusted.
# MAGIC This notebook does not revoke legacy grants. Review those separately.

# COMMAND ----------

from pyspark.sql import SparkSession
from theater_security import app_principal, gold_grants

dbutils.widgets.text("app_service_principal_client_id", "")
principal = app_principal(dbutils.widgets.get("app_service_principal_client_id"))

spark = SparkSession.builder.getOrCreate()
for statement in gold_grants(principal):
    spark.sql(statement)

for table in ("theater_daily_sales", "decision_feedback"):
    spark.sql(f"SHOW GRANTS ON TABLE theater_analytics.gold.{table}").show(truncate=False)
