import datetime as dt
import decimal
import unittest

from service_capacity.demand_planning.capacity_plan import (
    CapacityPlanner,
    CapacityPlanningError,
    StaffingConstraints,
    compare_plans,
    explain_plan,
    resolve_available_agents,
)
from service_capacity.demand_planning.forecast_calibration import calibrate_forecast


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


def calibration(values=(20, 22, 24, 26, 28, 30, 32), **options):
    rows = observations(values)
    return calibrate_forecast(rows, "2026-11-07", horizon=options.pop("horizon", 2), **options)


class ConstraintTests(unittest.TestCase):
    def test_agents_round_up(self):
        constraints = StaffingConstraints(service_minutes=30, shift_minutes=480, occupancy_target="0.8")
        self.assertEqual(constraints.agents_for(10), 1)
        self.assertEqual(constraints.agents_for(12.8), 1)
        self.assertEqual(constraints.agents_for(13), 2)

    def test_invalid_occupancy(self):
        with self.assertRaises(CapacityPlanningError):
            StaffingConstraints(occupancy_target="0")
        with self.assertRaises(CapacityPlanningError):
            StaffingConstraints(service_minutes="0")
        with self.assertRaises(CapacityPlanningError):
            StaffingConstraints(shift_minutes="-1")

    def test_resolve_agents_from_roster(self):
        roster = [
            {"date": "2026-11-08", "site_id": "s1", "service_type": "window", "agents": 4},
            {"date": "2026-11-08", "site_id": "s2", "service_type": "window", "agents": 9},
        ]
        self.assertEqual(
            resolve_available_agents(roster, "s1", "window", "2026-11-08"),
            decimal.Decimal("4"),
        )
        self.assertIsNone(resolve_available_agents(roster, "s3", "window", "2026-11-08"))
        self.assertEqual(
            resolve_available_agents({"default": 3}, "s1", "window", "2026-11-08"),
            decimal.Decimal("3"),
        )


class PlanTests(unittest.TestCase):
    def test_capacity_shortage_vs_forecast_uncertainty(self):
        planner = CapacityPlanner()
        # Forecast 34/36 -> 3 agents at 30 service minutes, 480 shift, 0.8.
        shortage = planner.plan(calibration(), capacity=1)
        self.assertEqual(shortage["status"], "capacity_shortage")
        self.assertTrue(all(row["status"] == "capacity_shortage" for row in shortage["rows"]))
        self.assertTrue(all(row["shortfall"] == "2" for row in shortage["rows"]))
        # Enough people for the low end but not the high end.
        uncertain_forecast = {
            "deterministic": True,
            "forecast_groups": [
                {
                    "site_id": "s1",
                    "service_type": "window",
                    "deterministic": True,
                    "forecast": [
                        {
                            "date": "2026-11-08",
                            "demand": "20",
                            "lower": "10",
                            "upper": "40",
                            "calendar_factor": "1",
                        }
                    ],
                }
            ],
        }
        uncertain = planner.plan(uncertain_forecast, capacity=1)
        row = uncertain["rows"][0]
        self.assertEqual(row["status"], "forecast_uncertainty")
        self.assertEqual(row["shortfall_lower"], "0")
        self.assertGreater(decimal.Decimal(row["shortfall_upper"]), 0)
        self.assertGreater(decimal.Decimal(row["uncertainty_shortfall"]), 0)

    def test_sufficient_capacity(self):
        plan = CapacityPlanner().plan(calibration(), capacity=10)
        self.assertEqual(plan["status"], "sufficient")
        self.assertEqual(plan["summary"]["shortfall_total"], "0")
        self.assertFalse(plan["rows"][0]["overloaded"])

    def test_capacity_unknown(self):
        plan = CapacityPlanner().plan(calibration())
        self.assertEqual(plan["status"], "capacity_unknown")
        row = plan["rows"][0]
        # Demand is known, so a requirement can still be stated; the roster
        # shortfall cannot.
        self.assertEqual(row["recommended_agents"], row["required_agents_point"])
        self.assertIsNone(row["available_agents"])
        self.assertIsNone(row["shortfall"])

    def test_insufficient_history_has_no_headcount(self):
        result = calibrate_forecast(observations((10, 12)), "2026-11-02", horizon=1)
        plan = CapacityPlanner().plan(result, capacity=1)
        row = plan["rows"][0]
        self.assertEqual(plan["status"], "insufficient_history")
        self.assertIsNone(row["recommended_agents"])
        self.assertIsNone(row["shortfall"])
        self.assertEqual(row["status"], "insufficient_history")
        self.assertIn("indicative_agents", row)
        self.assertEqual(plan["summary"]["recommended_agents_total"], 0)

    def test_explanation_by_site_and_service(self):
        rows = observations([10] * 7, site="s1", service="window") + observations(
            [20] * 7, site="s1", service="hotline"
        )
        result = calibrate_forecast(rows, "2026-11-07", horizon=1)
        plan = CapacityPlanner().plan(result, capacity={"s1": 5})
        explanation = plan["explanation"]
        self.assertIn("s1", explanation["by_site"])
        self.assertEqual(set(explanation["by_service"]), {"window", "hotline"})
        self.assertIn("s1|window", explanation["by_site_service"])
        self.assertIn("calibration", explanation["by_site_service"]["s1|window"])

    def test_calendar_closed_day(self):
        plan = CapacityPlanner().plan(
            calibration(calendar={"2026-11-08": {"closed": True}}), capacity=1
        )
        self.assertEqual(plan["rows"][0]["status"], "closed")
        self.assertEqual(plan["rows"][0]["recommended_agents"], 0)

    def test_existing_service_constraints_are_respected(self):
        constraints = StaffingConstraints(
            service_minutes=60, shift_minutes=240, occupancy_target="1"
        )
        plan = CapacityPlanner(constraints).plan(calibration(), capacity=1)
        row = plan["rows"][0]
        self.assertEqual(row["workload_minutes"], "2040")
        # 34 visits * 60 minutes / (240 * 1.0) -> 9 agents
        self.assertEqual(row["required_agents_point"], 9)
        self.assertEqual(plan["constraints"]["service_minutes"], "60")


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.planner = CapacityPlanner()
        self.base = self.planner.plan(calibration(), capacity=1)

    def test_revision_can_resolve_shortfall(self):
        revised = self.planner.revise(self.base, {"default": 3})
        self.assertEqual(revised["status"], "sufficient")
        comparison = self.planner.compare(self.base, revised)
        self.assertTrue(comparison["comparable"])
        self.assertEqual(comparison["change_count"], 2)
        self.assertEqual(comparison["improved"], 2)
        self.assertTrue(comparison["constraints_unchanged"])
        change = comparison["changes"][0]
        self.assertEqual(change["from_status"], "capacity_shortage")
        self.assertEqual(change["to_status"], "sufficient")
        self.assertEqual(change["fields"]["shortfall"], {"from": "2", "to": "0"})

    def test_revision_keyed_by_date(self):
        revised = self.planner.revise(self.base, {"2026-11-08": {"agents": 5}})
        dates = {row["date"]: row for row in revised["rows"]}
        self.assertEqual(dates["2026-11-08"]["available_agents"], 5)
        self.assertEqual(dates["2026-11-09"]["available_agents"], 1)

    def test_revision_list_shape(self):
        revised = self.planner.revise(
            self.base,
            [
                {"date": "2026-11-08", "site_id": "s1", "service_type": "window", "agents": 4},
                {"date": "2026-11-09", "delta": 1},
            ],
        )
        dates = {row["date"]: row for row in revised["rows"]}
        self.assertEqual(dates["2026-11-08"]["available_agents"], 4)
        self.assertEqual(dates["2026-11-09"]["available_agents"], 2)

    def test_revision_never_drops_below_zero(self):
        revised = self.planner.revise(self.base, -100)
        self.assertTrue(all(row["available_agents"] == 0 for row in revised["rows"]))

    def test_comparison_keeps_constraints(self):
        revised = self.planner.revise(self.base, {"default": 3})
        comparison = compare_plans(self.base, revised)
        self.assertEqual(comparison["base"]["constraints"], comparison["revised"]["constraints"])
        self.assertEqual(
            set(comparison["base"]["summary"]), set(comparison["revised"]["summary"])
        )

    def test_explanation_after_revision(self):
        revised = self.planner.revise(self.base, {"default": 3})
        explanation = explain_plan(revised)
        self.assertEqual(explanation["by_site"]["s1"]["sufficient_days"], 2)


class EndToEndTests(unittest.TestCase):
    def test_backfill_does_not_become_known_forecast(self):
        rows = observations((20, 22, 24, 26, 28, 30, 32))
        # A late correction for the last day arrives after the decision date.
        rows.append(
            {
                "date": "2026-11-07",
                "value": 999,
                "site_id": "s1",
                "service_type": "window",
                "recorded_at": "2026-11-20T20:00:00",
            }
        )
        result = calibrate_forecast(rows, "2026-11-07", horizon=1)
        self.assertEqual(result["forecast"][0]["demand"], "34")
        self.assertEqual(result["forecast"][0]["date"], "2026-11-08")
        self.assertEqual(result["excluded_late"][0]["date"], "2026-11-07")

    def test_plan_uses_calibrated_not_raw_demand(self):
        rows = observations((10, 10, 10, 10, 10, 10, 10))
        result = calibrate_forecast(rows, "2026-11-07", horizon=1)
        row = CapacityPlanner().plan(result, capacity=1)["rows"][0]
        self.assertEqual(row["demand"], "10")
        self.assertEqual(row["required_agents_point"], 1)


if __name__ == "__main__":
    unittest.main()
