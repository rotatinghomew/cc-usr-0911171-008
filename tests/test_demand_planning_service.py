import datetime as dt
import unittest

from service_capacity.demand_planning import DemandPlanningService
from service_capacity.storage import Database


def observations(values, start=1, site="s1", service="window"):
    rows = []
    for offset, value in enumerate(values):
        day = dt.date(2026, 11, start) + dt.timedelta(days=offset)
        rows.append(
            {
                "date": day.isoformat(),
                "value": value,
                "site_id": site,
                "service_type": service,
                "recorded_at": day.isoformat() + "T20:00:00",
            }
        )
    return rows


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.service = DemandPlanningService()
        self.result = self.service.calibrate(
            observations((20, 22, 24, 26, 28, 30, 32)), "2026-11-07", horizon=1
        )

    def test_record_keeps_provenance(self):
        run = self.service.ledger.latest()
        self.assertIsNotNone(run)
        self.assertEqual(run.as_of, "2026-11-07")
        self.assertEqual(run.source_fingerprint, self.result["source_fingerprint"])
        self.assertTrue(run.result_digest)
        self.assertEqual(run.status, "ok")

    def test_audit_is_clean(self):
        self.service.plan(self.result, capacity=2)
        audit = self.service.audit()
        self.assertEqual(audit["runs"], 1)
        self.assertEqual(audit["plans"], 1)
        self.assertEqual(audit["problems"], [])
        self.assertEqual(audit["latest"]["source_fingerprint"], self.result["source_fingerprint"])

    def test_run_bundles_calibration_and_plan(self):
        bundle = self.service.run(
            observations((20, 22, 24, 26, 28, 30, 32)), "2026-11-07", capacity=2
        )
        self.assertEqual(bundle["calibration"]["status"], "ok")
        self.assertIn(bundle["plan"]["status"], {"capacity_shortage", "forecast_uncertainty"})
        self.assertIn("explanation", bundle["plan"])

    def test_revision_comparison_is_attached(self):
        plan = self.service.plan(self.result, capacity=1)
        revised = self.service.revise(plan, {"default": 3})
        self.assertIn("comparison", revised)
        self.assertEqual(revised["comparison"]["improved"], len(revised["rows"]))


class PersistenceTests(unittest.TestCase):
    def test_runs_survive_database_round_trip(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "plan.sqlite3")
            service = DemandPlanningService(database)
            service.calibrate(observations((10, 12, 14, 16, 18, 20)), "2026-11-06", horizon=1)
            from service_capacity.demand_planning.trace import CalibrationLedger

            restored = CalibrationLedger(database)
            runs = restored.restore()
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0].as_of, "2026-11-06")
            self.assertEqual(restored.audit()["problems"], [])


if __name__ == "__main__":
    unittest.main()
