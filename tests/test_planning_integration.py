import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path

from service_capacity.cli import main
from service_capacity.service import ApplicationService
from service_capacity.storage import Database


def observations(values, site="station-1", service="window"):
    rows = []
    for offset, value in enumerate(values):
        day = dt.date(2026, 11, 1) + dt.timedelta(days=offset)
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


class ApplicationServicePlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "db.sqlite3")
        self.service = ApplicationService(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def payload(self):
        return {
            "id": "station-1",
            "owner_id": "station-1",
            "history": [20, 22, 24, 26, 28, 30],
            "demand": 1,
            "observations": observations([20, 22, 24, 26, 28, 30]),
            "as_of": "2026-11-06",
            "capacity": {"station-1|window": 1},
        }

    def test_planning_fields_added_to_legacy_response(self):
        response = self.service.process(self.payload(), {}, "k1")
        self.assertEqual(response["value"], "32")
        self.assertEqual(response["planning"]["as_of"], "2026-11-06")
        self.assertIn("capacity_plan", response)
        self.assertIn("staffing", response)
        self.assertEqual(response["risk"], response["capacity_plan"]["status"])

    def test_legacy_payload_unchanged(self):
        payload = {"id": "station-1", "history": [10, 12, 14], "demand": 1}
        response = self.service.process(payload, {"window": 3, "capacity": "30"}, "legacy")
        self.assertEqual(response["value"], "14")
        self.assertNotIn("capacity_plan", response)

    def test_idempotent_planning_response(self):
        first = self.service.process(self.payload(), {}, "same")
        second = self.service.process(self.payload(), {}, "same")
        self.assertEqual(first, second)

    def test_adjustment_produces_comparison(self):
        payload = self.payload()
        payload["adjustments"] = {"default": 2}
        response = self.service.process(payload, {}, "k2")
        revision = response["revision"]
        self.assertIn("comparison", revision)
        self.assertTrue(revision["comparison"]["comparable"])
        self.assertGreaterEqual(revision["comparison"]["change_count"], 1)


class CliPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, **extra):
        data = {
            "as_of": "2026-11-08",
            "horizon": 2,
            "capacity": {"station-1|window": 2},
            "calendar": {"2026-11-09": {"factor": "1.2"}},
            "adjustments": {"default": 1},
            "observations": observations([20, 22, 24, 26, 28, 30, 32, 34]),
        }
        data.update(extra)
        path = self.root / "fixture.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_plan_command_writes_calibration_plan_and_audit(self):
        output = self.root / "plan.json"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(
                [
                    "--database",
                    str(self.root / "db.sqlite3"),
                    "plan",
                    "--fixture",
                    str(self.fixture()),
                    "--output",
                    str(output),
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(output.read_text(encoding="utf-8"))
        result = payload["results"][0]
        self.assertEqual(result["calibration"]["as_of"], "2026-11-08")
        self.assertEqual(len(result["calibration"]["forecast"]), 2)
        self.assertEqual(result["plan"]["status"], "capacity_shortage")
        self.assertIn("comparison", result["revision"])
        self.assertEqual(payload["audit"]["problems"], [])

    def test_insufficient_history_has_no_deterministic_headcount(self):
        output = self.root / "plan.json"
        with contextlib.redirect_stdout(io.StringIO()):
            main(
                [
                    "--database",
                    str(self.root / "db.sqlite3"),
                    "plan",
                    "--fixture",
                    str(self.fixture(observations=observations([20, 22]), as_of="2026-11-02", horizon=1)),
                    "--output",
                    str(output),
                ]
            )
        payload = json.loads(output.read_text(encoding="utf-8"))
        result = payload["results"][0]
        self.assertEqual(result["calibration"]["status"], "insufficient_history")
        self.assertEqual(result["plan"]["status"], "insufficient_history")
        self.assertIsNone(result["plan"]["rows"][0]["recommended_agents"])


if __name__ == "__main__":
    unittest.main()
