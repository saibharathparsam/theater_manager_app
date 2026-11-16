# Databricks notebook source
# MAGIC %md
# MAGIC # Verify Gold access as the actual viewer
# MAGIC Run interactively as each test user, not as the owner or a run-as job.
# MAGIC Obtain expected counts and theater IDs from the owner's fixed-snapshot baseline.
# MAGIC The expected identity is an assertion, never an impersonation mechanism.
# MAGIC A permission error, missing policy, wrong identity, extra row or missing row fails.

# COMMAND ----------

import json

from theater_security import SALES_TABLE, verify_row_filter


def verify_access(spark, expected_user: str, expected_theaters: list[str], expected_rows: int) -> None:
    if not expected_user.strip():
        raise ValueError("Set expected_user to the real viewer's workspace identity.")
    if (
        not isinstance(expected_theaters, list)
        or any(not isinstance(item, str) or not item.strip() for item in expected_theaters)
        or type(expected_rows) is not int
        or expected_rows < 0
        or bool(expected_theaters) != bool(expected_rows)
    ):
        raise ValueError("Supply theater IDs as a JSON list and a matching nonnegative row count.")
    actual_user = spark.sql("SELECT current_user() AS viewer").first().viewer
    if actual_user.casefold() != expected_user.strip().casefold():
        raise RuntimeError(f"Wrong execution identity: expected {expected_user}, got {actual_user}.")
    verify_row_filter(spark)
    actual = spark.sql(f"""
        SELECT COUNT(*) AS visible_rows,
               collect_set(COALESCE(theater_id, '<NULL>')) AS theater_ids
        FROM {SALES_TABLE}
    """).first()
    if actual.visible_rows != expected_rows or set(actual.theater_ids) != set(expected_theaters):
        raise RuntimeError(
            f"Access mismatch for {actual_user}: expected {expected_rows} rows in "
            f"{sorted(set(expected_theaters))}; got {actual.visible_rows} rows in "
            f"{sorted(actual.theater_ids)}."
        )
    print(f"PASS: {actual_user}; {actual.visible_rows} rows; theaters {sorted(actual.theater_ids)}.")


if __name__ == "__main__":
    from pyspark.sql import SparkSession

    dbutils.widgets.text("expected_user", "")
    dbutils.widgets.text("expected_theater_ids_json", "[]")
    dbutils.widgets.text("expected_row_count", "")
    verify_access(
        SparkSession.builder.getOrCreate(),
        dbutils.widgets.get("expected_user"),
        json.loads(dbutils.widgets.get("expected_theater_ids_json")),
        int(dbutils.widgets.get("expected_row_count")),
    )
