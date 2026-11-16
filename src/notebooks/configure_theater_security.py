# Databricks notebook source
# MAGIC %md
# MAGIC # Owner-controlled theater security
# MAGIC Default `mode=inspect` only reads metadata and prints the proposed SQL.
# MAGIC Apply requires an explicit app client UUID and existing Gold/quarantine tables.
# MAGIC Existing ledger records and memberships are preserved; no users are enrolled.
# MAGIC Review existing policies/grants first: apply replaces the named filter function
# MAGIC and attachment, but never revokes existing privileges.

# COMMAND ----------

from pyspark.sql import SparkSession

from theater_security import configure_security

dbutils.widgets.dropdown("mode", "inspect", ["inspect", "apply"])
dbutils.widgets.text("app_service_principal_client_id", "")
dbutils.widgets.text("viewer_principal", "")
configure_security(
    SparkSession.builder.getOrCreate(),
    mode=dbutils.widgets.get("mode"),
    client_id=dbutils.widgets.get("app_service_principal_client_id"),
    viewer_principal=dbutils.widgets.get("viewer_principal"),
)
