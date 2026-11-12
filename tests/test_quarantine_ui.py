import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "app"))
import app


class QuarantineUiTests(unittest.TestCase):
    def setUp(self):
        self.email = "operator@example.com"
        self.request = SimpleNamespace(headers={"x-forwarded-email": self.email})
        auth = patch.object(app.quarantine, "OPERATORS", {self.email})
        auth.start()
        self.addCleanup(auth.stop)

    def test_unauthorized_panel_never_loads_quarantine(self):
        with patch.object(app.quarantine, "load_queue") as load:
            result = app.load_operations(SimpleNamespace(headers={}))
        self.assertFalse(result[0]["visible"])
        self.assertTrue(result[1].empty)
        load.assert_not_called()

    def test_operator_sees_queue_reasons_and_history_without_gold_user_token(self):
        row = {
            "source_row_hash": "h" * 64, "transaction_id": "txn1", "pos_source": "pos1",
            "theater_id": "t1", "movie_id": "m1", "ticket_price": -1,
            "data_quality_issues": ["invalid_ticket_price"], "ingestion_timestamp": "2026-01-01",
        }
        with patch.object(app.quarantine, "load_queue", return_value=(
            [row], [{"request_id": "r1", "status": "REJECTED", "last_error": "invalid"}],
        )) as load:
            result = app.load_operations(self.request, "pos1", "invalid_ticket_price")
        load.assert_called_once_with(self.email, "pos1", "invalid_ticket_price")
        self.assertTrue(result[0]["visible"])
        self.assertEqual(result[1].iloc[0]["DQ reasons"], "invalid_ticket_price")
        self.assertEqual(result[3].iloc[0]["status"], "REJECTED")

    def test_empty_queue_has_useful_columns_and_empty_state(self):
        with patch.object(app.quarantine, "load_queue", return_value=([], [])):
            result = app.load_operations(self.request)
        self.assertEqual(result[1].columns.tolist(), app.QUARANTINE_COLUMNS)
        self.assertEqual(result[3].columns.tolist(), app.RECOVERY_COLUMNS)
        self.assertIn("No open versions match", result[4])

    def test_selection_displays_raw_timestamps_and_resets_correction_form(self):
        row = {
            "purchase_timestamp_raw": "bad date", "purchase_timestamp": None,
            "showtime_raw": "2026-01-01T10:00:00", "ticket_price": -1,
        }
        with patch.object(app.quarantine, "load_record", return_value=row):
            result = app.select_quarantine_record("hash", self.request)
        self.assertIn("bad date", result[0])
        UUID(result[1])
        self.assertEqual(result[2:], ("{}", "", False, ""))

    def test_submit_announces_queued_request_not_successful_restoration(self):
        with patch.object(app.quarantine, "submit_request", return_value=123) as submit, patch.object(
            app.gr, "Info"
        ) as notify:
            result = app.submit_quarantine_correction(
                "hash", "id", '{"ticket_price":14.5}', "Receipt", True, self.request,
            )
        submit.assert_called_once_with(
            self.email, "id", "hash", '{"ticket_price":14.5}', "Receipt", True,
        )
        self.assertIn("not yet a successful restoration", result)
        notify.assert_called_once_with(result)

    def test_failed_submission_and_failed_reads_do_not_show_success(self):
        with patch.object(app.quarantine, "submit_request", side_effect=RuntimeError("Saved, job failed")), patch.object(
            app.gr, "Info"
        ) as notify, self.assertLogs(app.logger):
            with self.assertRaises(app.gr.Error):
                app.submit_quarantine_correction("hash", "id", "{}", "Receipt", True, self.request)
        notify.assert_not_called()
        with patch.object(app.quarantine, "load_queue", side_effect=RuntimeError("Missing ledger")), self.assertLogs(app.logger):
            with self.assertRaises(app.gr.Error):
                app.load_operations(self.request)


if __name__ == "__main__":
    unittest.main()
