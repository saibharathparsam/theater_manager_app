# Databricks notebook source
# MAGIC %md
# MAGIC # Load Silver POS, then process operator corrections
# MAGIC Share the existing single-writer job and the exact normalizer/quality policy.
# MAGIC A request never overwrites an existing Silver key. Original ingestion metadata
# MAGIC is preserved, so recovery cannot advance Bronze replay watermarks.

# COMMAND ----------

# MAGIC %run ./build_silver_pos_ticket_sales

# COMMAND ----------

import json
import hashlib
import logging
import os
import sys

from pyspark.sql import functions as F

sys.path.insert(0, os.path.abspath("../app"))
from quarantine import REQUEST_TABLE, serialize_record, validate_patch

logger = logging.getLogger("quarantine_recovery")


def reject_request(spark, request_id: str, message: str) -> None:
    logger.warning("Rejecting quarantine request %s: %s", request_id, message)
    spark.sql(
        f"""UPDATE {REQUEST_TABLE}
            SET status = 'REJECTED', last_error = :error, processed_at = current_timestamp()
            WHERE request_id = :request_id AND status = 'SUBMITTED'""",
        args={"request_id": request_id, "error": message},
    )


def publish_recovery_batch(spark, applied) -> None:
    dbutils.jobs.taskValues.set(key="recovery_applied", value=bool(applied))
    validate_silver(spark.table(TARGET_TABLE))


def process_requests(spark) -> None:
    spark.conf.set("spark.sql.session.timeZone", "UTC")
    if not spark.catalog.tableExists(REQUEST_TABLE):
        logger.warning(
            "Operations recovery is disabled: provision quarantine_recovery_requests. "
            "Normal Silver ingestion completed; no recovery requests were processed."
        )
        publish_recovery_batch(spark, [])
        return
    duplicate_ids = (
        spark.table(REQUEST_TABLE).groupBy("request_id").count().where("count > 1").take(1)
    )
    if duplicate_ids:
        raise RuntimeError("Duplicate correction request IDs found; resolve the ledger before processing.")
    requests = (
        spark.table(REQUEST_TABLE).where("status = 'SUBMITTED'")
        .orderBy("submitted_at", "request_id").toLocalIterator()
    )
    bronze_schema = spark.table(SOURCE_TABLE).schema
    for request in requests:
        request_id = request.request_id
        try:
            patch = validate_patch(json.loads(request.corrections_json))
        except (ValueError, TypeError) as error:
            reject_request(spark, request_id, str(error))
            continue
        originals = (
            spark.table(QUARANTINE_TABLE)
            .where(F.col("source_row_hash") == request.quarantine_hash).take(2)
        )
        if len(originals) != 1:
            reject_request(spark, request_id, "Original quarantine version is missing or ambiguous.")
            continue
        original = originals[0].asDict()
        original_json = serialize_record(original)
        spark.sql(
            f"""UPDATE {REQUEST_TABLE} SET original_record_json = :original
                WHERE request_id = :request_id AND status = 'SUBMITTED'""",
            args={"request_id": request_id, "original": original_json},
        )
        # Reconstruct the raw input, not already parsed timestamps or DQ columns.
        raw_columns = []
        for field in bronze_schema:
            source = {"purchase_timestamp": "purchase_timestamp_raw", "showtime": "showtime_raw"}.get(
                field.name, field.name
            )
            value = F.col(source)
            if field.name in patch:
                value = F.lit(patch[field.name]).cast(field.dataType)
            raw_columns.append(value.alias(field.name))
        raw = spark.table(QUARANTINE_TABLE).where(
            F.col("source_row_hash") == request.quarantine_hash
        ).select(*raw_columns)
        normalized = normalize_sales(raw)
        if normalized.where(should_quarantine()).take(1):
            reject_request(spark, request_id, "Corrected values still fail Silver admission rules.")
            continue
        # Recovery is stricter than normal ingestion: do not restore other invalid
        # measures, unparseable showtimes, or unknown reference relationships.
        invalid = normalized.where(
            F.col("showtime").isNull()
            | F.array_contains("data_quality_issues", "invalid_seats_sold")
            | F.array_contains("data_quality_issues", "invalid_revenue")
        ).take(1)
        if invalid:
            reject_request(spark, request_id, "Correct showtime, seats sold, and revenue before restoring.")
            continue
        row = normalized.first()
        layout_matches = spark.table("theater_analytics.silver.theater_layout").where(
            (F.col("theater_id") == row.theater_id) & (F.col("screen_id") == row.screen_id)
        ).take(2)
        movie_matches = spark.table("theater_analytics.silver.imdb_movie_metadata").where(
            F.col("imdb_id") == row.movie_id
        ).take(2)
        if len(layout_matches) != 1 or len(movie_matches) != 1:
            reject_request(spark, request_id, "Movie and theater/screen must each match exactly one Silver reference.")
            continue
        recovery_hash = hashlib.sha256(
            f"{request_id}:{row.source_row_hash}".encode("utf-8")
        ).hexdigest()
        batch = fill_unknown_strings(normalized).withColumn("source_row_hash", F.lit(recovery_hash))
        batch = batch.select(*spark.table(TARGET_TABLE).columns)
        key_filter = (
            (F.col("transaction_id") == row.transaction_id)
            & (F.col("pos_source") == row.pos_source)
        )
        existing = spark.table(TARGET_TABLE).where(key_filter).take(2)
        if existing and (len(existing) != 1 or existing[0].source_row_hash != recovery_hash):
            reject_request(spark, request_id, "Silver key already exists. Review the conflict; recovery never overwrites sales.")
            continue
        resolved = spark.table(REQUEST_TABLE).where(
            (F.col("quarantine_hash") == request.quarantine_hash)
            & F.col("status").isin("APPLIED", "COMPLETED")
            & (F.col("request_id") != request_id)
        ).take(1)
        if resolved:
            reject_request(spark, request_id, "This quarantine version was already restored by another request.")
            continue
        batch.createOrReplaceTempView("quarantine_recovery_batch")
        try:
            spark.sql(f"""
                MERGE INTO {TARGET_TABLE} t USING quarantine_recovery_batch s
                ON t.transaction_id = s.transaction_id AND t.pos_source = s.pos_source
                WHEN NOT MATCHED THEN INSERT *
            """)
        finally:
            spark.catalog.dropTempView("quarantine_recovery_batch")
        restored = spark.table(TARGET_TABLE).where(key_filter).take(2)
        if len(restored) != 1 or restored[0].source_row_hash != recovery_hash:
            raise RuntimeError(f"Silver restoration could not be verified for request {request_id}. Retry the job.")
        spark.sql(
            f"""UPDATE {REQUEST_TABLE}
                SET status = 'APPLIED', last_error = NULL, processed_at = current_timestamp(),
                    restored_transaction_id = :transaction_id, restored_pos_source = :pos_source,
                    restored_source_row_hash = :restored_hash
                WHERE request_id = :request_id AND status = 'SUBMITTED'""",
            args={
                "request_id": request_id, "transaction_id": row.transaction_id,
                "pos_source": row.pos_source, "restored_hash": recovery_hash,
            },
        )
    applied = spark.table(REQUEST_TABLE).where("status = 'APPLIED'").take(1)
    publish_recovery_batch(spark, applied)


spark = SparkSession.builder.getOrCreate()
process_requests(spark)
