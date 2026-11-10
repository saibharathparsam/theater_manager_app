# Databricks notebook source
# MAGIC %md
# MAGIC # Grant Reel Insights App access to theater tables
# MAGIC
# MAGIC Run this notebook after deploying the app and creating the `workspace.fde_theater` schema.
# MAGIC Execute it as a user who is allowed to grant privileges on these Unity Catalog objects.
# MAGIC
# MAGIC The schema-level `SELECT`, `INSERT`, and `UPDATE` grants apply to current and future
# MAGIC tables in the schema. `INSERT` and `UPDATE` are fine-grained DML privileges currently
# MAGIC documented by Databricks as Beta; confirm they are enabled in your workspace.
# MAGIC They avoid granting `MODIFY`, which also allows deletes.

# COMMAND ----------

# MAGIC %sql
# MAGIC GRANT USE CATALOG ON CATALOG workspace
# MAGIC TO `e44e0047-b327-4a29-bcf2-809a85ff2811`;

# COMMAND ----------

# MAGIC %sql
# MAGIC GRANT USE SCHEMA ON SCHEMA workspace.fde_theater
# MAGIC TO `e44e0047-b327-4a29-bcf2-809a85ff2811`;

# COMMAND ----------

# MAGIC %sql
# MAGIC GRANT SELECT ON SCHEMA workspace.fde_theater
# MAGIC TO `e44e0047-b327-4a29-bcf2-809a85ff2811`;

# COMMAND ----------

# MAGIC %sql
# MAGIC GRANT INSERT, UPDATE ON SCHEMA workspace.fde_theater
# MAGIC TO `e44e0047-b327-4a29-bcf2-809a85ff2811`;

# COMMAND ----------

# MAGIC %sql
# MAGIC SHOW GRANTS ON CATALOG workspace;

# COMMAND ----------

# MAGIC %sql
# MAGIC SHOW GRANTS ON SCHEMA workspace.fde_theater;
