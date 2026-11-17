"""One-stop demand planning facade.

``DemandPlanningService`` wires calibration, tracing and capacity planning
together.  It is a thin composition layer: each step can also be used on its
own, but the facade guarantees that a capacity plan is always derived from a
traceable, point-in-time calibration run.
"""

from __future__ import annotations

from typing import Any, Mapping

from .capacity_plan import CapacityPlanner, explain_plan
from .forecast_calibration import ForecastCalibrator, visible_observations
from .trace import CalibrationLedger

__all__ = ["DemandPlanningService", "DemandPlanningError"]


class DemandPlanningError(RuntimeError):
    pass


class DemandPlanningService:
    def __init__(
        self,
        database: Any = None,
        calibration: ForecastCalibrator | None = None,
        planner: CapacityPlanner | None = None,
        ledger: CalibrationLedger | None = None,
    ):
        self.database = database
        self.calibration = calibration or ForecastCalibrator()
        self.planner = planner or CapacityPlanner()
        self.ledger = ledger or CalibrationLedger(database)

    # -- calibration -----------------------------------------------------
    def backtest(self, observations: Any, as_of: Any, **options: Any) -> dict[str, Any]:
        return self.calibration.rolling_backtest(observations, as_of, **options)

    def calibrate(self, observations: Any, as_of: Any, **options: Any) -> dict[str, Any]:
        result = self.calibration.calibrate(observations, as_of, **options)
        self.ledger.record(result)
        return result

    def visible(self, observations: Any, as_of: Any, **options: Any) -> dict[str, Any]:
        return visible_observations(observations, as_of, **options)

    # -- capacity --------------------------------------------------------
    def plan(
        self,
        calibration: Mapping[str, Any],
        capacity: Any = None,
        calendar: Any = None,
        **options: Any,
    ) -> dict[str, Any]:
        plan = self.planner.plan(calibration, capacity=capacity, calendar=calendar, **options)
        self.ledger.record_plan(plan, calibration)
        return plan

    def revise(self, plan: Mapping[str, Any], adjustments: Any, **options: Any) -> dict[str, Any]:
        revised = self.planner.revise(plan, adjustments, **options)
        revised["comparison"] = self.planner.compare(plan, revised)
        return revised

    def compare(self, base: Mapping[str, Any], revised: Mapping[str, Any]) -> dict[str, Any]:
        return self.planner.compare(base, revised)

    def explain(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        return explain_plan(plan)

    # -- convenience -----------------------------------------------------
    def run(
        self,
        observations: Any,
        as_of: Any,
        capacity: Any = None,
        calendar: Any = None,
        **options: Any,
    ) -> dict[str, Any]:
        calibration = self.calibrate(observations, as_of, calendar=calendar, **options)
        plan = self.plan(calibration, capacity=capacity, calendar=calendar)
        return {"calibration": calibration, "plan": plan}

    def audit(self) -> dict[str, Any]:
        return self.ledger.audit()
