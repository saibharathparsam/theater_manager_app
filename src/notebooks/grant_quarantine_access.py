# Databricks notebook source
# MAGIC %md
# MAGIC # Grant the app Operations queue access
# MAGIC Run as owner after provisioning the ledger and quarantine tables.
# MAGIC The app does not receive MODIFY on quarantine or Silver POS.
# MAGIC Unity Catalog MODIFY on the ledger permits updates/deletes too: trust app
# MAGIC administrators and restrict direct ledger grants. Actor names in the ledger
# MAGIC are proxy-authenticated app attribution, not an immutable platform audit log.
# MAGIC Set app_service_principal_client_id to the app's client UUID before running.

# COMMAND ----------

from pyspark.sql import SparkSession
from theater_security import app_principal, quarantine_grants

dbutils.widgets.text("app_service_principal_client_id", "")
principal = app_principal(dbutils.widgets.get("app_service_principal_client_id"))
spark = SparkSession.builder.getOrCreate()
for statement in quarantine_grants(principal):
    spark.sql(statement)
