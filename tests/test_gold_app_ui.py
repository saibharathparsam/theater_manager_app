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
    def test_separate_pages_preserve_notification_and_approval_wiring(self):
        config = app.gradio_app.config
        tabs = {
            component["props"].get("id"): component
            for component in config["components"] if component["type"] == "tabitem"
        }
        self.assertEqual(set(tabs), {"sales", "feedback", "quarantine"})
        self.assertFalse(tabs["quarantine"]["props"]["visible"])
        self.assertEqual(app.operations_panel._id, tabs["quarantine"]["id"])
        self.assertIn(app.feedback_scope._id, [component._id for component in app.outputs])
        dependencies = config["dependencies"]
        approval = next(event for event in dependencies if event["outputs"] == [app.status._id])
        self.assertEqual(approval["inputs"], [
            app.decision._id, app.recommendations._id, app.theater_filter._id,
            app.confirm._id, app.note._id, app.state_filter._id,
        ])
        self.assertTrue(any(
            event["outputs"] == [component._id for component in app.operations_outputs]
            and (app.gradio_app._id, "load") in event["targets"]
            for event in dependencies
        ))

    def test_notification_timer_and_load_only_update_notification_components(self):
        self.assertEqual(app.decision_timer.value, 60)
        self.assertTrue(app.decision_timer.active)
        notification_ids = [component._id for component in app.notification_outputs]
        events = [
            event for event in app.gradio_app.config["dependencies"]
            if event["outputs"] == notification_ids
        ]
        self.assertEqual(len(events), 2)
        self.assertEqual(
            {target for event in events for target in event["targets"]},
            {(app.gradio_app._id, "load"), (app.decision_timer._id, "tick")},
        )
        self.assertTrue(all(event["inputs"] == [app.seen_decisions._id] for event in events))
        self.assertTrue(all(event["queue"] for event in events))
        self.assertTrue(set(notification_ids).isdisjoint(component._id for component in app.outputs))

    def decision_row(self, decision_id="d1"):
        return {
            "decision_id": decision_id, "approved_at": "2026-03-30 12:00:00",
            "approver_email": "leader@example.com", "action": "Discontinue",
            "movie_title": "Movie One", "theater_id": "t1",
            "period_start": date(2026, 3, 24), "period_end": date(2026, 3, 30),
            "approval_note": "Reviewed", "state": "NY",
        }

    def test_notification_feed_loads_recent_decisions_without_historical_toasts(self):
        with patch.object(app.sales, "load_recent_decisions", return_value=[self.decision_row()]), patch.object(
            app.gr, "Info"
        ) as notify:
            table, seen, status = app.refresh_decision_notifications(None)
        self.assertEqual(len(table), 1)
        self.assertEqual(seen, {"d1"})
        self.assertIn("all states/theaters", status)
        notify.assert_not_called()

    def test_new_decision_notifies_each_session_once_independent_of_sales_filters(self):
        with patch.object(
            app.sales, "load_recent_decisions",
            return_value=[self.decision_row("d2"), self.decision_row()],
        ) as load, patch.object(app.gr, "Info") as notify:
            for existing in ({"d1"}, {"d1"}):
                _, seen, _ = app.refresh_decision_notifications(existing)
                self.assertEqual(seen, {"d1", "d2"})
            self.assertEqual(notify.call_count, 2)
            self.assertIn("Discontinue - Movie One", notify.call_args.args[0])
            notify.reset_mock()
            app.refresh_decision_notifications(seen)
            notify.assert_not_called()
        self.assertTrue(all(call.args == () for call in load.call_args_list))

    def test_empty_feed_then_first_approval_notifies(self):
        with patch.object(app.sales, "load_recent_decisions", return_value=[]):
            table, seen, status = app.refresh_decision_notifications(None)
        self.assertTrue(table.empty)
        self.assertEqual(seen, set())
        self.assertIn("No leadership decisions", status)
        with patch.object(app.sales, "load_recent_decisions", return_value=[self.decision_row()]), patch.object(
            app.gr, "Info"
        ) as notify:
            app.refresh_decision_notifications(seen)
        notify.assert_called_once()

    def test_notification_failure_preserves_seen_state_and_shows_staleness(self):
        seen = {"d1"}
        with patch.object(app.sales, "load_recent_decisions", side_effect=RuntimeError("test")), patch.object(
            app.gr, "Info"
        ) as notify, self.assertLogs(app.logger, level="ERROR"):
            table, result_seen, status = app.refresh_decision_notifications(seen)
        self.assertEqual(table, app.gr.skip())
        self.assertIs(result_seen, seen)
        self.assertIn("stale", status)
        notify.assert_not_called()

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
        with patch.object(app.sales, "approve_decision", return_value="Recorded") as approve, patch.object(
            app.gr, "Info"
        ) as notify:
            result = app.approve("server-id", evidence, "t1", True, "Reviewed", request)
        self.assertEqual(result, "Recorded")
        notify.assert_called_once_with("Recorded")
        approve.assert_called_once_with(
            "server-id", evidence, "t1", True, "Reviewed", "leader@example.com", "test-token", ""
        )

    def test_failed_approval_does_not_show_success_notification(self):
        request = SimpleNamespace(headers={
            "x-forwarded-email": "leader@example.com",
            "x-forwarded-access-token": "test-token",
        })
        for error in (ValueError("Review required"), RuntimeError("Write not verified")):
            with self.subTest(error=type(error).__name__), patch.object(
                app.sales, "approve_decision", side_effect=error
            ), patch.object(app.gr, "Info") as notify:
                with self.assertLogs(app.logger), self.assertRaises(app.gr.Error):
                    app.approve("id", {}, "", True, "Reviewed", request)
                notify.assert_not_called()

    def test_already_recorded_approval_shows_retry_notification(self):
        request = SimpleNamespace(headers={
            "x-forwarded-email": "leader@example.com",
            "x-forwarded-access-token": "test-token",
        })
        message = "Decision id was already recorded. No duplicate was added."
        with patch.object(app.sales, "approve_decision", return_value=message), patch.object(
            app.gr, "Info"
        ) as notify:
            result = app.approve("id", {}, "", True, "Reviewed", request)
        self.assertEqual(result, message)
        notify.assert_called_once_with(message)

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
        self.assertIn("State: NY", result[17])
        self.assertIn("All theaters in selected state", result[17])
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
