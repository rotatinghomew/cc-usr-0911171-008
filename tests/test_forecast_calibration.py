import datetime as dt
import decimal
import unittest

from service_capacity.demand_planning import forecast_calibration as calibration
from service_capacity.demand_planning.forecast_calibration import (
    ForecastCalibrationError,
    ForecastCalibrator,
    calibrate_forecast,
    error_metrics,
    expand_observations,
    rolling_backtest,
    visible_observations,
)

UTC = dt.timezone.utc


def series(values, start=1, recorded_at=None, **extra):
    rows = []
    for offset, value in enumerate(values):
        day = dt.date(2026, 11, start) + dt.timedelta(days=offset)
        row = {"date": day.isoformat(), "value": value}
        row.update(extra)
        if recorded_at is not None:
            row["recorded_at"] = recorded_at(day)
        rows.append(row)
    return rows


class PointInTimeTests(unittest.TestCase):
    def test_late_record_is_not_visible(self):
        rows = series([10, 12, 14, 16])
        rows[3]["recorded_at"] = "2026-11-20T09:00:00"
        view = visible_observations(rows, "2026-11-10")
        self.assertEqual([row["date"] for row in view["observations"]], [
            dt.date(2026, 11, 1),
            dt.date(2026, 11, 2),
            dt.date(2026, 11, 3),
        ])
        self.assertEqual(view["excluded_late"][0]["date"], "2026-11-04")
        self.assertEqual(view["excluded_late"][0]["reason"], "recorded_after_as_of")

    def test_latest_visible_revision_wins(self):
        rows = [
            {"date": "2026-11-01", "value": 10, "recorded_at": "2026-11-01T20:00:00"},
            {"date": "2026-11-01", "value": 11, "recorded_at": "2026-11-03T20:00:00"},
            {"date": "2026-11-01", "value": 12, "recorded_at": "2026-11-20T20:00:00"},
        ]
        first = visible_observations(rows, "2026-11-02")
        second = visible_observations(rows, "2026-11-05")
        third = visible_observations(rows, "2026-11-21")
        self.assertEqual(calibration.format_decimal(first["observations"][0]["value"]), "10")
        self.assertEqual(calibration.format_decimal(second["observations"][0]["value"]), "11")
        self.assertEqual(calibration.format_decimal(third["observations"][0]["value"]), "12")

    def test_conflicting_observations_are_reported(self):
        rows = [
            {"date": "2026-11-01", "value": 10, "recorded_at": "2026-11-01T20:00:00"},
            {"date": "2026-11-01", "value": 99, "recorded_at": "2026-11-01T20:00:00"},
        ]
        view = visible_observations(rows, "2026-11-02")
        self.assertEqual(len(view["conflicts"]), 1)
        self.assertEqual(view["conflicts"][0]["values"], ["10", "99"])

    def test_missing_days_are_not_imputed(self):
        rows = [
            {"date": "2026-11-01", "value": 10},
            {"date": "2026-11-03", "value": 14},
        ]
        view = visible_observations(rows, "2026-11-05")
        self.assertEqual(view["missing_dates"], ["2026-11-02"])

    def test_absent_recorded_at_is_assumed_end_of_day(self):
        rows = [{"date": "2026-11-01", "value": 10}]
        view = visible_observations(rows, "2026-11-01")
        self.assertTrue(view["observations"][0]["assumed_known_at"])
        later = visible_observations(rows, "2026-10-31")
        self.assertEqual(later["observations"], [])

    def test_decision_date_limits_observations(self):
        rows = series([10, 12, 14, 16, 18])
        view = visible_observations(rows, "2026-11-03")
        self.assertEqual(len(view["observations"]), 3)
        self.assertTrue(all(item["date"].day <= 3 for item in view["observations"]))


class InputShapeTests(unittest.TestCase):
    def test_neutral_observation_envelope(self):
        rows = expand_observations(
            [
                {
                    "date": "2026-11-01",
                    "value": 5,
                    "dimensions": {"site_id": "s1", "service_type": "window"},
                }
            ]
        )
        self.assertEqual((rows[0]["site_id"], rows[0]["service_type"]), ("s1", "window"))

    def test_nested_site_service_shape(self):
        rows = expand_observations(
            {
                "s1": {
                    "window": [
                        {"date": "2026-11-01", "value": 5},
                        {"date": "2026-11-02", "value": 6},
                    ]
                }
            }
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["service_type"] for row in rows}, {"window"})

    def test_date_value_series(self):
        rows = expand_observations({"2026-11-01": 5, "2026-11-02": 6})
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["value"], decimal.Decimal("6"))

    def test_invalid_row_raises(self):
        with self.assertRaises(ForecastCalibrationError):
            expand_observations([{"date": "not-a-date", "value": 1}])
        with self.assertRaises(ForecastCalibrationError):
            expand_observations([{"date": "2026-11-01"}])


class RollingBacktestTests(unittest.TestCase):
    def linear(self):
        return series([20, 22, 24, 26, 28, 30, 32], recorded_at=lambda day: day.isoformat() + "T20:00:00")

    def test_folds_use_only_visible_training_data(self):
        result = rolling_backtest(self.linear(), "2026-11-07", horizon=1)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["deterministic"])
        for fold in result["folds"]:
            self.assertLessEqual(fold["origin"], fold["target"])
            self.assertEqual(fold["training_gaps"], [])

    def test_late_backfill_excluded_from_training(self):
        captured = {}

        class Spy(ForecastCalibrator):
            def _point_in_time_forecast(self, train, target, calendar, window=7):
                captured[target.isoformat()] = [
                    row["date"].isoformat() for row in train
                ]
                return super()._point_in_time_forecast(train, target, calendar, window)

        rows = series([10, 12, 14, 16, 18, 20])
        rows[2]["recorded_at"] = "2026-11-20T10:00:00"
        result = Spy().rolling_backtest(rows, "2026-11-10", horizon=1)
        self.assertTrue(captured)
        for target, dates in captured.items():
            # The late record is never training evidence, even for later
            # origins that occur before it was recorded.
            self.assertNotIn("2026-11-03", dates)
        # Because it is absent, it surfaces as a gap rather than being filled.
        self.assertTrue(
            any("2026-11-03" in fold["training_gaps"] for fold in result["folds"])
        )

    def test_insufficient_history_is_not_deterministic(self):
        result = rolling_backtest(series([10, 12]), "2026-11-02", horizon=1)
        self.assertEqual(result["status"], "insufficient_history")
        self.assertFalse(result["deterministic"])
        self.assertIn("insufficient_history", result["reasons"])

    def test_scored_actual_backfill_is_reported(self):
        rows = series([10, 12, 14, 16, 18])
        # Visible before the decision date but recorded two days after the day
        # it describes: known for planning, yet not knowable at forecast time.
        rows[3]["recorded_at"] = "2026-11-06T10:00:00"
        relaxed = rolling_backtest(rows, "2026-11-06", horizon=2, strict_point_in_time=False)
        strict = rolling_backtest(
            rows, "2026-11-06", horizon=2, strict_point_in_time=True
        )
        self.assertIn("scored_actuals_include_backfill", relaxed["reasons"])
        self.assertNotIn("scored_actuals_include_backfill", strict["reasons"])
        self.assertTrue(
            any(item["reason"] == "actual_backfilled_after_target" for item in strict["groups"][0]["skipped"])
        )

    def test_missing_actual_skips_fold(self):
        rows = [
            {"date": "2026-11-01", "value": 10},
            {"date": "2026-11-02", "value": 12},
            {"date": "2026-11-03", "value": 14},
            {"date": "2026-11-05", "value": 18},
        ]
        result = rolling_backtest(rows, "2026-11-05", horizon=1)
        targets = [fold["target"] for fold in result["folds"]]
        self.assertNotIn("2026-11-04", targets)
        self.assertIn("2026-11-04", result["missing_dates"])

    def test_metrics_by_site_and_service(self):
        rows = []
        for site, service, base in (("s1", "window", 10), ("s2", "hotline", 40)):
            for offset in range(6):
                day = dt.date(2026, 11, 1) + dt.timedelta(days=offset)
                rows.append(
                    {
                        "date": day.isoformat(),
                        "value": base + offset,
                        "site_id": site,
                        "service_type": service,
                        "recorded_at": day.isoformat() + "T20:00:00",
                    }
                )
        result = rolling_backtest(rows, "2026-11-06", horizon=1)
        self.assertEqual(set(result["by_site"]), {"s1", "s2"})
        self.assertEqual(set(result["by_service"]), {"window", "hotline"})
        self.assertIn("s1|window", result["by_site_service"])
        self.assertEqual(result["count"], len(result["folds"]))

    def test_calibration_factor_bounded(self):
        rows = series([10, 12, 14, 16, 18, 20], start=1)
        rows = [
            {**row, "recorded_at": row["date"] + "T20:00:00"}
            for row in rows
        ]
        # Actuals far above predictions should push the raw ratio high, but the
        # factor stays within the configured bound.
        result = calibrate_forecast(
            rows, "2026-11-06", horizon=1, max_factor="1.1"
        )
        self.assertLessEqual(
            decimal.Decimal(result["calibration_factor"]), decimal.Decimal("1.1")
        )


class CalibrateTests(unittest.TestCase):
    def rows(self):
        return series(
            [20, 22, 24, 26, 28, 30, 32],
            recorded_at=lambda day: day.isoformat() + "T20:00:00",
        )

    def test_calibrated_forecast_dates_follow_decision_date(self):
        result = calibrate_forecast(self.rows(), "2026-11-07", horizon=2)
        self.assertEqual(
            [item["date"] for item in result["forecast"]],
            ["2026-11-08", "2026-11-09"],
        )
        self.assertEqual(result["forecast"][0]["demand"], "34")

    def test_calendar_factor_applied(self):
        result = calibrate_forecast(
            self.rows(), "2026-11-07", horizon=1, calendar={"2026-11-08": {"factor": "2"}}
        )
        self.assertEqual(result["forecast"][0]["demand"], "68")

    def test_interval_is_ordered(self):
        result = calibrate_forecast(self.rows(), "2026-11-07", horizon=1)
        item = result["forecast"][0]
        self.assertLessEqual(decimal.Decimal(item["lower"]), decimal.Decimal(item["demand"]))
        self.assertLessEqual(decimal.Decimal(item["demand"]), decimal.Decimal(item["upper"]))

    def test_insufficient_history_forecast_not_deterministic(self):
        result = calibrate_forecast(self.rows()[:2], "2026-11-02", horizon=1)
        self.assertFalse(result["deterministic"])
        self.assertEqual(result["status"], "insufficient_history")

    def test_requires_decision_date(self):
        with self.assertRaises(ForecastCalibrationError):
            calibrate_forecast(self.rows())


class MetricTests(unittest.TestCase):
    def test_empty_metrics(self):
        result = error_metrics([])
        self.assertEqual(result["count"], 0)
        self.assertIsNone(result["mae"])

    def test_known_values(self):
        folds = [
            {"actual": 10, "predicted": 12},
            {"actual": 20, "predicted": 18},
        ]
        result = error_metrics(folds)
        self.assertEqual(result["mae"], "2")
        self.assertEqual(result["bias"], "0")
        self.assertEqual(result["rmse"], "2")

    def test_missing_pairs_are_skipped(self):
        result = error_metrics([{"actual": 10, "predicted": None}, {"actual": 4, "predicted": 6}])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["mae"], "2")


if __name__ == "__main__":
    unittest.main()
