# Databricks notebook source
# MAGIC %md
# MAGIC # Grant the app Operations queue access
# MAGIC Run as owner after provisioning the ledger and quarantine tables.
# MAGIC The app does not receive MODIFY on quarantine or Silver POS.
# MAGIC Unity Catalog MODIFY on the ledger permits updates/deletes too: trust app
# MAGIC administrators and restrict direct ledger grants. Actor names in the ledger
# MAGIC are proxy-authenticated app attribution, not an immutable platform audit log.

# COMMAND ----------

from pyspark.sql import SparkSession

spark = SparkSession.builder.getOrCreate()
APP_SERVICE_PRINCIPAL = "e44e0047-b327-4a29-bcf2-809a85ff2811"
for statement in (
    "GRANT USE CATALOG ON CATALOG theater_analytics",
    "GRANT USE SCHEMA ON SCHEMA theater_analytics.silver",
    "GRANT SELECT ON TABLE theater_analytics.silver.pos_ticket_sales_quarantine",
    "GRANT SELECT, MODIFY ON TABLE theater_analytics.silver.quarantine_recovery_requests",
):
    spark.sql(f"{statement} TO `{APP_SERVICE_PRINCIPAL}`")
