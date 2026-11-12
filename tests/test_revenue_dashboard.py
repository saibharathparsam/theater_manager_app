import json
import sqlite3
import unittest
from pathlib import Path


DASHBOARD = Path(__file__).resolve().parents[1] / "src" / "dashboard" / "Theater_Revenue_Insights.lvdash.json"


class RevenueDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        cls.datasets = {item["name"]: item for item in cls.dashboard["datasets"]}
        cls.page = next(
            page for page in cls.dashboard["pages"] if page["name"] == "approved_decisions_page"
        )
        cls.movie_page = next(
            page for page in cls.dashboard["pages"] if page["name"] == "by_movie"
        )
        cls.movie_widgets = {
            entry["widget"]["name"]: entry["widget"] for entry in cls.movie_page["layout"]
        }

    def test_movie_page_removes_requested_widgets_and_stale_matrix_guidance(self):
        for name in ("kpi_total_revenue", "kpi_showings", "scatter_placement"):
            self.assertNotIn(name, self.movie_widgets)
        self.assertNotIn("Placement matrix", json.dumps(self.movie_page))
        self.assertIn("kpi_total_movies", self.movie_widgets)
        self.assertIn("kpi_revenue_per_showing", self.movie_widgets)

    def test_movie_count_uses_distinct_known_ids_not_sales_rows_or_titles(self):
        config = self.datasets["theater_sales_metrics"]["config"]
        measure = next(item for item in config["measures"] if item["name"] == "total_movies")
        with sqlite3.connect(":memory:") as connection:
            connection.execute("CREATE TABLE sales (movie_id TEXT, movie_title TEXT)")
            connection.executemany("INSERT INTO sales VALUES (?, ?)", [
                ("m1", "Same title"), ("m1", "Same title"), ("m2", "Same title"),
                (None, "Missing"), ("", "Missing"), ("  ", "Missing"),
                ("Unknown", "Missing"), ("unknown", "Missing"),
            ])
            count = connection.execute(f"SELECT {measure['expr']} FROM sales").fetchone()[0]
        self.assertEqual(count, 2)
        query = self.movie_widgets["kpi_total_movies"]["queries"][0]["query"]
        self.assertEqual(query["fields"], [{
            "name": "measure(total_movies)", "expression": "MEASURE(`total_movies`)",
        }])
        self.assertFalse(query["disaggregated"])

    def test_movie_filter_and_all_movie_views_share_one_dataset(self):
        widget = self.movie_widgets["filter_movie"]
        self.assertEqual(widget["spec"]["widgetType"], "filter-multi-select")
        self.assertEqual(widget["spec"]["version"], 2)
        binding = widget["spec"]["encodings"]["fields"][0]
        query = widget["queries"][0]
        self.assertEqual(binding["queryName"], query["name"])
        self.assertEqual(binding["fieldName"], "movie_title")
        self.assertEqual(query["query"]["fields"], [
            {"name": "movie_title", "expression": "`movie_title`"},
        ])
        self.assertFalse(query["query"]["disaggregated"])
        for widget in self.movie_widgets.values():
            for query in widget.get("queries", []):
                self.assertEqual(query["query"]["datasetName"], "theater_sales_metrics")

    def test_movie_locations_lists_distinct_movie_theater_state_combinations(self):
        widget = self.movie_widgets["table_movie_locations"]
        query = widget["queries"][0]["query"]
        fields = {"movie_id", "movie_title", "theater", "state"}
        self.assertEqual({field["name"] for field in query["fields"]}, fields)
        self.assertFalse(query["disaggregated"])
        self.assertEqual(widget["spec"]["version"], 2)
        self.assertEqual(widget["spec"]["widgetType"], "table")
        self.assertEqual(
            {column["fieldName"] for column in widget["spec"]["encodings"]["columns"]},
            fields,
        )
        config = self.datasets["theater_sales_metrics"]["config"]
        dimensions = {item["name"]: item["expr"] for item in config["dimensions"]}
        self.assertIn("theater_id", dimensions["theater"])
        self.assertEqual(dimensions["movie_id"], "movie_id")
        self.assertEqual(config["source"], "theater_analytics.gold.theater_daily_sales")

    def test_movie_page_layout_fills_rows_without_overlaps(self):
        positions = [item["position"] for item in self.movie_page["layout"]]
        for y in range(max(p["y"] + p["height"] for p in positions)):
            cells = []
            for p in positions:
                if p["y"] <= y < p["y"] + p["height"]:
                    cells.extend(range(p["x"], p["x"] + p["width"]))
            self.assertEqual(sorted(cells), list(range(12)), f"Invalid grid row {y}")
        for entry in self.movie_page["layout"]:
            if entry["widget"].get("spec", {}).get("widgetType") == "counter":
                self.assertGreaterEqual(entry["position"]["height"], 3)

    def test_existing_pages_and_new_decision_page_are_present(self):
        self.assertEqual(
            {page["displayName"] for page in self.dashboard["pages"]},
            {"Overall performance", "By Movie", "By Theater", "Approved decisions"},
        )
        self.assertEqual(self.page["pageType"], "PAGE_TYPE_CANVAS")
        self.assertEqual(self.page["layoutVersion"], "GRID_V1")

    def test_feedback_dataset_is_read_only_gold_with_no_history_limit(self):
        dataset = self.datasets["approved_decisions"]
        self.assertEqual((dataset["catalog"], dataset["schema"]), ("theater_analytics", "gold"))
        query = "".join(dataset["queryLines"])
        self.assertIn("FROM decision_feedback\n", query)
        self.assertIn("ORDER BY approved_at DESC, decision_id DESC", query)
        self.assertIn("WHEN theater_id = '' THEN 'All theaters'", query)
        for word in ("INSERT", "UPDATE", "DELETE", "MERGE", "LIMIT"):
            self.assertNotIn(word, query.upper())

    def test_all_widget_queries_reference_existing_datasets(self):
        for page in self.dashboard["pages"]:
            for entry in page["layout"]:
                for query in entry["widget"].get("queries", []):
                    self.assertIn(query["query"]["datasetName"], self.datasets)

    def test_all_encoding_fields_match_widget_queries(self):
        def encoded_fields(value):
            if isinstance(value, dict):
                if "fieldName" in value:
                    yield value["fieldName"]
                for child in value.values():
                    yield from encoded_fields(child)
            elif isinstance(value, list):
                for child in value:
                    yield from encoded_fields(child)

        for page in self.dashboard["pages"]:
            for entry in page["layout"]:
                widget = entry["widget"]
                fields = {
                    field["name"] for query in widget.get("queries", [])
                    for field in query["query"]["fields"]
                }
                for name in encoded_fields(widget.get("spec", {}).get("encodings", {})):
                    with self.subTest(page=page["name"], widget=widget["name"], field=name):
                        self.assertIn(name, fields)

    def test_decision_table_includes_complete_audit_evidence(self):
        widget = next(
            item["widget"] for item in self.page["layout"]
            if item["widget"]["name"] == "approved_decisions_table"
        )
        query = widget["queries"][0]["query"]
        fields = {field["name"] for field in query["fields"]}
        self.assertEqual(fields, {
            "approved_at", "action", "movie_title", "movie_id", "theater_scope",
            "period_start", "period_end", "approver_email", "approval_note",
            "decision_id", "source_table", "evidence_json",
        })
        self.assertTrue(query["disaggregated"])
        self.assertEqual(query["orders"], [
            {"direction": "DESC", "expression": "`approved_at`"},
            {"direction": "DESC", "expression": "`decision_id`"},
        ])
        self.assertEqual(widget["spec"]["version"], 2)
        self.assertEqual(
            {column["fieldName"] for column in widget["spec"]["encodings"]["columns"]},
            fields,
        )

    def test_decision_filters_bind_to_the_same_audit_dataset(self):
        filters = [
            item["widget"] for item in self.page["layout"]
            if item["widget"].get("spec", {}).get("widgetType", "").startswith("filter-")
        ]
        self.assertEqual(len(filters), 3)
        names = set()
        for widget in filters:
            query = widget["queries"][0]
            self.assertEqual(query["query"]["datasetName"], "approved_decisions")
            self.assertFalse(query["query"]["disaggregated"])
            binding = widget["spec"]["encodings"]["fields"][0]
            self.assertEqual(binding["queryName"], query["name"])
            self.assertEqual(binding["fieldName"], query["query"]["fields"][0]["name"])
            names.add(binding["fieldName"])
        self.assertEqual(names, {"approval_date", "action", "theater_scope"})

    def test_decision_page_layout_has_no_overlaps_or_out_of_bounds_widgets(self):
        rectangles = [item["position"] for item in self.page["layout"]]
        for index, rect in enumerate(rectangles):
            self.assertGreaterEqual(rect["x"], 0)
            self.assertLessEqual(rect["x"] + rect["width"], 12)
            for other in rectangles[index + 1:]:
                overlaps = (
                    rect["x"] < other["x"] + other["width"]
                    and other["x"] < rect["x"] + rect["width"]
                    and rect["y"] < other["y"] + other["height"]
                    and other["y"] < rect["y"] + rect["height"]
                )
                self.assertFalse(overlaps)


if __name__ == "__main__":
    unittest.main()
