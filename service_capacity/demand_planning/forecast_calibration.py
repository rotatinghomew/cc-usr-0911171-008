"""Point-in-time forecast calibration and rolling backtests.

The module deliberately separates *when a value happened* (``date``) from
*when the organisation could know it* (``recorded_at`` and its aliases).  A
rolling backtest only ever trains on records that were known at the origin of
the forecast.  Missing calendar days are reported as missing; they are never
silently promoted to observed values in the scored actuals.

The public helpers are intentionally tolerant about input spelling because the
same observations arrive from a number of upstream feeds.  Every accepted
alias is recorded in the returned provenance so the result remains auditable.
"""

from __future__ import annotations

import datetime as dt
import decimal
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from ..contracts import fingerprint

__all__ = [
    "ForecastCalibrationError",
    "ForecastCalibrator",
    "ForecastCalibration",
    "CalibrationEngine",
    "RollingBacktester",
    "Backtester",
    "as_date",
    "as_decimal",
    "format_decimal",
    "expand_observations",
    "normalize_calendar",
    "visible_observations",
    "rolling_backtest",
    "calibrate_forecast",
    "error_metrics",
]


class ForecastCalibrationError(ValueError):
    """Raised when a calibration request cannot be interpreted."""


class CalibrationResult(dict):
    """Dictionary result that also supports attribute access.

    The project's public surface is dictionary based (the CLI serialises
    results directly), but object-style access keeps the calibration API
    pleasant for programmatic callers.
    """

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


# ---------------------------------------------------------------------------
# Small parsing / formatting helpers
# ---------------------------------------------------------------------------

_DATE_FIELDS = (
    "date",
    "day",
    "period",
    "observed_on",
    "observation_date",
    "effective_from",
    "occurred_on",
)
_VALUE_FIELDS = ("value", "demand", "quantity", "count", "observed_value", "actual")
_SITE_FIELDS = (
    "site_id",
    "site",
    "station_id",
    "station",
    "owner_id",
    "location_id",
    "place_id",
)
_SERVICE_FIELDS = (
    "service_type",
    "service",
    "service_kind",
    "channel",
    "queue",
    "window_type",
    "category",
    "service_channel",
)
_KNOWN_FIELDS = (
    "recorded_at",
    "recorded_on",
    "known_at",
    "knowledge_time",
    "available_at",
    "observed_at",
    "captured_at",
    "ingested_at",
    "inserted_at",
    "reported_at",
    "updated_at",
)
_CALENDAR_FIELDS = ("factor", "season_index", "multiplier", "value")


def _first(row: Mapping[str, Any], names: Sequence[str], default: Any = None) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return default


def as_date(value: Any, name: str = "date") -> dt.date:
    """Coerce a date/ISO string into ``datetime.date``."""

    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            return dt.date.fromisoformat(text)
        except ValueError:
            try:
                return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).date()
            except ValueError as exc:
                raise ForecastCalibrationError(f"{name} must be an ISO date") from exc
    raise ForecastCalibrationError(f"{name} must be a date")


def _as_datetime(value: Any, name: str = "recorded_at") -> dt.datetime:
    if isinstance(value, dt.datetime):
        moment = value
    elif isinstance(value, dt.date):
        # A bare date means "known by the end of that day".
        moment = dt.datetime.combine(value, dt.time.max, tzinfo=dt.timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        try:
            moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                moment = dt.datetime.combine(
                    dt.date.fromisoformat(text), dt.time.max, tzinfo=dt.timezone.utc
                )
            except ValueError as exc:
                raise ForecastCalibrationError(
                    f"{name} must be an ISO date or datetime"
                ) from exc
    else:
        raise ForecastCalibrationError(f"{name} must be a date or datetime")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc)


def as_decimal(value: Any, name: str = "value") -> decimal.Decimal:
    if isinstance(value, bool) or value is None:
        raise ForecastCalibrationError(f"{name} must be numeric")
    try:
        result = decimal.Decimal(str(value).strip())
    except (decimal.InvalidOperation, AttributeError) as exc:
        raise ForecastCalibrationError(f"{name} must be numeric") from exc
    if not result.is_finite():
        raise ForecastCalibrationError(f"{name} must be finite")
    return result


def format_decimal(value: Any, places: int = 4) -> str:
    """Stable, human-readable decimal text (no exponent, no trailing zeroes)."""

    number = as_decimal(value)
    quantizer = decimal.Decimal(1).scaleb(-places)
    text = format(number.quantize(quantizer), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _end_of_day(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, dt.time.max, tzinfo=dt.timezone.utc)


def _flatten(row: Mapping[str, Any]) -> dict[str, Any]:
    """Expose dimensions nested under common envelope fields."""

    merged: dict[str, Any] = {}
    for container in ("dimensions", "attributes", "metadata", "tags", "labels"):
        nested = row.get(container)
        if isinstance(nested, Mapping):
            merged.update(nested)
    merged.update(row)
    return merged


def _looks_like_row(value: Mapping[str, Any]) -> bool:
    flat = _flatten(value)
    return any(field in flat for field in _DATE_FIELDS) and any(
        field in flat for field in _VALUE_FIELDS
    )


def _row_from_series_mapping(key: Any, value: Any) -> dict[str, Any] | None:
    """Turn common ``{date: value}`` and ``{date: {...}}`` series into rows."""

    try:
        day = as_date(key)
    except ForecastCalibrationError:
        return None
    if isinstance(value, Mapping):
        item = dict(value)
        if _looks_like_row(item):
            item.setdefault("date", day)
            return item
        if any(field in item for field in _VALUE_FIELDS):
            item.setdefault("date", day)
            return item
        return None
    if isinstance(value, (int, float, decimal.Decimal, str)):
        return {"date": day, "value": value}
    return None


def expand_observations(
    observations: Any,
    site_id: str | None = None,
    service_type: str | None = None,
) -> list[dict[str, Any]]:
    """Expand supported observation container shapes into a flat row list.

    Accepted shapes include a list of mappings, a single mapping, a mapping of
    site -> rows, a nested site -> service_type -> rows mapping, and a simple
    ``{date: value}`` series.  Rows may carry dimensions in
    ``dimensions``/``attributes``/``metadata``.
    """

    rows: list[dict[str, Any]] = []

    def add(item: Any, inherited: Mapping[str, Any] | None = None) -> None:
        if not isinstance(item, Mapping):
            return
        flat = _flatten(item)
        if inherited:
            for key, value in inherited.items():
                flat.setdefault(key, value)
        if flat.get("site_id") is None:
            value = _first(flat, _SITE_FIELDS)
            if value is not None:
                flat["site_id"] = value
        if flat.get("service_type") is None:
            value = _first(flat, _SERVICE_FIELDS)
            if value is not None:
                flat["service_type"] = value
        rows.append(flat)

    if observations is None:
        return []
    if isinstance(observations, Mapping):
        if _looks_like_row(observations):
            add(observations)
        elif any(field in observations for field in _VALUE_FIELDS):
            add(observations)
        else:
            # A flat {date: value} series.
            series_rows = [
                _row_from_series_mapping(key, value)
                for key, value in observations.items()
            ]
            if series_rows and all(item is not None for item in series_rows):
                for item in series_rows:
                    assert item is not None
                    add(item)
            else:
                # A {date: value} series may also carry a few scalar metadata
                # fields such as site_id/service_type; inherit those.
                date_keys = {}
                metadata: dict[str, Any] = {}
                for key, value in observations.items():
                    try:
                        day = as_date(key)
                    except ForecastCalibrationError:
                        metadata[key] = value
                        continue
                    date_keys[day] = value
                if date_keys and all(
                    not isinstance(value, (list, tuple, Mapping))
                    or _looks_like_row(value)
                    or any(field in value for field in _VALUE_FIELDS)
                    for value in date_keys.values()
                ):
                    for day, value in date_keys.items():
                        item = _row_from_series_mapping(day, value)
                        if item is not None:
                            add(item, metadata)
                else:
                    for key, value in observations.items():
                        if isinstance(value, Mapping) and not _looks_like_row(value):
                            # Could be site -> {service_type: rows}, or
                            # site -> {date: value}.
                            date_series = [
                                _row_from_series_mapping(inner_key, inner_value)
                                for inner_key, inner_value in value.items()
                            ]
                            if date_series and all(item is not None for item in date_series):
                                for item in date_series:
                                    assert item is not None
                                    add(item, {"site_id": str(key)})
                            else:
                                for inner_key, inner_value in value.items():
                                    inherited = {
                                        "site_id": str(key),
                                        "service_type": str(inner_key),
                                    }
                                    if isinstance(inner_value, (list, tuple)):
                                        for item in inner_value:
                                            add(item, inherited)
                                    elif isinstance(inner_value, Mapping):
                                        add(inner_value, inherited)
                        elif isinstance(value, (list, tuple)):
                            for item in value:
                                add(item, {"site_id": str(key)})
                        else:
                            item = _row_from_series_mapping(key, value)
                            if item is not None:
                                add(item)
    elif isinstance(observations, (list, tuple)):
        for item in observations:
            add(item)
    else:
        raise ForecastCalibrationError("observations must be a mapping or sequence")

    for index, row in enumerate(rows):
        row["_position"] = index
        row["site_id"] = str(row.get("site_id", site_id or "default"))
        row["service_type"] = str(row.get("service_type", service_type or "default"))
        if row.get("date") is None:
            # Series keys may already have supplied date.
            value = _first(row, _DATE_FIELDS)
            if value is None:
                # Some feeds only carry a capture timestamp; its calendar day
                # is the observation day.
                known_only = _first(row, _KNOWN_FIELDS)
                if known_only is not None:
                    value = _as_datetime(known_only, "recorded_at").date()
            if value is None:
                raise ForecastCalibrationError("observation date is required")
            row["date"] = value
        if row.get("value") is None:
            value = _first(row, _VALUE_FIELDS)
            if value is None:
                raise ForecastCalibrationError("observation value is required")
            row["value"] = value
        row["date"] = as_date(row["date"], "observation date")
        row["value"] = as_decimal(row["value"], "observation value")
        known = _first(row, _KNOWN_FIELDS)
        assumed = known is None
        if known is None:
            known = _end_of_day(row["date"])
        else:
            known = _as_datetime(known, "recorded_at")
        row["known_at"] = known
        row["assumed_known_at"] = assumed
        row["recorded_at"] = known.isoformat()
        if row["value"] < 0:
            raise ForecastCalibrationError("observation value cannot be negative")
    return rows


def normalize_calendar(calendar: Any) -> dict[str, decimal.Decimal]:
    """Return ``{iso-date: factor}`` from the calendar shapes used upstream."""

    if not calendar:
        return {}
    result: dict[str, decimal.Decimal] = {}
    if isinstance(calendar, Mapping):
        for key, value in calendar.items():
            if isinstance(value, Mapping):
                if value.get("closed") in {True, "true", "yes", 1}:
                    result[str(key)] = decimal.Decimal("0")
                    continue
                factor = _first(value, _CALENDAR_FIELDS, "1")
            else:
                factor = value
            result[str(key)] = as_decimal(factor, "calendar factor")
        return result
    if isinstance(calendar, (list, tuple)):
        for row in calendar:
            if not isinstance(row, Mapping):
                raise ForecastCalibrationError("calendar rows must be mappings")
            day = _first(row, _DATE_FIELDS)
            if day is None:
                raise ForecastCalibrationError("calendar row date is required")
            if row.get("closed") in {True, "true", "yes", 1}:
                result[str(as_date(day))] = decimal.Decimal("0")
                continue
            factor = _first(row, _CALENDAR_FIELDS, "1")
            result[str(as_date(day))] = as_decimal(factor, "calendar factor")
        return result
    raise ForecastCalibrationError("calendar must be a mapping or sequence")


# ---------------------------------------------------------------------------
# Point-in-time visibility
# ---------------------------------------------------------------------------


def _canonical_cutoff(value: Any) -> tuple[dt.datetime, dt.date]:
    if value is None:
        raise ForecastCalibrationError("as_of/decision_date is required")
    if isinstance(value, dt.datetime):
        moment = value
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.timezone.utc)
        moment = moment.astimezone(dt.timezone.utc)
        return moment, moment.date()
    day = as_date(value, "as_of")
    return _end_of_day(day), day


def _latest_revisions(
    rows: Iterable[Mapping[str, Any]],
    cutoff: dt.datetime,
    end_date: dt.date | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Select the latest record per date visible at ``cutoff``.

    Returns ``(visible, excluded_late, conflicts)``.  A conflicting revision is
    one where two records for the same date became visible at exactly the same
    instant with different values; the caller can surface that ambiguity
    instead of silently choosing one.
    """

    visible_candidates: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if end_date is not None and item["date"] > end_date:
            excluded.append({**item, "exclusion_reason": "after_decision_date"})
            continue
        if item["known_at"] > cutoff:
            excluded.append({**item, "exclusion_reason": "recorded_after_as_of"})
            continue
        visible_candidates.append(item)

    latest: dict[dt.date, dict[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []
    for item in visible_candidates:
        current = latest.get(item["date"])
        if current is None:
            latest[item["date"]] = item
            continue
        current_key = (current["known_at"], current["_position"])
        item_key = (item["known_at"], item["_position"])
        if item["known_at"] == current["known_at"] and item["value"] != current["value"]:
            conflicts.append(
                {
                    "site_id": item["site_id"],
                    "service_type": item["service_type"],
                    "date": item["date"].isoformat(),
                    "values": sorted(
                        {format_decimal(current["value"]), format_decimal(item["value"])}
                    ),
                }
            )
        if item_key >= current_key:
            latest[item["date"]] = item
    ordered = [latest[day] for day in sorted(latest)]
    return ordered, excluded, conflicts


def _missing_dates(series: Sequence[Mapping[str, Any]]) -> list[str]:
    if not series:
        return []
    days = sorted({row["date"] for row in series})
    if len(days) < 2:
        return []
    present = set(days)
    missing: list[str] = []
    cursor = days[0] + dt.timedelta(days=1)
    while cursor < days[-1]:
        if cursor not in present:
            missing.append(cursor.isoformat())
        cursor += dt.timedelta(days=1)
    return missing


def _group_rows(rows: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (str(row["site_id"]), str(row["service_type"]))
        groups.setdefault(key, []).append(row)
    for values in groups.values():
        values.sort(key=lambda item: (item["date"], item["known_at"], item["_position"]))
    return dict(sorted(groups.items()))


def visible_observations(
    observations: Any,
    as_of: Any,
    site_id: str | None = None,
    service_type: str | None = None,
) -> dict[str, Any]:
    """Return observations knowable at ``as_of``.

    The result contains the visible rows, the rows excluded because they were
    recorded later, per-group missing calendar dates, and a fingerprint of the
    exact visible evidence used by downstream calculations.
    """

    cutoff, day = _canonical_cutoff(as_of)
    raw = expand_observations(observations, site_id=site_id, service_type=service_type)
    if site_id is not None:
        raw = [row for row in raw if row["site_id"] == str(site_id)]
    if service_type is not None:
        raw = [row for row in raw if row["service_type"] == str(service_type)]

    groups = _group_rows(raw)
    visible: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    missing_by_group: dict[str, list[str]] = {}
    backfilled: list[dict[str, Any]] = []
    for (site, service), rows in groups.items():
        selected, late, clashes = _latest_revisions(rows, cutoff, day)
        visible.extend(selected)
        excluded.extend(late)
        conflicts.extend(clashes)
        key = f"{site}|{service}"
        missing_by_group[key] = _missing_dates(selected)
        for item in selected:
            if item["known_at"].date() > item["date"]:
                backfilled.append(
                    {
                        "site_id": site,
                        "service_type": service,
                        "date": item["date"].isoformat(),
                        "recorded_at": item["known_at"].isoformat(),
                    }
                )
    visible.sort(key=lambda item: (item["site_id"], item["service_type"], item["date"]))
    excluded.sort(key=lambda item: (item["site_id"], item["service_type"], item["date"]))
    serialized_excluded = [
        {
            "site_id": row["site_id"],
            "service_type": row["service_type"],
            "date": row["date"].isoformat(),
            "value": format_decimal(row["value"]),
            "recorded_at": row["known_at"].isoformat(),
            "reason": row.get("exclusion_reason"),
        }
        for row in excluded
    ]
    evidence = [
        {
            "site_id": row["site_id"],
            "service_type": row["service_type"],
            "date": row["date"].isoformat(),
            "value": format_decimal(row["value"]),
            "recorded_at": row["known_at"].isoformat(),
        }
        for row in visible
    ]
    return CalibrationResult({
        "as_of": day.isoformat(),
        "knowledge_cutoff": cutoff.isoformat(),
        "observations": visible,
        "excluded": serialized_excluded,
        "excluded_late": serialized_excluded,
        "conflicts": conflicts,
        "missing_dates": sorted(
            {date for values in missing_by_group.values() for date in values}
        ),
        "missing_by_group": missing_by_group,
        "backfilled": backfilled,
        "source_fingerprint": fingerprint(evidence),
    })


# ---------------------------------------------------------------------------
# Metrics and calibration
# ---------------------------------------------------------------------------


def _quantile(values: Sequence[decimal.Decimal], probability: decimal.Decimal) -> decimal.Decimal:
    if not values:
        return decimal.Decimal("0")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (decimal.Decimal(len(ordered) - 1) * probability)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (decimal.Decimal("1") - weight) + ordered[upper] * weight


def error_metrics(
    folds: Sequence[Mapping[str, Any]],
    confidence: Any = "0.8",
) -> dict[str, Any]:
    """Summarise signed forecast errors.

    ``folds`` must contain ``actual`` and ``predicted`` decimal-compatible
    values.  Missing or non-numeric folds are ignored rather than imputed.
    """

    pairs: list[tuple[decimal.Decimal, decimal.Decimal]] = []
    for fold in folds:
        actual = fold.get("actual")
        predicted = fold.get("predicted")
        if actual is None or predicted is None:
            continue
        pairs.append((as_decimal(actual, "actual"), as_decimal(predicted, "predicted")))
    errors = [predicted - actual for actual, predicted in pairs]
    absolute = [abs(value) for value in errors]
    count = len(pairs)
    if not count:
        return {
            "count": 0,
            "sample_size": 0,
            "mae": None,
            "rmse": None,
            "bias": None,
            "mape": None,
            "wape": None,
            "errors": [],
            "signed_errors": [],
            "margin": None,
            "confidence": format_decimal(as_decimal(confidence), 4),
        }
    mae = sum(absolute, decimal.Decimal("0")) / decimal.Decimal(count)
    mse = sum((value * value for value in errors), decimal.Decimal("0")) / decimal.Decimal(
        count
    )
    rmse = decimal.Decimal(math.sqrt(float(mse)))
    bias = sum(errors, decimal.Decimal("0")) / decimal.Decimal(count)
    non_zero: list[decimal.Decimal] = []
    for (actual, _), error in zip(pairs, errors):
        if actual != 0:
            non_zero.append(abs(error) / actual)
    mape = (
        sum(non_zero, decimal.Decimal("0")) / decimal.Decimal(len(non_zero))
        if non_zero
        else None
    )
    actual_total = sum((actual for actual, _ in pairs), decimal.Decimal("0"))
    wape = (
        sum(absolute, decimal.Decimal("0")) / actual_total if actual_total != 0 else None
    )
    margin = _quantile(absolute, as_decimal(confidence, "confidence"))
    return {
        "count": count,
        "sample_size": count,
        "mae": format_decimal(mae),
        "rmse": format_decimal(rmse),
        "bias": format_decimal(bias),
        "mape": None if mape is None else format_decimal(mape),
        "wape": None if wape is None else format_decimal(wape),
        "errors": [format_decimal(value) for value in absolute],
        "signed_errors": [format_decimal(value) for value in errors],
        "margin": format_decimal(margin),
        "confidence": format_decimal(as_decimal(confidence), 4),
    }


@dataclass(frozen=True)
class ForecastCalibrator:
    """Rolling, knowledge-time aware forecast calibration.

    ``minimum_history`` is the number of *actual* observations that must be
    visible at a forecast origin before that origin participates in the
    backtest.  ``min_samples`` is the number of scored folds required before
    the result is considered deterministic enough for staffing advice.
    """

    capacity_model: Any = None
    minimum_history: int = 3
    min_samples: int = 3
    confidence: str = "0.8"
    min_factor: str = "0.5"
    max_factor: str = "1.5"
    train_window: int | None = None
    strict_point_in_time: bool = False
    overload_threshold: str = "0.9"

    def __post_init__(self) -> None:
        if self.minimum_history < 1:
            raise ForecastCalibrationError("minimum_history must be positive")
        if self.min_samples < 1:
            raise ForecastCalibrationError("min_samples must be positive")
        confidence = as_decimal(self.confidence, "confidence")
        if not decimal.Decimal("0") < confidence <= decimal.Decimal("1"):
            raise ForecastCalibrationError("confidence must be between 0 and 1")
        minimum = as_decimal(self.min_factor, "min_factor")
        maximum = as_decimal(self.max_factor, "max_factor")
        if minimum <= 0 or maximum <= 0:
            raise ForecastCalibrationError("calibration factors must be positive")
        if minimum > maximum:
            raise ForecastCalibrationError("min_factor cannot exceed max_factor")
        if self.train_window is not None and self.train_window < 1:
            raise ForecastCalibrationError("train_window must be positive")

    # -- forecast rule ----------------------------------------------------
    def _forecast_target(
        self,
        train: Sequence[Mapping[str, Any]],
        target: dt.date,
        calendar: Mapping[str, decimal.Decimal],
    ) -> decimal.Decimal | None:
        if not train:
            return None
        last = train[-1]["date"]
        span = (target - last).days
        if span < 1:
            return None
        return self._point_in_time_forecast(train, target, calendar)

    def _point_in_time_forecast(
        self,
        train: Sequence[Mapping[str, Any]],
        target: dt.date,
        calendar: Mapping[str, decimal.Decimal],
        window: int = 7,
    ) -> decimal.Decimal | None:
        """Forecast without inventing values for missing calendar days.

        This mirrors :class:`CapacityModel`'s trend/seasonality rule but uses
        only the observations that actually exist.  A gap in the series is a
        gap in the evidence; it is never forward-filled.
        """

        if not train:
            return None
        values = [row["value"] for row in train]
        last_day = train[-1]["date"]
        offset = (target - last_day).days
        if offset < 1:
            return None
        span = min(max(1, int(window)), len(values))
        baseline = values[-1]
        delta = (
            (values[-1] - values[max(0, len(values) - span)])
            / decimal.Decimal(max(1, span - 1))
            if len(values) > 1
            else decimal.Decimal("0")
        )
        season = calendar.get(target.isoformat(), decimal.Decimal("1"))
        return max(decimal.Decimal("0"), (baseline + delta * offset) * season)

    def _train_at(
        self,
        group_rows: Sequence[Mapping[str, Any]],
        origin: dt.date,
        train_window: int | None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        visible, _, _ = _latest_revisions(group_rows, _end_of_day(origin), origin)
        if train_window is not None and visible:
            first = max(origin - dt.timedelta(days=train_window - 1), visible[0]["date"])
            visible = [row for row in visible if row["date"] >= first]
            visible, _, _ = _latest_revisions(visible, _end_of_day(origin), origin)
        expected: list[str] = []
        present = {row["date"] for row in visible}
        if visible:
            cursor = visible[0]["date"] + dt.timedelta(days=1)
            while cursor <= origin:
                if cursor not in present:
                    expected.append(cursor.isoformat())
                cursor += dt.timedelta(days=1)
        return visible, expected

    def rolling_backtest(
        self,
        observations: Any,
        as_of: Any = None,
        horizon: int = 1,
        calendar: Any = None,
        **options: Any,
    ) -> dict[str, Any]:
        """Backtest the forecast with information available at each origin.

        Only observations recorded on or before an origin are used to train the
        forecast for that origin.  Scored actuals are only taken from the
        decision-date snapshot; dates with no observation are skipped (and
        reported) rather than filled.
        """

        if as_of is None:
            as_of = options.pop("decision_date", None)
        if as_of is None:
            as_of = options.pop("cutoff", None)
        if as_of is None:
            raise ForecastCalibrationError("as_of/decision_date is required")
        horizon = int(options.pop("forecast_horizon", horizon))
        if horizon < 1:
            raise ForecastCalibrationError("horizon must be positive")
        minimum_history = int(
            options.pop("min_history", options.pop("min_train", self.minimum_history))
        )
        min_samples = int(options.pop("min_samples", self.min_samples))
        confidence = options.pop("confidence", self.confidence)
        strict = bool(options.pop("strict_point_in_time", self.strict_point_in_time))
        max_origins = options.pop("max_origins", options.pop("origins", None))
        origin_stride = int(options.pop("origin_stride", 1))
        if origin_stride < 1:
            raise ForecastCalibrationError("origin_stride must be positive")
        site_filter = options.pop("site_id", None)
        service_filter = options.pop("service_type", None)
        train_window = options.pop("window", options.pop("train_window", self.train_window))
        if train_window is not None:
            train_window = int(train_window)

        cutoff, decision_day = _canonical_cutoff(as_of)
        raw = expand_observations(observations, site_id=site_filter, service_type=service_filter)
        if site_filter is not None:
            raw = [row for row in raw if row["site_id"] == str(site_filter)]
        if service_filter is not None:
            raw = [row for row in raw if row["service_type"] == str(service_filter)]
        calendar_map = normalize_calendar(calendar)
        groups = _group_rows(raw)

        group_reports: list[dict[str, Any]] = []
        all_folds: list[dict[str, Any]] = []
        all_excluded: list[dict[str, Any]] = []
        all_missing: set[str] = set()
        for (site, service), rows in groups.items():
            snapshot, excluded, conflicts = _latest_revisions(rows, cutoff, decision_day)
            all_excluded.extend(excluded)
            all_missing.update(_missing_dates(snapshot))
            actual_by_day = {row["date"]: row for row in snapshot}
            origins = sorted({row["date"] for row in snapshot if row["date"] <= decision_day})
            if max_origins is not None:
                origins = origins[-int(max_origins) :]
            folds: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            for origin in origins:
                if (origin - origins[0]).days % origin_stride:
                    continue
                train, training_gaps = self._train_at(rows, origin, train_window)
                if len(train) < minimum_history:
                    skipped.append(
                        {
                            "origin": origin.isoformat(),
                            "reason": "insufficient_history",
                            "train_points": len(train),
                        }
                    )
                    continue
                for offset in range(1, horizon + 1):
                    target = origin + dt.timedelta(days=offset)
                    if target > decision_day:
                        continue
                    actual_row = actual_by_day.get(target)
                    if actual_row is None:
                        # A missing actual is not imputed.  The gap itself is
                        # reported; there is simply no scored fold here.
                        continue
                    if strict and actual_row["known_at"] > _end_of_day(target):
                        skipped.append(
                            {
                                "origin": origin.isoformat(),
                                "target": target.isoformat(),
                                "reason": "actual_backfilled_after_target",
                            }
                        )
                        continue
                    predicted = self._forecast_target(train, target, calendar_map)
                    if predicted is None:
                        skipped.append(
                            {
                                "origin": origin.isoformat(),
                                "target": target.isoformat(),
                                "reason": "no_forecast_horizon",
                            }
                        )
                        continue
                    actual = actual_row["value"]
                    error = predicted - actual
                    fold = {
                        "site_id": site,
                        "service_type": service,
                        "origin": origin.isoformat(),
                        "target": target.isoformat(),
                        "predicted": format_decimal(predicted),
                        "actual": format_decimal(actual),
                        "value": format_decimal(predicted),
                        "error": format_decimal(abs(error)),
                        "signed_error": format_decimal(error),
                        "absolute_error": format_decimal(abs(error)),
                        "train_points": len(train),
                        "training_gaps": training_gaps,
                        "known_at": actual_row["known_at"].isoformat(),
                        "actual_known_at": actual_row["known_at"].isoformat(),
                        "actual_assumed_known_at": actual_row["assumed_known_at"],
                        "actual_was_backfilled": actual_row["known_at"].date() > target,
                        "calendar_factor": format_decimal(
                            calendar_map.get(target.isoformat(), decimal.Decimal("1"))
                        ),
                    }
                    folds.append(fold)
            metrics = error_metrics(folds, confidence)
            factor = self._calibration_factor(folds)
            deterministic = metrics["count"] >= min_samples
            reasons: list[str] = []
            if not deterministic:
                reasons.append("insufficient_history")
            if any(fold["actual_was_backfilled"] for fold in folds):
                reasons.append("scored_actuals_include_backfill")
            group_reports.append(
                {
                    **metrics,
                    "site_id": site,
                    "service_type": service,
                    "status": "ok" if deterministic else "insufficient_history",
                    "deterministic": deterministic,
                    "calibration_factor": format_decimal(factor),
                    "factor": format_decimal(factor),
                    "folds": folds,
                    "fold_count": len(folds),
                    "skipped": skipped,
                    "missing_dates": _missing_dates(snapshot),
                    "conflicts": conflicts,
                    "excluded_late": [
                        {
                            "date": row["date"].isoformat(),
                            "recorded_at": row["known_at"].isoformat(),
                            "reason": row.get("exclusion_reason"),
                        }
                        for row in excluded
                    ],
                    "visible_points": len(snapshot),
                    "reasons": reasons,
                }
            )
            all_folds.extend(folds)

        overall = error_metrics(all_folds, confidence)
        deterministic = bool(group_reports) and all(
            group["deterministic"] for group in group_reports
        )
        status = "ok" if deterministic else "insufficient_history"
        if not group_reports:
            status = "insufficient_history"
        factor = self._calibration_factor(all_folds)
        by_site: dict[str, dict[str, Any]] = {}
        by_service: dict[str, dict[str, Any]] = {}
        by_site_service: dict[str, dict[str, Any]] = {}
        for group in group_reports:
            summary = {
                key: group[key]
                for key in (
                    "status",
                    "deterministic",
                    "count",
                    "mae",
                    "rmse",
                    "bias",
                    "mape",
                    "wape",
                    "margin",
                    "calibration_factor",
                )
            }
            by_site.setdefault(group["site_id"], {"folds": []})["folds"].extend(
                group["folds"]
            )
            by_service.setdefault(group["service_type"], {"folds": []})["folds"].extend(
                group["folds"]
            )
            by_site_service[f"{group['site_id']}|{group['service_type']}"] = summary
        for mapping in (by_site, by_service):
            for key, value in list(mapping.items()):
                metrics = error_metrics(value["folds"], confidence)
                mapping[key] = {
                    **metrics,
                    "count": metrics["count"],
                    "deterministic": metrics["count"] >= min_samples,
                }
        reasons: list[str] = []
        if status != "ok":
            reasons.append("insufficient_history")
        if all_excluded:
            reasons.append("excluded_late_records")
        if all_missing:
            reasons.append("missing_observation_dates")
        if any(group["conflicts"] for group in group_reports):
            reasons.append("conflicting_observations")
        if any(fold["actual_was_backfilled"] for fold in all_folds):
            reasons.append("scored_actuals_include_backfill")
        return CalibrationResult({
            "method": "rolling_backtest",
            "as_of": decision_day.isoformat(),
            "knowledge_cutoff": cutoff.isoformat(),
            "horizon": horizon,
            "status": status,
            "deterministic": deterministic,
            **{key: overall[key] for key in ("count", "sample_size", "mae", "rmse", "bias", "mape", "wape", "margin", "confidence")},
            "calibration_factor": format_decimal(factor),
            "factor": format_decimal(factor),
            "folds": all_folds,
            "fold_count": len(all_folds),
            "skipped": [
                {"site_id": group["site_id"], "service_type": group["service_type"], **item}
                for group in group_reports
                for item in group.get("skipped", ())
            ],
            "groups": group_reports,
            "by_site": by_site,
            "by_service": by_service,
            "by_site_service": by_site_service,
            "conflicts": [
                conflict
                for group in group_reports
                for conflict in group.get("conflicts", ())
            ],
            "missing_dates": sorted(all_missing),
            "missing_by_group": {
                group["site_id"] + "|" + group["service_type"]: group["missing_dates"]
                for group in group_reports
            },
            "excluded_late": [
                {
                    "site_id": row["site_id"],
                    "service_type": row["service_type"],
                    "date": row["date"].isoformat(),
                    "recorded_at": row["known_at"].isoformat(),
                    "reason": row.get("exclusion_reason"),
                }
                for row in all_excluded
            ],
            "source_fingerprint": visible_observations(
                observations, as_of, site_id=site_filter, service_type=service_filter
            )["source_fingerprint"],
            "reasons": reasons,
        })

    def _calibration_factor(self, folds: Sequence[Mapping[str, Any]]) -> decimal.Decimal:
        predicted_total = decimal.Decimal("0")
        actual_total = decimal.Decimal("0")
        for fold in folds:
            try:
                predicted = as_decimal(fold.get("predicted"), "predicted")
                actual = as_decimal(fold.get("actual"), "actual")
            except ForecastCalibrationError:
                continue
            predicted_total += predicted
            actual_total += actual
        if predicted_total <= 0:
            return decimal.Decimal("1")
        factor = actual_total / predicted_total
        return max(
            as_decimal(self.min_factor), min(as_decimal(self.max_factor), factor)
        )

    # -- calibration forecast --------------------------------------------
    def calibrate(
        self,
        observations: Any,
        as_of: Any = None,
        horizon: int = 1,
        calendar: Any = None,
        **options: Any,
    ) -> dict[str, Any]:
        """Backtest, calibrate and produce bounded future demand.

        The returned forecast is deterministic only when the rolling backtest
        has enough scored folds.  Even when it does not, raw values are
        included for diagnosis but ``deterministic`` is false and staffing
        code must not turn them into a definitive headcount.
        """

        if as_of is None:
            as_of = options.pop("decision_date", None)
        if as_of is None:
            raise ForecastCalibrationError("as_of/decision_date is required")
        horizon = int(options.pop("forecast_horizon", horizon))
        if horizon < 1:
            raise ForecastCalibrationError("horizon must be positive")
        site_filter = options.pop("site_id", None)
        service_filter = options.pop("service_type", None)
        report = self.rolling_backtest(
            observations,
            as_of,
            horizon=horizon,
            calendar=calendar,
            site_id=site_filter,
            service_type=service_filter,
            **options,
        )
        cutoff, decision_day = _canonical_cutoff(as_of)
        raw = expand_observations(observations, site_id=site_filter, service_type=service_filter)
        if site_filter is not None:
            raw = [row for row in raw if row["site_id"] == str(site_filter)]
        if service_filter is not None:
            raw = [row for row in raw if row["service_type"] == str(service_filter)]
        calendar_map = normalize_calendar(calendar)
        groups = _group_rows(raw)
        confidence = as_decimal(options.get("confidence", self.confidence), "confidence")
        report_groups = {
            (group["site_id"], group["service_type"]): group for group in report["groups"]
        }
        forecast_groups: list[dict[str, Any]] = []
        all_forecast: list[dict[str, Any]] = []
        for (site, service), rows in groups.items():
            group_report = report_groups.get((site, service))
            snapshot, _, _ = _latest_revisions(rows, cutoff, decision_day)
            factor = (
                as_decimal(group_report["calibration_factor"])
                if group_report is not None
                else decimal.Decimal("1")
            )
            margin = (
                as_decimal(group_report["margin"])
                if group_report is not None and group_report["margin"] is not None
                else decimal.Decimal("0")
            )
            deterministic = bool(group_report and group_report["deterministic"])
            forecasts: list[dict[str, Any]] = []
            if snapshot:
                last_day = snapshot[-1]["date"]
                for offset in range(1, horizon + 1):
                    target = decision_day + dt.timedelta(days=offset)
                    span = (target - last_day).days
                    if span < 1:
                        continue
                    raw_value = self._point_in_time_forecast(snapshot, target, calendar_map)
                    if raw_value is None:
                        continue
                    calibrated = raw_value * factor
                    scale = decimal.Decimal(str(math.sqrt(offset)))
                    scaled_margin = margin * scale
                    lower = max(decimal.Decimal("0"), calibrated - scaled_margin)
                    upper = calibrated + scaled_margin
                    item = {
                        "site_id": site,
                        "service_type": service,
                        "date": target.isoformat(),
                        "demand": format_decimal(calibrated),
                        "calibrated_demand": format_decimal(calibrated),
                        "raw_demand": format_decimal(raw_value),
                        "forecast": format_decimal(calibrated),
                        "lower": format_decimal(lower),
                        "upper": format_decimal(upper),
                        "lower_demand": format_decimal(lower),
                        "upper_demand": format_decimal(upper),
                        "margin": format_decimal(scaled_margin),
                        "calibration_factor": format_decimal(factor),
                        "calendar_factor": format_decimal(
                            calendar_map.get(target.isoformat(), decimal.Decimal("1"))
                        ),
                        "deterministic": deterministic,
                    }
                    forecasts.append(item)
                    all_forecast.append(item)
            forecast_groups.append(
                {
                    "site_id": site,
                    "service_type": service,
                    "status": "ok" if deterministic else "insufficient_history",
                    "deterministic": deterministic,
                    "calibration_factor": format_decimal(factor),
                    "factor": format_decimal(factor),
                    "margin": format_decimal(margin),
                    "confidence": format_decimal(confidence, 4),
                    "forecast": forecasts,
                    "demand": forecasts,
                    "reasons": list(group_report["reasons"]) if group_report else ["insufficient_history"],
                }
            )
        status = "ok" if report["deterministic"] and forecast_groups else "insufficient_history"
        return CalibrationResult({
            **report,
            "status": status,
            "deterministic": bool(report["deterministic"] and forecast_groups),
            "forecast": all_forecast,
            "calibrated_forecast": all_forecast,
            "forecast_groups": forecast_groups,
            "groups": report["groups"],
            "forecast_by_group": forecast_groups,
        })


# Friendly aliases used by callers and older integrations.
ForecastCalibration = ForecastCalibrator
CalibrationEngine = ForecastCalibrator
RollingBacktester = ForecastCalibrator
Backtester = ForecastCalibrator


def rolling_backtest(
    observations: Any,
    as_of: Any = None,
    horizon: int = 1,
    calendar: Any = None,
    **options: Any,
) -> dict[str, Any]:
    model = options.pop("capacity_model", None)
    config = {
        key: options.pop(key)
        for key in (
            "minimum_history",
            "min_samples",
            "confidence",
            "min_factor",
            "max_factor",
            "train_window",
            "strict_point_in_time",
        )
        if key in options
    }
    return ForecastCalibrator(capacity_model=model, **config).rolling_backtest(
        observations, as_of, horizon=horizon, calendar=calendar, **options
    )


def calibrate_forecast(
    observations: Any,
    as_of: Any = None,
    horizon: int = 1,
    calendar: Any = None,
    **options: Any,
) -> dict[str, Any]:
    model = options.pop("capacity_model", None)
    config = {
        key: options.pop(key)
        for key in (
            "minimum_history",
            "min_samples",
            "confidence",
            "min_factor",
            "max_factor",
            "train_window",
            "strict_point_in_time",
        )
        if key in options
    }
    return ForecastCalibrator(capacity_model=model, **config).calibrate(
        observations, as_of, horizon=horizon, calendar=calendar, **options
    )
