import ast
import unittest
from pathlib import Path
from unittest.mock import MagicMock


SOURCE = Path(__file__).resolve().parents[1] / "src" / "notebooks" / "build_gold_theater_daily_sales.py"


class GoldLoadTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        definitions = [
            node for node in tree.body
            if isinstance(node, (ast.Assign, ast.FunctionDef))
        ]
        self.functions = MagicMock()
        self.functions.col.return_value = 1
        namespace = {"SparkSession": MagicMock(), "F": self.functions}
        exec(compile(ast.Module(body=definitions, type_ignores=[]), str(SOURCE), "exec"), namespace)
        self.query = namespace["DAILY_SALES_QUERY"]
        self.load = namespace["run_load"]
        self.spark = MagicMock()
        self.spark.table.return_value.groupBy.return_value.count.return_value.where.return_value.take.return_value = []

    def test_existing_target_refresh_does_not_enable_unsupported_table_feature(self):
        self.load(self.spark)
        self.assertEqual(self.spark.sql.call_args_list[0].args, (self.query,))
        self.assertEqual(self.spark.sql.call_count, 2)
        self.assertNotIn("delta.feature", SOURCE.read_text(encoding="utf-8"))
        writer = self.spark.sql.return_value.write.format.return_value
        writer.mode.assert_called_once_with("overwrite")
        writer.mode.return_value.option.assert_called_once_with("overwriteSchema", "true")
        writer.mode.return_value.option.return_value.saveAsTable.assert_called_once_with(
            "theater_analytics.gold.theater_daily_sales"
        )

    def test_time_expressions_preserve_nulls_midnight_and_seconds_and_reject_invalid_values(self):
        for column in ("open_time", "close_time"):
            self.assertIn(f"WHEN l.{column} IS NULL THEN CAST(NULL AS STRING)", self.query)
            self.assertIn(f"THEN concat(l.{column}, ':00') ELSE l.{column} END", self.query)
            self.assertIn(f"raise_error('Invalid theater layout {column}", self.query)
        self.assertNotIn("TIME(0)", self.query)

    def test_duplicate_sources_abort_before_any_gold_write(self):
        self.spark.table.return_value.groupBy.return_value.count.return_value.where.return_value.take.return_value = [object()]
        with self.assertRaisesRegex(ValueError, "duplicate keys"):
            self.load(self.spark)
        self.spark.sql.assert_not_called()


if __name__ == "__main__":
    unittest.main()
