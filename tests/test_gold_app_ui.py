import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "app"))

import app


def dashboard_data(empty=False):
    start, end = date(2026, 3, 24), date(2026, 3, 30)
    candidates = {}
    if not empty:
        for index in range(10):
            candidates[str(index)] = {
                "movie_id": f"m{index}",
                "movie_title": f"Movie {index}",
                "tickets_sold": 100 + index,
                "active_screen_days": 10,
                "score": Decimal(100 + index) / 10,
                "revenue": Decimal("125.50"),
                "action": "Discontinue" if index < 5 else "Increase screens",
            }
    return {
        "theaters": [{"theater_id": "t1", "theater_name": "Theater One", "state": "NY"}],
        "states": ["NY"],
        "metrics": {
            "period_start": start,
            "period_end": end,
            "revenue": Decimal("0") if empty else Decimal("1255.00"),
            "tickets_sold": 0 if empty else 1045,
            "theaters": 0 if empty else 1,
            "movies": 0 if empty else 10,
            "daily_groups": 0 if empty else 10,
            "unmatched_groups": 0,
            "suspect_groups": 0,
        },
        "daily": [] if empty else [{"show_date": start, "revenue": Decimal("1255.00")}],
        "recommendations": candidates,
        "history": [],
    }


class GoldAppUiTests(unittest.TestCase):
    def test_read_only_viewer_sees_metrics_but_cannot_approve(self):
        request = SimpleNamespace(headers={"x-forwarded-access-token": "test-token"})
        with patch.object(app.sales, "load_dashboard", return_value=dashboard_data()):
            result = app.update_dashboard("", request)
        self.assertEqual(len(result), len(app.outputs))
        self.assertEqual(result[1:5], (1255.0, 1045, 1, 10))
        self.assertEqual(len(result[7]), 5)
        self.assertEqual(len(result[8]), 5)
        self.assertEqual(result[6]["show_date"].tolist(), ["2026-03-24"])
        self.assertEqual(result[6]["revenue"].tolist(), [1255.0])
        self.assertIn("Read-only", result[5])
        for index in (10, 11, 12, 13):
            self.assertFalse(result[index]["interactive"])

    def test_authenticated_leader_has_approval_controls(self):
        request = SimpleNamespace(headers={
            "x-forwarded-email": " LEADER@example.com ",
            "x-forwarded-access-token": "test-token",
        })
        with patch.object(app.sales, "APPROVERS", {"leader@example.com"}), patch.object(
            app.sales, "load_dashboard", return_value=dashboard_data()
        ) as load:
            result = app.update_dashboard("t1", request)
        load.assert_called_once_with("t1", "test-token", "")
        self.assertIn("Leadership", result[5])
        for index in (10, 11, 12, 13):
            self.assertTrue(result[index]["interactive"])
        self.assertFalse(result[11]["value"])
        self.assertIsNone(result[10]["value"])

    def test_empty_scope_renders_without_candidates_or_approval(self):
        request = SimpleNamespace(headers={
            "x-forwarded-email": "leader@example.com",
            "x-forwarded-access-token": "test-token",
        })
        with patch.object(app.sales, "APPROVERS", {"leader@example.com"}), patch.object(
            app.sales, "load_dashboard", return_value=dashboard_data(empty=True)
        ):
            result = app.update_dashboard("t1", request)
        self.assertEqual(result[1:5], (0.0, 0, 0, 0))
        self.assertIn("No sales in this scope/period", result[5])
        for index in (6, 7, 8, 15):
            self.assertTrue(result[index].empty)
        self.assertFalse(result[13]["interactive"])

    def test_refresh_failure_is_an_explicit_ui_error(self):
        with patch.object(app.sales, "load_dashboard", side_effect=RuntimeError("test")):
            with self.assertLogs(app.logger, level="ERROR"), self.assertRaises(app.gr.Error):
                app.update_dashboard("", SimpleNamespace(
                    headers={"x-forwarded-access-token": "test-token"}
                ))

    def test_missing_user_token_fails_without_service_principal_fallback(self):
        with patch.object(app.sales, "load_dashboard") as load:
            with self.assertLogs(app.logger, level="ERROR"), self.assertRaises(app.gr.Error):
                app.update_dashboard("", SimpleNamespace(headers={}))
        load.assert_not_called()

    def test_approval_uses_proxy_identity_and_server_evidence(self):
        request = SimpleNamespace(headers={
            "x-forwarded-email": " LEADER@example.com ",
            "x-forwarded-access-token": "test-token",
        })
        evidence = {"server-id": {"movie_id": "m1"}}
        with patch.object(app.sales, "approve_decision", return_value="Recorded") as approve:
            result = app.approve("server-id", evidence, "t1", True, "Reviewed", request)
        self.assertEqual(result, "Recorded")
        approve.assert_called_once_with(
            "server-id", evidence, "t1", True, "Reviewed", "leader@example.com", "test-token", ""
        )

    def test_state_filter_is_forwarded_to_reads_and_approval(self):
        request = SimpleNamespace(headers={
            "x-forwarded-email": "leader@example.com",
            "x-forwarded-access-token": "test-token",
        })
        data = dashboard_data()
        data["theater_id"] = ""
        with patch.object(app.sales, "load_dashboard", return_value=data) as load:
            result = app.update_dashboard("t2", request, "NY")
        load.assert_called_once_with("t2", "test-token", "NY")
        self.assertEqual(result[0]["value"], "")
        self.assertEqual(result[16]["value"], "NY")
        self.assertIn("State: NY", result[5])
        with patch.object(app.sales, "approve_decision", return_value="Recorded") as approve:
            app.approve("id", {}, "", True, "Reviewed", request, "NY")
        self.assertEqual(approve.call_args.args[-1], "NY")

    def test_chart_preserves_gold_calendar_dates_as_iso_labels(self):
        data = dashboard_data()
        data["daily"] = [
            {"show_date": date(2026, 3, 24), "revenue": Decimal("12.50")},
            {"show_date": date(2026, 3, 25), "revenue": Decimal("20.00")},
        ]
        request = SimpleNamespace(headers={"x-forwarded-access-token": "test-token"})
        with patch.object(app.sales, "load_dashboard", return_value=data):
            result = app.update_dashboard("", request)
        chart_data = result[6]
        self.assertEqual(chart_data["show_date"].tolist(), ["2026-03-24", "2026-03-25"])
        serialized = app.daily.postprocess(chart_data)
        self.assertIsNotNone(serialized)
        plot = serialized.model_dump()
        self.assertEqual(plot["datatypes"]["show_date"], "nominal")
        self.assertEqual(
            plot["data"], [["2026-03-24", 12.5], ["2026-03-25", 20.0]]
        )


if __name__ == "__main__":
    unittest.main()
