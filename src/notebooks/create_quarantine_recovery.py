# Databricks notebook source
# MAGIC %md
# MAGIC # Provision the Operations correction ledger
# MAGIC Run once as owner before granting app access. Originals remain in quarantine.
# MAGIC The app may submit requests; only the job identity may restore Silver records.

# COMMAND ----------

from pyspark.sql import SparkSession
from theater_security import RECOVERY_DDL

spark = SparkSession.builder.getOrCreate()
spark.sql(RECOVERY_DDL)
