import importlib.util
import json
import runpy
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

APP_PATH = Path(__file__).resolve().parents[1] / "src" / "app"
spec = importlib.util.spec_from_file_location("gold_sales", APP_PATH / "sales.py")
sales = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sales)


def movie(index, tickets=10, days=2):
    return {
        "movie_id": f"m{index:02}",
        "movie_title": f"Movie {index}",
        "tickets_sold": tickets,
        "active_screen_days": days,
        "score": Decimal(tickets) / days,
        "revenue": Decimal("12.50"),
    }


class GoldSalesTests(unittest.TestCase):
    def test_shared_decisions_use_app_identity_without_scope_filters_or_writes(self):
        connection = MagicMock()
        cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        with patch.object(sales, "get_connection", return_value=connection), patch.object(
            sales, "fetch_rows", return_value=[{"decision_id": "d1"}]
        ) as fetch:
            self.assertEqual(sales.load_recent_decisions(), [{"decision_id": "d1"}])
        query = fetch.call_args.args[1]
        self.assertIn(sales.FEEDBACK_TABLE, query)
        self.assertIn("ORDER BY approved_at DESC, decision_id DESC LIMIT 20", query)
        self.assertNotIn("WHERE", query)
        self.user_connect.assert_not_called()
        cursor.execute.assert_not_called()

    def setUp(self):
        user_connection = patch.object(sales, "get_user_connection")
        self.user_connect = user_connection.start()
        self.addCleanup(user_connection.stop)

    def test_user_connection_uses_forwarded_token_not_app_credentials(self):
        connect = MagicMock()
        config = MagicMock(return_value=SimpleNamespace(host="https://example.databricks.com"))
        with patch.dict("sys.modules", {
            "databricks": SimpleNamespace(sql=SimpleNamespace(connect=connect)),
            "databricks.sdk": SimpleNamespace(),
            "databricks.sdk.core": SimpleNamespace(Config=config),
        }), patch.dict("os.environ", {"DATABRICKS_WAREHOUSE_ID": "test-warehouse"}):
            original = runpy.run_path(str(APP_PATH / "sales.py"))["get_user_connection"]
            original("test-user-token")
            connect.assert_called_once_with(
                server_hostname="example.databricks.com",
                http_path="/sql/1.0/warehouses/test-warehouse",
                access_token="test-user-token",
            )
            connect.reset_mock()
            with self.assertRaisesRegex(RuntimeError, "User SQL authorization"):
                original("")
            connect.assert_not_called()

    def test_default_queries_read_gold_not_legacy_denormalized_sales(self):
        with patch.dict("os.environ", {}, clear=True):
            defaults = runpy.run_path(str(APP_PATH / "sales.py"))
        self.assertEqual(
            defaults["TABLE_NAME"], "`theater_analytics`.`gold`.`theater_daily_sales`"
        )
        self.assertEqual(
            defaults["FEEDBACK_TABLE"], "`theater_analytics`.`gold`.`decision_feedback`"
        )
        for key in ("METRICS_SQL", "MOVIES_SQL"):
            self.assertIn(defaults["TABLE_NAME"], defaults[key])
            self.assertNotIn("denormalized_theater_sales", defaults[key])

    def run_grant_notebook(self):
        spark = MagicMock()
        spark_session = SimpleNamespace(builder=MagicMock())
        spark_session.builder.getOrCreate.return_value = spark
        modules = {
            "pyspark": SimpleNamespace(),
            "pyspark.sql": SimpleNamespace(SparkSession=spark_session),
        }
        with patch.dict("sys.modules", modules):
            runpy.run_path(
                str(APP_PATH.parent / "notebooks" / "grant_app_catalog_access.py"),
            )
        return spark

    def test_grant_notebook_limits_access_to_gold_tables(self):
        spark = self.run_grant_notebook()
        statements = [call.args[0] for call in spark.sql.call_args_list]
        principal = "`e44e0047-b327-4a29-bcf2-809a85ff2811`"
        self.assertEqual(statements[:4], [
            f"GRANT USE CATALOG ON CATALOG theater_analytics TO {principal}",
            f"GRANT USE SCHEMA ON SCHEMA theater_analytics.gold TO {principal}",
            f"GRANT SELECT ON TABLE theater_analytics.gold.theater_daily_sales TO {principal}",
            f"GRANT SELECT, MODIFY ON TABLE theater_analytics.gold.decision_feedback TO {principal}",
        ])
        self.assertEqual(len(statements), 6)
        self.assertFalse(any("workspace.fde_theater" in sql for sql in statements))

    def test_ranking_normalizes_by_screen_days_and_never_overlaps(self):
        rows = [movie(index, tickets=index + 10) for index in range(12)]
        rows[0] = movie(0, tickets=100, days=100)
        ranked = sales.rank_movies(rows)
        top = [row for row in ranked if row["action"] == "Increase screens"]
        bottom = [row for row in ranked if row["action"] == "Discontinue"]
        self.assertEqual(len(top), 5)
        self.assertEqual(len(bottom), 5)
        self.assertEqual(bottom[0]["movie_id"], "m00")
        self.assertFalse({r["movie_id"] for r in top} & {r["movie_id"] for r in bottom})
        self.assertEqual(rows[0].get("action"), None)

    def test_small_catalogs_and_deterministic_ties(self):
        self.assertEqual(sales.rank_movies([]), [])
        for size in range(1, 11):
            ranked = sales.rank_movies([movie(i) for i in reversed(range(size))])
            self.assertEqual(len(ranked), size)
            self.assertEqual(len({r["movie_id"] for r in ranked}), size)
            self.assertEqual(ranked[0]["movie_id"], "m00")

    def test_load_dashboard_creates_server_evidence_without_writes(self):
        connection = MagicMock()
        cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        metrics = {
            "period_start": date(2026, 3, 24), "period_end": date(2026, 3, 30)
        }
        with patch.object(sales, "get_connection", return_value=connection), patch.object(
            sales, "fetch_rows", side_effect=[[], [metrics], [movie(1)], [], []]
        ) as fetch:
            loaded = sales.load_dashboard("", "test-user-token")
        self.assertEqual(len(loaded["recommendations"]), 1)
        evidence = next(iter(loaded["recommendations"].values()))
        self.assertEqual(evidence["theater_id"], "")
        self.assertEqual(json.loads(evidence["evidence"])["tickets_per_active_screen_day"], "5")
        cursor.execute.assert_not_called()
        self.user_connect.assert_called_once_with("test-user-token")
        user_cursor = self.user_connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        self.assertTrue(all(call.args[0] is user_cursor for call in fetch.call_args_list[:4]))
        self.assertIs(fetch.call_args_list[4].args[0], cursor)

    def test_state_scope_filters_theaters_and_is_saved_in_evidence(self):
        metrics = {"period_start": date(2026, 3, 24), "period_end": date(2026, 3, 30)}
        theaters = [
            {"theater_id": "t1", "theater_name": "One", "state": "NY"},
            {"theater_id": "t2", "theater_name": "Two", "state": "CA"},
        ]
        with patch.object(sales, "get_connection"), patch.object(
            sales, "fetch_rows", side_effect=[theaters, [metrics], [movie(1)], [], []]
        ) as fetch:
            loaded = sales.load_dashboard("t2", "test-user-token", "NY")
        self.assertEqual(loaded["states"], ["CA", "NY"])
        self.assertEqual(loaded["theaters"], [theaters[0]])
        self.assertEqual(loaded["theater_id"], "")
        self.assertEqual(fetch.call_args_list[1].args[2], {"theater_id": "", "state": "NY"})
        self.assertEqual(fetch.call_args_list[4].args[2], {"theater_id": "", "state": "NY"})
        evidence = next(iter(loaded["recommendations"].values()))
        self.assertEqual(evidence["state"], "NY")
        self.assertEqual(json.loads(evidence["evidence"])["state"], "NY")

    def approval_fixture(self):
        candidate = sales.rank_movies([movie(1)])[0]
        start, end = date(2026, 3, 24), date(2026, 3, 30)
        return {
            "id": {
                **candidate,
                "decision_id": "id",
                "theater_id": "t1",
                "period_start": start,
                "period_end": end,
                "evidence": sales.evidence_fingerprint(candidate, start, end),
            }
        }

    def approve(self, **overrides):
        args = {
            "decision_id": "id",
            "recommendations": self.approval_fixture(),
            "theater_id": "t1",
            "confirmed": True,
            "note": "Leadership reviewed evidence",
            "email": "leader@example.com",
            "user_token": "test-user-token",
        }
        args.update(overrides)
        return sales.approve_decision(**args)

    def test_rejects_missing_identity_unapproved_user_or_incomplete_review(self):
        with patch.object(sales, "APPROVERS", {"leader@example.com"}), patch.object(
            sales, "get_connection"
        ) as connect:
            for overrides, error in [
                ({"email": ""}, PermissionError),
                ({"email": "viewer@example.com"}, PermissionError),
                ({"confirmed": False}, ValueError),
                ({"note": ""}, ValueError),
                ({"note": "x" * 2001}, ValueError),
                ({"decision_id": "client-forged-id"}, ValueError),
                ({"theater_id": "t2"}, ValueError),
                ({"state": "NY"}, ValueError),
            ]:
                with self.subTest(overrides=overrides), self.assertRaises(error):
                    self.approve(**overrides)
            connect.assert_not_called()

    def test_approval_is_parameterized_verified_and_idempotent(self):
        connection = MagicMock()
        cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        metrics = {"period_start": date(2026, 3, 24), "period_end": date(2026, 3, 30)}
        with patch.object(sales, "APPROVERS", {"leader@example.com"}), patch.object(
            sales, "get_connection", return_value=connection
        ), patch.object(
            sales, "fetch_rows",
            side_effect=[[], [metrics], [{"decision_id": "id"}]],
        ) as fetch, patch.object(sales, "read_movies", return_value=[movie(1)]) as read:
            self.assertIn("Approved and recorded", self.approve(note="It's approved; --"))
        statement = cursor.execute.call_args.args[0]
        parameters = cursor.execute.call_args.kwargs["parameters"]
        self.assertIn("WHEN NOT MATCHED THEN INSERT", statement)
        self.assertNotIn("It's approved", statement)
        self.assertEqual(parameters["approval_note"], "It's approved; --")
        self.assertEqual(parameters["approver_email"], "leader@example.com")
        self.user_connect.assert_called_once_with("test-user-token")
        user_cursor = self.user_connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        self.assertIs(fetch.call_args_list[1].args[0], user_cursor)
        read.assert_called_once_with(user_cursor, "t1", "")
        user_cursor.execute.assert_not_called()
        cursor.execute.reset_mock()
        with patch.object(sales, "APPROVERS", {"leader@example.com"}), patch.object(
            sales, "get_connection", return_value=connection
        ), patch.object(sales, "fetch_rows", return_value=[{"decision_id": "id"}]):
            self.assertIn("already recorded", self.approve())
        cursor.execute.assert_not_called()

    def test_changed_data_or_period_rejects_approval(self):
        for rows, end in [
            ([movie(1, tickets=12)], date(2026, 3, 30)),
            ([], date(2026, 3, 30)),
            ([movie(1)], date(2026, 3, 31)),
        ]:
            connection = MagicMock()
            cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
            with patch.object(sales, "APPROVERS", {"leader@example.com"}), patch.object(
                sales, "get_connection", return_value=connection
            ), patch.object(
                sales, "fetch_rows",
                side_effect=[[], [{"period_start": date(2026, 3, 24), "period_end": end}]],
            ), patch.object(sales, "read_movies", return_value=rows):
                with self.assertRaisesRegex(ValueError, "changed"):
                    self.approve()
            cursor.execute.assert_not_called()

    def test_state_scoped_approval_rechecks_scope_and_persists_it_in_evidence(self):
        connection = MagicMock()
        cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        recommendations = self.approval_fixture()
        candidate = recommendations["id"]
        candidate["state"] = "NY"
        candidate["evidence"] = sales.evidence_fingerprint(
            candidate, candidate["period_start"], candidate["period_end"], "NY"
        )
        metrics = {
            "period_start": candidate["period_start"], "period_end": candidate["period_end"]
        }
        with patch.object(sales, "APPROVERS", {"leader@example.com"}), patch.object(
            sales, "get_connection", return_value=connection
        ), patch.object(
            sales, "fetch_rows", side_effect=[[], [metrics], [{"decision_id": "id"}]]
        ) as fetch, patch.object(sales, "read_movies", return_value=[movie(1)]) as read:
            self.approve(recommendations=recommendations, state="NY")
        self.assertEqual(fetch.call_args_list[1].args[2], {"theater_id": "t1", "state": "NY"})
        self.assertEqual(read.call_args.args[1:], ("t1", "NY"))
        parameters = cursor.execute.call_args.kwargs["parameters"]
        self.assertEqual(json.loads(parameters["evidence_json"])["state"], "NY")

    def test_unverified_write_fails_instead_of_reporting_success(self):
        connection = MagicMock()
        with patch.object(sales, "APPROVERS", {"leader@example.com"}), patch.object(
            sales, "get_connection", return_value=connection
        ), patch.object(
            sales, "fetch_rows",
            side_effect=[
                [],
                [{"period_start": date(2026, 3, 24), "period_end": date(2026, 3, 30)}],
                [],
            ],
        ), patch.object(sales, "read_movies", return_value=[movie(1)]):
            with self.assertRaisesRegex(RuntimeError, "verified"):
                self.approve()


if __name__ == "__main__":
    unittest.main()
