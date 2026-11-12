import ast
import hashlib
import json
import logging
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "app"))
import quarantine


class QuarantineApiTests(unittest.TestCase):
    def setUp(self):
        self.email = "operator@example.com"
        self.request_id = str(uuid4())
        self.patch_text = '{"ticket_price": 14.5}'
        self.parameters = {
            "request_id": self.request_id, "quarantine_hash": "hash",
            "corrections_json": '{"ticket_price":14.5}',
            "submitted_by": self.email, "reason": "Verified receipt",
        }
        self.connection = MagicMock()
        self.cursor = self.connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        self.client = MagicMock()
        self.client.jobs.run_now.return_value.response.run_id = 123
        self.auth = patch.object(quarantine, "OPERATORS", {self.email})
        self.auth.start()
        self.addCleanup(self.auth.stop)

    def submit(self, **overrides):
        values = {
            "email": self.email, "request_id": self.request_id, "quarantine_hash": "hash",
            "patch_text": self.patch_text, "reason": "Verified receipt", "confirmed": True,
        }
        values.update(overrides)
        return quarantine.submit_request(**values)

    def test_patch_restricts_types_and_immutable_fields(self):
        for value in (
            {}, [], {"source_row_hash": "tamper"}, {"ingestion_date": "2026-01-01"},
            {"ticket_price": 0}, {"ticket_price": -1}, {"ticket_price": float("nan")},
            {"ticket_price": float("inf")}, {"ticket_price": True}, {"ticket_price": "14"},
            {"ticket_price": 10 ** 500},
            {"seats_sold": 0}, {"seats_sold": 1.5}, {"seats_sold": True},
            {"seats_sold": 2147483648}, {"transaction_id": ""}, {"pos_source": None},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                quarantine.validate_patch(value)
        self.assertEqual(quarantine.validate_patch({
            "ticket_price": 14.5, "seats_sold": 2, "transaction_id": "t1",
        }), {"ticket_price": 14.5, "seats_sold": 2, "transaction_id": "t1"})

    def test_unauthorized_users_cannot_read_or_submit(self):
        with patch.object(quarantine.sales, "get_connection") as connect:
            for operation in (
                lambda: quarantine.load_queue("viewer@example.com"),
                lambda: quarantine.load_record("viewer@example.com", "hash"),
                lambda: self.submit(email="viewer@example.com"),
            ):
                with self.assertRaises(PermissionError):
                    operation()
            connect.assert_not_called()

    def test_empty_allowlist_denies_access(self):
        with patch.object(quarantine, "OPERATORS", set()), self.assertRaises(PermissionError):
            quarantine.require_operator(self.email)

    def test_audit_and_original_display_encode_nonfinite_prices_as_valid_json(self):
        serialized = quarantine.serialize_record({
            "ticket_price": float("nan"), "other": [float("inf"), None],
        })
        self.assertEqual(json.loads(serialized), {
            "ticket_price": "nan", "other": ["inf", None],
        })

    def test_invalid_or_unconfirmed_forms_do_not_write(self):
        with patch.object(quarantine.sales, "get_connection") as connect:
            for overrides in (
                {"confirmed": False}, {"request_id": ""}, {"request_id": "not-uuid"},
                {"reason": ""}, {"reason": "x" * 2001}, {"patch_text": "not json"},
                {"patch_text": "{}"}, {"patch_text": "x" * 10001},
            ):
                with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                    self.submit(**overrides)
            connect.assert_not_called()

    def test_missing_job_configuration_does_not_save_a_request(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
            quarantine.sales, "get_connection"
        ) as connect, self.assertRaisesRegex(RuntimeError, "no request was submitted"):
            self.submit()
        connect.assert_not_called()

    def test_queue_is_read_only_scoped_and_hides_active_or_resolved_versions(self):
        with patch.object(quarantine.sales, "get_connection", return_value=self.connection), patch.object(
            quarantine.sales, "fetch_rows", side_effect=[[], []]
        ) as fetch:
            quarantine.load_queue(self.email, "pos_v2_batch", "invalid_ticket_price")
        query = fetch.call_args_list[0].args[1]
        self.assertIn("'SUBMITTED', 'APPLIED', 'COMPLETED'", query)
        self.assertIn("LIMIT 100", query)
        self.assertEqual(fetch.call_args_list[0].args[2], {
            "pos_source": "pos_v2_batch", "issue": "invalid_ticket_price",
        })
        self.cursor.execute.assert_not_called()

    def test_submission_is_verified_parameterized_and_starts_idempotent_job(self):
        class JobError(Exception):
            pass

        modules = {
            "databricks.sdk": SimpleNamespace(WorkspaceClient=lambda: self.client),
            "databricks.sdk.errors": SimpleNamespace(DatabricksError=JobError),
        }
        with patch.dict(sys.modules, modules), patch.dict(
            os.environ, {"QUARANTINE_RECOVERY_JOB_ID": "42"}
        ), patch.object(
            quarantine.sales, "get_connection", return_value=self.connection
        ), patch.object(quarantine.sales, "fetch_rows", side_effect=[
            [{"source_row_hash": "hash"}], [self.parameters],
        ]):
            self.assertEqual(self.submit(email=" OPERATOR@example.com "), 123)
        query = self.cursor.execute.call_args_list[0].args[0]
        self.assertNotIn("WHEN MATCHED", query)
        self.assertNotIn("Verified receipt", query)
        self.assertEqual(self.cursor.execute.call_args_list[0].kwargs["parameters"], self.parameters)
        self.client.jobs.run_now.assert_called_once_with(
            job_id=42, idempotency_token=f"quarantine-{self.request_id}",
        )
        self.assertIn("job_run_id", self.cursor.execute.call_args_list[1].args[0])

    def test_missing_original_or_changed_retry_never_starts_job(self):
        for rows in (
            [[], []],
            [[{"source_row_hash": "hash"}], [{**self.parameters, "reason": "Different"}]],
        ):
            with self.subTest(rows=rows), patch.dict(
                os.environ, {"QUARANTINE_RECOVERY_JOB_ID": "42"}
            ), patch.object(
                quarantine.sales, "get_connection", return_value=self.connection
            ), patch.object(quarantine.sales, "fetch_rows", side_effect=rows):
                with self.assertRaises((ValueError, RuntimeError)):
                    self.submit()
        self.client.jobs.run_now.assert_not_called()

    def test_trigger_failure_reports_saved_request_not_success(self):
        class JobError(Exception):
            pass

        self.client.jobs.run_now.side_effect = JobError("Permission denied")
        with patch.dict(sys.modules, {
            "databricks.sdk": SimpleNamespace(WorkspaceClient=lambda: self.client),
            "databricks.sdk.errors": SimpleNamespace(DatabricksError=JobError),
        }), patch.dict(os.environ, {"QUARANTINE_RECOVERY_JOB_ID": "42"}), patch.object(
            quarantine.sales, "get_connection", return_value=self.connection
        ), patch.object(quarantine.sales, "fetch_rows", side_effect=[
            [{"source_row_hash": "hash"}], [self.parameters],
        ]), self.assertRaisesRegex(RuntimeError, "was saved, but job submission failed"):
            self.submit()


def frame():
    result = MagicMock()
    for method in ("where", "orderBy", "limit", "select", "groupBy", "count", "withColumn"):
        getattr(result, method).return_value = result
    result.take.return_value = []
    return result


class RecoveryProcessorTests(unittest.TestCase):
    """Exercise the actual processor control flow with mocked Spark I/O."""

    def setUp(self):
        source = ROOT / "src" / "notebooks" / "process_quarantine_recovery.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        functions = ast.Module(
            body=[node for node in tree.body if isinstance(node, ast.FunctionDef)], type_ignores=[],
        )
        self.spark = MagicMock()
        self.request_id = str(uuid4())
        self.request = SimpleNamespace(
            request_id=self.request_id, quarantine_hash="bad-hash",
            corrections_json='{"ticket_price":14.5}',
        )
        self.row = SimpleNamespace(
            transaction_id="txn1", pos_source="pos1", movie_id="m1",
            theater_id="t1", screen_id="s1", source_row_hash="normalized-hash",
        )
        self.recovery_hash = hashlib.sha256(
            f"{self.request_id}:normalized-hash".encode("utf-8")
        ).hexdigest()
        self.requests = frame()
        self.requests.toLocalIterator.return_value = iter([self.request])
        self.requests.take.side_effect = [[], [], [self.request]]
        self.originals = frame()
        original = MagicMock()
        original.asDict.return_value = {"ticket_price": -1}
        self.originals.take.return_value = [original]
        self.silver = frame()
        self.silver.columns = ["transaction_id", "pos_source", "source_row_hash"]
        self.silver.take.side_effect = [[], [SimpleNamespace(source_row_hash=self.recovery_hash)]]
        self.bronze = frame()
        self.bronze.schema = [SimpleNamespace(name=name, dataType="string") for name in (
            "transaction_id", "pos_source", "purchase_timestamp", "showtime", "ticket_price",
        )]
        self.layout = frame()
        self.layout.take.return_value = [self.row]
        self.movies = frame()
        self.movies.take.return_value = [self.row]
        self.normalized = frame()
        self.normalized.first.return_value = self.row
        self.tables = {
            quarantine.REQUEST_TABLE: self.requests, quarantine.QUARANTINE_TABLE: self.originals,
            "silver": self.silver, "bronze": self.bronze,
            "theater_analytics.silver.theater_layout": self.layout,
            "theater_analytics.silver.imdb_movie_metadata": self.movies,
        }
        self.spark.table.side_effect = self.tables.__getitem__
        self.env = {
            "json": json, "hashlib": hashlib, "F": MagicMock(),
            "logger": logging.getLogger("test_recovery"),
            "REQUEST_TABLE": quarantine.REQUEST_TABLE,
            "QUARANTINE_TABLE": quarantine.QUARANTINE_TABLE,
            "SOURCE_TABLE": "bronze", "TARGET_TABLE": "silver",
            "validate_patch": quarantine.validate_patch,
            "serialize_record": quarantine.serialize_record,
            "normalize_sales": MagicMock(return_value=self.normalized),
            "should_quarantine": MagicMock(), "validate_silver": MagicMock(),
            "fill_unknown_strings": MagicMock(return_value=self.normalized),
            "dbutils": MagicMock(),
        }
        exec(compile(functions, str(source), "exec"), self.env)

    def process(self):
        self.env["process_requests"](self.spark)

    def statements(self):
        return [call.args[0] for call in self.spark.sql.call_args_list]

    def test_success_inserts_only_then_marks_applied_after_verification(self):
        self.process()
        sql = "\n".join(self.statements())
        self.assertIn("original_record_json", sql)
        self.assertIn("WHEN NOT MATCHED THEN INSERT *", sql)
        self.assertNotIn("WHEN MATCHED", sql)
        self.assertIn("status = 'APPLIED'", sql)
        self.assertNotIn("DELETE", sql)
        self.env["normalize_sales"].assert_called_once()
        self.env["validate_silver"].assert_called_once()
        self.normalized.withColumn.assert_called_once()
        self.spark.catalog.dropTempView.assert_called_once_with("quarantine_recovery_batch")
        self.env["dbutils"].jobs.taskValues.set.assert_any_call(key="recovery_applied", value=True)

    def test_partial_failure_retry_recognizes_exact_recovered_hash(self):
        self.silver.take.side_effect = [
            [SimpleNamespace(source_row_hash=self.recovery_hash)],
            [SimpleNamespace(source_row_hash=self.recovery_hash)],
        ]
        self.process()
        self.assertIn("status = 'APPLIED'", "\n".join(self.statements()))
        self.assertNotIn("REJECTED", "\n".join(self.statements()))

    def test_conflicting_silver_key_rejected_without_overwrite(self):
        self.silver.take.side_effect = [[SimpleNamespace(source_row_hash="existing")]]
        self.process()
        self.assertNotIn("MERGE INTO", "\n".join(self.statements()))
        self.assertIn("status = 'REJECTED'", "\n".join(self.statements()))

    def test_invalid_correction_never_writes_silver(self):
        for patch_text in ('{"ticket_price":0}', '{"ingestion_date":"change"}', "invalid"):
            with self.subTest(patch_text=patch_text):
                self.setUp()
                self.request.corrections_json = patch_text
                self.process()
                self.assertNotIn("MERGE INTO", "\n".join(self.statements()))
                self.assertIn("status = 'REJECTED'", "\n".join(self.statements()))

    def test_unparseable_required_values_and_invalid_measures_stay_quarantined(self):
        for outcomes in ([self.row], [[], [self.row]]):
            with self.subTest(outcomes=outcomes):
                self.setUp()
                if len(outcomes) == 1:
                    self.normalized.take.return_value = outcomes
                else:
                    self.normalized.take.side_effect = outcomes
                self.process()
                self.assertNotIn("MERGE INTO", "\n".join(self.statements()))

    def test_missing_or_ambiguous_references_are_rejected(self):
        for matches in ([], [self.row, self.row]):
            with self.subTest(matches=matches):
                self.setUp()
                self.movies.take.return_value = matches
                self.process()
                self.assertNotIn("MERGE INTO", "\n".join(self.statements()))

    def test_missing_original_or_already_restored_version_is_not_inserted(self):
        for already_resolved in (False, True):
            with self.subTest(already_resolved=already_resolved):
                self.setUp()
                if already_resolved:
                    self.requests.take.return_value = []
                    self.requests.take.side_effect = [[], [self.request], [self.request]]
                else:
                    self.originals.take.return_value = []
                self.process()
                self.assertNotIn("MERGE INTO", "\n".join(self.statements()))

    def test_unverified_merge_or_infrastructure_failure_does_not_claim_applied(self):
        for failure in ("verification", "merge"):
            with self.subTest(failure=failure):
                self.setUp()
                if failure == "verification":
                    self.silver.take.side_effect = [[], []]
                else:
                    def execute(sql, **kwargs):
                        if "MERGE INTO" in sql:
                            raise RuntimeError("Storage unavailable")
                    self.spark.sql.side_effect = execute
                with self.assertRaises(RuntimeError):
                    self.process()
                self.assertNotIn("status = 'APPLIED'", "\n".join(self.statements()))
                self.spark.catalog.dropTempView.assert_called_once()

    def test_duplicate_request_ids_fail_closed(self):
        self.requests.take.side_effect = [[self.request]]
        with self.assertRaisesRegex(RuntimeError, "Duplicate"):
            self.process()
        self.spark.sql.assert_not_called()

    def test_unprovisioned_recovery_preserves_normal_job_with_explicit_warning(self):
        self.spark.catalog.tableExists.return_value = False
        with self.assertLogs("test_recovery", level="WARNING"):
            self.process()
        self.env["normalize_sales"].assert_not_called()
        self.env["dbutils"].jobs.taskValues.set.assert_any_call(
            key="recovery_applied", value=False,
        )

    def test_failed_downstream_publication_is_retried_without_new_requests(self):
        self.requests.toLocalIterator.return_value = iter([])
        self.requests.take.side_effect = [[], [self.request]]
        self.process()
        self.env["normalize_sales"].assert_not_called()
        self.spark.sql.assert_not_called()
        self.env["dbutils"].jobs.taskValues.set.assert_any_call(
            key="recovery_applied", value=True,
        )


class RecoveryCompletionTests(unittest.TestCase):
    def run_completion(self, result_count=1):
        spark = MagicMock()
        request = SimpleNamespace(request_id=str(uuid4()))
        snapshot = MagicMock()
        snapshot.toLocalIterator.return_value = iter([request])
        verification = MagicMock()
        verification.take.return_value = [request] * result_count

        def execute(sql, **kwargs):
            return snapshot if "SELECT request_id FROM" in sql else verification

        spark.sql.side_effect = execute
        session = SimpleNamespace(builder=MagicMock())
        session.builder.getOrCreate.return_value = spark
        modules = {
            "pyspark": SimpleNamespace(),
            "pyspark.sql": SimpleNamespace(SparkSession=session),
        }
        source = ROOT / "src" / "notebooks" / "complete_quarantine_recovery.py"
        with patch.dict(sys.modules, modules):
            if result_count != 1:
                with self.assertRaises(RuntimeError):
                    exec(compile(source.read_text(), str(source), "exec"), {})
            else:
                exec(compile(source.read_text(), str(source), "exec"), {})
        return spark

    def test_completed_status_requires_exact_match_in_silver_and_combined(self):
        spark = self.run_completion()
        queries = [call.args[0] for call in spark.sql.call_args_list]
        self.assertIn("silver.pos_ticket_sales s", queries[1])
        self.assertIn("silver.denormalized_theater_sales s", queries[2])
        self.assertIn("status = 'COMPLETED'", queries[3])
        self.assertTrue(all("restored_source_row_hash" in query for query in queries[1:3]))

    def test_missing_or_ambiguous_restored_rows_never_mark_complete(self):
        for count in (0, 2):
            with self.subTest(count=count):
                spark = self.run_completion(count)
                self.assertFalse(any(
                    "status = 'COMPLETED'" in call.args[0]
                    for call in spark.sql.call_args_list
                ))


if __name__ == "__main__":
    unittest.main()
