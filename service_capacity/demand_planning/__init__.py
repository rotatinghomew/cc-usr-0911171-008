"""Demand planning primitives: forecasting, rolling calibration, capacity plans."""

from .capacity_model import CapacityModel
from .forecast_calibration import (
    Backtester,
    CalibrationEngine,
    ForecastCalibration,
    ForecastCalibrationError,
    ForecastCalibrator,
    RollingBacktester,
    calibrate_forecast,
    error_metrics,
    expand_observations,
    normalize_calendar,
    rolling_backtest,
    visible_observations,
)
from .capacity_plan import (
    CapacityPlanner,
    CapacityPlanningError,
    StaffingConstraints,
    compare_plans,
    explain_plan,
    resolve_available_agents,
)
from .trace import CalibrationLedger, CalibrationRun, TraceError
from .service import DemandPlanningError, DemandPlanningService

__all__ = [
    "CapacityModel",
    "ForecastCalibrationError",
    "ForecastCalibrator",
    "ForecastCalibration",
    "CalibrationEngine",
    "RollingBacktester",
    "Backtester",
    "calibrate_forecast",
    "rolling_backtest",
    "error_metrics",
    "expand_observations",
    "normalize_calendar",
    "visible_observations",
    "CapacityPlanner",
    "CapacityPlanningError",
    "StaffingConstraints",
    "compare_plans",
    "explain_plan",
    "resolve_available_agents",
    "CalibrationLedger",
    "CalibrationRun",
    "TraceError",
    "DemandPlanningService",
    "DemandPlanningError",
]
