import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


class RecoveryWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.job = yaml.safe_load(
            (ROOT / "resources" / "jobs.yml").read_text()
        )["resources"]["jobs"]["build_silver_theater_data"]
        cls.tasks = {task["task_key"]: task for task in cls.job["tasks"]}

    def test_one_queued_writer_and_reference_dependencies(self):
        self.assertEqual(self.job["max_concurrent_runs"], 1)
        self.assertTrue(self.job["queue"]["enabled"])
        task = self.tasks["build_silver_pos_ticket_sales"]
        self.assertTrue(task["notebook_task"]["notebook_path"].endswith("process_quarantine_recovery.py"))
        self.assertEqual(
            {item["task_key"] for item in task["depends_on"]},
            {"build_silver_theater_layout", "build_silver_imdb_metadata"},
        )

    def test_gold_and_completion_run_only_after_successful_recovery_and_enrichment(self):
        condition = self.tasks["has_applied_recoveries"]["condition_task"]
        self.assertEqual(condition["left"], "{{tasks.build_silver_pos_ticket_sales.values.recovery_applied}}")
        self.assertEqual(condition["right"], "true")
        gold = self.tasks["refresh_gold_after_recovery"]
        self.assertEqual(gold["depends_on"], [
            {"task_key": "build_silver_denormalized_theater_sales"},
            {"task_key": "has_applied_recoveries", "outcome": "true"},
        ])
        self.assertEqual(self.tasks["complete_quarantine_recovery"]["depends_on"], [
            {"task_key": "refresh_gold_after_recovery"},
        ])
        for task in (
            "build_silver_pos_ticket_sales", "build_silver_denormalized_theater_sales",
            "refresh_gold_after_recovery", "complete_quarantine_recovery",
        ):
            self.assertEqual(self.tasks[task]["max_retries"], 2)

    def test_app_manifests_bind_job_resource_and_same_operator_configuration(self):
        app = yaml.safe_load(
            (ROOT / "resources" / "apps.yml").read_text()
        )["resources"]["apps"]["reel_insights_app"]
        standalone = yaml.safe_load((ROOT / "src" / "app" / "app.yaml").read_text())
        standalone_env = [
            {("value_from" if key == "valueFrom" else key): value for key, value in item.items()}
            for item in standalone["env"]
        ]
        self.assertEqual(app["config"]["env"], standalone_env)
        self.assertEqual(
            {item["name"]: item["valueFrom"] for item in standalone["env"] if "valueFrom" in item},
            {"DATABRICKS_WAREHOUSE_ID": "sql-warehouse", "QUARANTINE_RECOVERY_JOB_ID": "quarantine-recovery-job"},
        )
        job = next(item for item in app["resources"] if item["name"] == "quarantine-recovery-job")
        self.assertEqual(job["job"], {
            "id": "${resources.jobs.build_silver_theater_data.id}", "permission": "CAN_MANAGE_RUN",
        })

    def test_app_does_not_receive_sales_or_quarantine_modify_grants(self):
        source = (ROOT / "src" / "notebooks" / "grant_quarantine_access.py").read_text()
        self.assertIn("GRANT SELECT, MODIFY ON TABLE theater_analytics.silver.quarantine_recovery_requests", source)
        self.assertNotIn("GRANT SELECT, MODIFY ON TABLE theater_analytics.silver.pos_ticket_sales", source)
        self.assertNotIn("GRANT MODIFY ON", source)

    def test_existing_pos_normalizer_is_reused_and_old_ingestion_is_preserved(self):
        source = (ROOT / "src" / "notebooks" / "process_quarantine_recovery.py").read_text()
        self.assertIn("# MAGIC %run ./build_silver_pos_ticket_sales", source)
        self.assertIn("normalize_sales(raw)", source)
        self.assertIn("should_quarantine()", source)
        self.assertNotIn('withColumn("ingestion_timestamp"', source)
        self.assertNotIn('withColumn("ingestion_date"', source)
        combined = (ROOT / "src" / "notebooks" / "build_silver_denormalized_theater_sales.py").read_text()
        self.assertIn('replay = replay | F.col("_recovery_hash").isNotNull()', combined)
        self.assertIn("left_anti", combined)


if __name__ == "__main__":
    unittest.main()
