import contextlib
import importlib.util
import io
import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

NOTEBOOKS = Path(__file__).resolve().parents[1] / "src" / "notebooks"
sys.path.insert(0, str(NOTEBOOKS))
import theater_security as security

spec = importlib.util.spec_from_file_location("verify_theater_access", NOTEBOOKS / "verify_theater_access.py")
verification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verification)

CLIENT_ID = "00000000-0000-0000-0000-000000000001"


def policy_row(**overrides):
    values = {
        "filter_catalog": "theater_analytics", "filter_schema": "gold",
        "filter_name": "sales_row_filter",
        "filter_col_usage": "`manager_email`, `zone_manager_email`",
    }
    return SimpleNamespace(**(values | overrides))


class SecurityProvisioningTests(unittest.TestCase):
    def configure(self, spark, **options):
        with contextlib.redirect_stdout(io.StringIO()):
            security.configure_security(spark, **options)

    def test_default_inspection_reports_missing_tables_without_executing_provisioning(self):
        spark = MagicMock()
        spark.catalog.tableExists.return_value = False
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            security.configure_security(spark)
        self.assertIn(f"MISSING: {security.SALES_TABLE}", output.getvalue())
        self.assertIn("CREATE OR REPLACE FUNCTION", output.getvalue())
        spark.sql.assert_not_called()
        spark.catalog.tableExists.return_value = True
        self.configure(spark)
        for call in spark.sql.call_args_list:
            self.assertTrue(call.args[0].lstrip().startswith(("SELECT", "SHOW GRANTS")))

    def test_invalid_mode_identity_or_missing_sources_fail_before_any_writes(self):
        spark = MagicMock()
        spark.catalog.tableExists.return_value = False
        for options, error in (
            ({"mode": "unknown"}, ValueError),
            ({"mode": "apply"}, ValueError),
            ({"mode": "apply", "client_id": "12345"}, ValueError),
            ({"mode": "apply", "client_id": CLIENT_ID}, RuntimeError),
            ({"mode": "apply", "client_id": CLIENT_ID, "viewer_principal": "bad\nprincipal"}, ValueError),
        ):
            with self.subTest(options=options), self.assertRaises(error):
                self.configure(spark, **options)
        spark.sql.assert_not_called()

    def test_apply_preserves_ledgers_installs_policy_before_grants_and_limits_viewer_access(self):
        spark = MagicMock()
        spark.catalog.tableExists.return_value = True
        spark.sql.return_value.collect.return_value = [policy_row()]
        self.configure(spark, mode="apply", client_id=CLIENT_ID, viewer_principal="viewers")
        statements = [call.args[0].strip() for call in spark.sql.call_args_list]
        creates = [sql for sql in statements if sql.startswith("CREATE TABLE")]
        self.assertEqual(len(creates), 3)
        self.assertTrue(all("IF NOT EXISTS" in sql for sql in creates))
        self.assertFalse(any(sql.startswith(("INSERT", "UPDATE", "DELETE", "DROP", "REVOKE")) for sql in statements))
        attachment = next(i for i, sql in enumerate(statements) if sql.startswith("ALTER TABLE"))
        first_grant = next(i for i, sql in enumerate(statements) if sql.startswith("GRANT"))
        self.assertLess(attachment, first_grant)
        viewer_sql = [sql for sql in statements if sql.endswith("TO `viewers`")]
        self.assertEqual(viewer_sql, security.viewer_grants("viewers"))
        self.assertFalse(any("MODIFY" in sql or "leadership_users" in sql for sql in viewer_sql))
        modified_tables = {
            sql.split(" ON TABLE ")[1].split(" TO ")[0]
            for sql in statements if sql.startswith("GRANT") and "MODIFY" in sql
        }
        self.assertEqual(modified_tables, {security.FEEDBACK_TABLE, security.REQUEST_TABLE})

    def test_misbound_or_absent_policy_never_reports_success(self):
        spark = MagicMock()
        for rows in (
            [], [policy_row(filter_name="another_filter")],
            [policy_row(filter_col_usage="zone_manager_email,manager_email")],
            [policy_row(), policy_row()],
        ):
            spark.sql.return_value.collect.return_value = rows
            with self.subTest(rows=rows), self.assertRaises(RuntimeError):
                security.verify_row_filter(spark)

    def test_principal_identifier_is_quoted_not_interpolated_as_sql(self):
        statements = security.viewer_grants("viewers`group")
        self.assertTrue(all(sql.endswith("TO `viewers``group`") for sql in statements))

    def test_policy_sql_handles_leadership_manager_zone_and_unrelated_identities(self):
        # Execute the production predicate with local stand-ins for UC identity functions.
        predicate = security.ROW_FILTER_BODY.replace(security.LEADERSHIP_TABLE, "leadership_users")
        with sqlite3.connect(":memory:") as connection:
            connection.execute("CREATE TABLE leadership_users (user_email TEXT NOT NULL)")
            connection.execute("INSERT INTO leadership_users VALUES (' Leader@Example.com ')")
            for user, group_member, manager, zone, expected in (
                ("leader@example.com", False, None, None, True),
                ("executive@example.com", True, None, None, True),
                ("manager@example.com", False, " MANAGER@example.com ", "zone@example.com", True),
                ("zone@example.com", False, "other@example.com", "zone@example.com", True),
                ("unrelated@example.com", False, "manager@example.com", "zone@example.com", False),
                ("unrelated@example.com", False, None, None, False),
                ("unrelated@example.com", False, "", "Unknown", False),
            ):
                with self.subTest(user=user, manager=manager, zone=zone):
                    connection.create_function("current_user", 0, lambda: user)
                    connection.create_function("is_account_group_member", 1, lambda group: group_member)
                    allowed = connection.execute(
                        f"SELECT ({predicate}) FROM (SELECT ? AS manager_email, ? AS zone_manager_email)",
                        (manager, zone),
                    ).fetchone()[0]
                    self.assertEqual(bool(allowed), expected)


class ActualViewerVerificationTests(unittest.TestCase):
    def spark(self, user="manager@example.com", theaters=None, count=2):
        spark = MagicMock()
        spark.sql.return_value.collect.return_value = [policy_row()]
        spark.sql.return_value.first.side_effect = [
            SimpleNamespace(viewer=user),
            SimpleNamespace(visible_rows=count, theater_ids=["T001"] if theaters is None else theaters),
        ]
        return spark

    def test_exact_authorized_scope_and_zero_row_unrelated_viewer_pass(self):
        for user, theaters, count in (
            ("manager@example.com", ["T001"], 2),
            ("zone@example.com", ["T001", "T002"], 4),
            ("leader@example.com", ["T001", "T002", "<NULL>"], 5),
            ("unrelated@example.com", [], 0),
        ):
            with self.subTest(user=user), contextlib.redirect_stdout(io.StringIO()):
                verification.verify_access(self.spark(user, theaters, count), user, theaters, count)

    def test_wrong_execution_identity_fails_before_sales_query(self):
        spark = self.spark(user="owner@example.com")
        with self.assertRaisesRegex(RuntimeError, "Wrong execution identity"):
            verification.verify_access(spark, "manager@example.com", ["T001"], 2)
        self.assertEqual(spark.sql.call_count, 1)

    def test_extra_theater_missing_rows_and_missing_filter_fail(self):
        for spark in (
            self.spark(theaters=["T001", "T002"]),
            self.spark(count=1),
            self.spark(count=0, theaters=[]),
        ):
            with self.assertRaisesRegex(RuntimeError, "Access mismatch"):
                verification.verify_access(spark, "manager@example.com", ["T001"], 2)
        spark = self.spark()
        spark.sql.return_value.collect.return_value = []
        with self.assertRaisesRegex(RuntimeError, "row filter"):
            verification.verify_access(spark, "manager@example.com", ["T001"], 2)

    def test_missing_baseline_or_query_permission_errors_do_not_pass(self):
        for user, theaters, count in (
            ("", ["T001"], 2), ("viewer", "T001", 2),
            ("viewer", [], 2), ("viewer", ["T001"], 0),
            ("viewer", [], -1), ("viewer", [], True),
        ):
            spark = MagicMock()
            with self.subTest(user=user, theaters=theaters, count=count), self.assertRaises(ValueError):
                verification.verify_access(spark, user, theaters, count)
            spark.sql.assert_not_called()
        spark = MagicMock()
        spark.sql.side_effect = PermissionError("SELECT denied")
        with self.assertRaises(PermissionError):
            verification.verify_access(spark, "manager@example.com", ["T001"], 2)


if __name__ == "__main__":
    unittest.main()
