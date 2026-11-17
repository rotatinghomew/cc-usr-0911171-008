"""Compatibility surface for rolling forecast calibration.

Callers sometimes import this module by a shorter name; it simply re-exports
the implementation from :mod:`.forecast_calibration`.
"""

from .forecast_calibration import *  # noqa: F401,F403
from .forecast_calibration import (  # noqa: F401
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
