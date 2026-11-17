"""Capacity decisions layered on top of calibrated demand forecasts.

The planner answers two operational questions that used to be conflated:

* is the site/service short of people even at the *low* end of the forecast
  (an actual capacity problem), or
* is the low end covered while the *high* end is not (a forecast-uncertainty
  problem)?

It deliberately refuses to emit a deterministic headcount when the rolling
backtest did not produce enough scored history.  The calendar factor, service
time and occupancy target rules are the same ones used by
:class:`service_capacity.demand_planning.capacity_model.CapacityModel`.
"""

from __future__ import annotations

import datetime as dt
import decimal
from dataclasses import dataclass
from decimal import ROUND_CEILING
from typing import Any, Mapping, Sequence

from .forecast_calibration import (
    ForecastCalibrationError,
    as_date,
    as_decimal,
    format_decimal,
)

__all__ = [
    "CapacityPlanningError",
    "StaffingConstraints",
    "CapacityPlanner",
    "resolve_available_agents",
    "explain_plan",
    "compare_plans",
]


class CapacityPlanningError(ValueError):
    pass


def _ceil(value: decimal.Decimal) -> int:
    if value <= 0:
        return 0
    return int(value.to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class StaffingConstraints:
    """Existing staffing rules: service minutes, shift length, occupancy."""

    service_minutes: Any = 30
    shift_minutes: Any = 480
    occupancy_target: Any = "0.8"
    overload_threshold: Any = "0.9"
    maximum_agents: int | None = None

    def __post_init__(self) -> None:
        service = as_decimal(self.service_minutes, "service_minutes")
        shift = as_decimal(self.shift_minutes, "shift_minutes")
        occupancy = as_decimal(self.occupancy_target, "occupancy_target")
        overload = as_decimal(self.overload_threshold, "overload_threshold")
        if service <= 0:
            raise CapacityPlanningError("service_minutes must be positive")
        if shift <= 0:
            raise CapacityPlanningError("shift_minutes must be positive")
        if not decimal.Decimal("0") < occupancy <= decimal.Decimal("1"):
            raise CapacityPlanningError("occupancy_target must be within (0, 1]")
        if not decimal.Decimal("0") < overload <= decimal.Decimal("1"):
            raise CapacityPlanningError("overload_threshold must be within (0, 1]")
        if self.maximum_agents is not None and int(self.maximum_agents) < 0:
            raise CapacityPlanningError("maximum_agents cannot be negative")
        object.__setattr__(self, "service_minutes", service)
        object.__setattr__(self, "shift_minutes", shift)
        object.__setattr__(self, "occupancy_target", occupancy)
        object.__setattr__(self, "overload_threshold", overload)

    def as_dict(self) -> dict[str, Any]:
        return {
            "service_minutes": format_decimal(self.service_minutes),
            "shift_minutes": format_decimal(self.shift_minutes),
            "occupancy_target": format_decimal(self.occupancy_target),
            "overload_threshold": format_decimal(self.overload_threshold),
            "maximum_agents": self.maximum_agents,
        }

    def agents_for(self, demand: Any) -> int:
        """Agents required by the existing occupancy rule."""

        workload = as_decimal(demand, "demand") * self.service_minutes
        agents = _ceil(workload / (self.shift_minutes * self.occupancy_target))
        if self.maximum_agents is not None:
            agents = min(agents, int(self.maximum_agents))
        return agents

    def utilization(self, demand: Any, agents: Any) -> decimal.Decimal | None:
        headcount = as_decimal(agents, "agents")
        if headcount <= 0:
            return None
        return as_decimal(demand, "demand") * self.service_minutes / (
            headcount * self.shift_minutes
        )


_AVAILABLE_FIELDS = (
    "agents",
    "available_agents",
    "headcount",
    "staff",
    "capacity_agents",
    "available",
    "capacity",
)


def _numeric_agents(value: Any) -> decimal.Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Mapping):
        for name in _AVAILABLE_FIELDS:
            if name in value:
                found = _numeric_agents(value[name])
                if found is not None:
                    return found
        return None
    try:
        return as_decimal(value, "available agents")
    except ForecastCalibrationError:
        return None


def resolve_available_agents(
    capacity: Any,
    site_id: str,
    service_type: str,
    day: dt.date | str,
) -> decimal.Decimal | None:
    """Resolve scheduled agents from the roster/capacity shapes we accept."""

    if capacity is None:
        return None
    if isinstance(day, str):
        day = as_date(day)
    iso = day.isoformat()
    direct = _numeric_agents(capacity)
    if direct is not None:
        return direct
    if isinstance(capacity, Mapping):
        combined = [f"{site_id}|{service_type}", f"{site_id}:{service_type}"]
        for key in combined:
            if key in capacity:
                result = resolve_available_agents(capacity[key], site_id, service_type, day)
                if result is not None:
                    return result
        for key in (service_type, f"service:{service_type}"):
            if key in capacity:
                value = _numeric_agents(capacity[key])
                if value is not None:
                    return value
                nested = resolve_available_agents(
                    capacity[key], site_id, service_type, day
                )
                if nested is not None:
                    return nested
        for key in (site_id, f"site:{site_id}"):
            if key in capacity and not isinstance(capacity[key], (int, float, decimal.Decimal)):
                result = resolve_available_agents(
                    capacity[key], site_id, service_type, day
                )
                if result is not None:
                    return result
            elif key in capacity:
                value = _numeric_agents(capacity[key])
                if value is not None:
                    return value
        for key in (iso, day, f"date:{iso}"):
            if key in capacity:
                value = _numeric_agents(capacity[key])
                if value is not None:
                    return value
                nested = resolve_available_agents(
                    capacity[key], site_id, service_type, day
                )
                if nested is not None:
                    return nested
        for key in ("default", "*", "all"):
            if key in capacity:
                value = _numeric_agents(capacity[key])
                if value is not None:
                    return value
                nested = resolve_available_agents(
                    capacity[key], site_id, service_type, day
                )
                if nested is not None:
                    return nested
        # A list of roster rows.
        for candidate in capacity.values():
            if isinstance(candidate, list):
                result = _resolve_row_list(candidate, site_id, service_type, day)
                if result is not None:
                    return result
        return None
    if isinstance(capacity, (list, tuple)):
        return _resolve_row_list(capacity, site_id, service_type, day)
    return _numeric_agents(capacity)


def _resolve_row_list(
    rows: Sequence[Any], site_id: str, service_type: str, day: dt.date
) -> decimal.Decimal | None:
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        row_day = row.get("date", row.get("day"))
        if row_day is not None:
            try:
                if as_date(row_day) != day:
                    continue
            except ForecastCalibrationError:
                continue
        row_site = row.get("site_id", row.get("site", row.get("owner_id")))
        if row_site is not None and str(row_site) not in {site_id, "*", "all"}:
            continue
        row_service = row.get(
            "service_type", row.get("service", row.get("category"))
        )
        if row_service is not None and str(row_service) not in {service_type, "*", "all"}:
            continue
        value = _numeric_agents(row)
        if value is not None:
            return value
    return None


def _extract_forecast_groups(calibration: Any) -> list[dict[str, Any]]:
    if calibration is None:
        raise CapacityPlanningError("calibration result is required")
    if isinstance(calibration, Mapping):
        for key in ("forecast_groups", "forecast_by_group"):
            if key in calibration and isinstance(calibration[key], (list, tuple)):
                # An empty list is meaningful: everything visible was
                # excluded, so there is nothing deterministic to plan.
                return [dict(group) for group in calibration[key]]
        groups = calibration.get("groups")
        if groups and any(
            isinstance(group, Mapping)
            and (group.get("forecast") or group.get("demand"))
            for group in groups
        ):
            return [dict(group) for group in groups]
        forecast = calibration.get("forecast") or calibration.get("calibrated_forecast")
        if forecast:
            return [
                {
                    "site_id": "default",
                    "service_type": "default",
                    "deterministic": calibration.get("deterministic", True),
                    "calibration_factor": calibration.get("calibration_factor", "1"),
                    "margin": calibration.get("margin", "0"),
                    "forecast": list(forecast),
                    "reasons": list(calibration.get("reasons", ())),
                }
            ]
        return []
    raise CapacityPlanningError("calibration result has no forecast groups")


def _extract_metrics(calibration: Any) -> dict[str, dict[str, Any]]:
    metrics: dict[str, dict[str, Any]] = {}
    if not isinstance(calibration, Mapping):
        return metrics
    for group in calibration.get("groups", ()):
        if not isinstance(group, Mapping):
            continue
        key = f"{group.get('site_id', 'default')}|{group.get('service_type', 'default')}"
        metrics[key] = {
            name: group.get(name)
            for name in (
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
                "missing_dates",
                "reasons",
            )
        }
    return metrics


def _calendar_factor(row: Mapping[str, Any]) -> decimal.Decimal:
    try:
        return as_decimal(row.get("calendar_factor", "1"), "calendar_factor")
    except ForecastCalibrationError:
        return decimal.Decimal("1")


def _row_key(site: str, service: str, day: str) -> str:
    return f"{site}|{service}|{day}"


class CapacityPlanner:
    """Turn calibrated demand into explainable staffing decisions."""

    def __init__(self, constraints: StaffingConstraints | None = None, **options: Any):
        if constraints is None:
            constraints = StaffingConstraints(
                service_minutes=options.pop("service_minutes", 30),
                shift_minutes=options.pop("shift_minutes", 480),
                occupancy_target=options.pop("occupancy_target", "0.8"),
                overload_threshold=options.pop("overload_threshold", "0.9"),
                maximum_agents=options.pop("maximum_agents", None),
            )
        self.constraints = constraints
        self.options = options

    # -- internal row construction --------------------------------------
    def _decision(
        self,
        forecast: Mapping[str, Any],
        available: decimal.Decimal | None,
        deterministic: bool,
        closed: bool,
    ) -> dict[str, Any]:
        constraints = self.constraints
        demand = as_decimal(forecast.get("demand", forecast.get("value", 0)), "demand")
        lower = as_decimal(forecast.get("lower", forecast.get("lower_demand", demand)), "lower")
        upper = as_decimal(forecast.get("upper", forecast.get("upper_demand", demand)), "upper")
        if closed:
            return {
                "status": "closed",
                "risk": "closed",
                "deterministic": deterministic,
                "recommended_agents": 0,
                "required_agents": 0,
                "required_agents_lower": 0,
                "required_agents_point": 0,
                "required_agents_upper": 0,
                "available_agents": None if available is None else _ceil(available),
                "shortfall": "0",
                "shortfall_lower": "0",
                "shortfall_upper": "0",
                "utilization": None,
                "overloaded": False,
                "reason": "calendar_closed",
                "reasons": ["calendar_closed"],
            }
        required_lower = constraints.agents_for(lower)
        required_point = constraints.agents_for(demand)
        required_upper = constraints.agents_for(upper)
        if not deterministic:
            return {
                "status": "insufficient_history",
                "risk": "insufficient_history",
                "deterministic": False,
                "recommended_agents": None,
                "indicative_agents": required_point,
                "required_agents": None,
                "required_agents_lower": None,
                "required_agents_point": None,
                "required_agents_upper": None,
                "indicative_agents_lower": required_lower,
                "indicative_agents_upper": required_upper,
                "shortfall": None,
                "shortfall_lower": None,
                "shortfall_upper": None,
                "utilization": None,
                "overloaded": False,
                "reason": "insufficient_history",
                "reasons": ["insufficient_history"],
            }
        if available is None:
            return {
                "status": "capacity_unknown",
                "risk": "capacity_unknown",
                "deterministic": True,
                "recommended_agents": required_point,
                "required_agents": required_point,
                "required_agents_lower": required_lower,
                "required_agents_point": required_point,
                "required_agents_upper": required_upper,
                "available_agents": None,
                "shortfall": None,
                "shortfall_lower": None,
                "shortfall_upper": None,
                "utilization": None,
                "overloaded": False,
                "reason": "capacity_not_provided",
                "reasons": ["capacity_not_provided"],
            }
        shortfall_lower = decimal.Decimal(max(0, required_lower - _ceil(available)))
        shortfall_point = decimal.Decimal(max(0, required_point - _ceil(available)))
        shortfall_upper = decimal.Decimal(max(0, required_upper - _ceil(available)))
        utilization = constraints.utilization(demand, available)
        overloaded = bool(
            utilization is not None
            and utilization > constraints.overload_threshold
        )
        if shortfall_lower > 0:
            status = "capacity_shortage"
            reason = "capacity_shortage"
        elif shortfall_upper > 0:
            status = "forecast_uncertainty"
            reason = "forecast_uncertainty"
        elif overloaded:
            status = "capacity_pressure"
            reason = "utilization_above_threshold"
        else:
            status = "sufficient"
            reason = "capacity_covers_forecast_range"
        reasons = [reason]
        if shortfall_point > 0 and status == "forecast_uncertainty":
            reasons.append("point_forecast_also_exceeds_capacity")
        return {
            "status": status,
            "risk": status,
            "deterministic": True,
            "recommended_agents": required_point,
            "required_agents": required_point,
            "required_agents_lower": required_lower,
            "required_agents_point": required_point,
            "required_agents_upper": required_upper,
            "available_agents": _ceil(available),
            "gap": required_point - _ceil(available),
            "shortfall": format_decimal(shortfall_point),
            "shortfall_lower": format_decimal(shortfall_lower),
            "shortfall_upper": format_decimal(shortfall_upper),
            "shortfall_point": format_decimal(shortfall_point),
            "utilization": None if utilization is None else format_decimal(utilization),
            "overloaded": overloaded,
            "reason": reason,
            "reasons": reasons,
            "capacity_shortfall": format_decimal(shortfall_lower),
            "uncertainty_shortfall": format_decimal(
                max(decimal.Decimal("0"), shortfall_upper - shortfall_lower)
            ),
        }

    def plan(
        self,
        calibration: Any,
        capacity: Any = None,
        calendar: Any = None,
        **options: Any,
    ) -> dict[str, Any]:
        groups = _extract_forecast_groups(calibration)
        constraints = self.constraints
        rows: list[dict[str, Any]] = []
        for group in groups:
            site = str(group.get("site_id", "default"))
            service = str(group.get("service_type", "default"))
            deterministic = bool(group.get("deterministic", True))
            group_reasons = list(group.get("reasons", ()))
            for forecast in group.get("forecast", ()):
                if not isinstance(forecast, Mapping):
                    continue
                day = as_date(forecast.get("date"), "forecast date")
                factor = _calendar_factor(forecast)
                available = resolve_available_agents(capacity, site, service, day)
                decision = self._decision(
                    forecast,
                    available,
                    deterministic,
                    closed=factor == 0,
                )
                if not deterministic:
                    decision["reasons"] = list(
                        dict.fromkeys(group_reasons + decision["reasons"])
                    )
                row = {
                    "site_id": site,
                    "service_type": service,
                    "date": day.isoformat(),
                    "demand": format_decimal(forecast.get("demand", 0)),
                    "demand_lower": format_decimal(
                        forecast.get("lower", forecast.get("lower_demand", forecast.get("demand", 0)))
                    ),
                    "demand_upper": format_decimal(
                        forecast.get("upper", forecast.get("upper_demand", forecast.get("demand", 0)))
                    ),
                    "workload_minutes": format_decimal(
                        as_decimal(forecast.get("demand", 0)) * constraints.service_minutes
                    ),
                    "workload_minutes_lower": format_decimal(
                        as_decimal(forecast.get("lower", forecast.get("lower_demand", forecast.get("demand", 0))))
                        * constraints.service_minutes
                    ),
                    "workload_minutes_upper": format_decimal(
                        as_decimal(forecast.get("upper", forecast.get("upper_demand", forecast.get("demand", 0))))
                        * constraints.service_minutes
                    ),
                    "calendar_factor": format_decimal(factor),
                    "constraints": constraints.as_dict(),
                    "calibration_factor": group.get("calibration_factor", "1"),
                    "margin": group.get("margin", "0"),
                    **decision,
                }
                rows.append(row)
        rows.sort(key=lambda row: (row["site_id"], row["service_type"], row["date"]))
        result = {
            "method": "capacity_plan",
            "status": self._overall_status(rows),
            "deterministic": all(row["deterministic"] for row in rows) if rows else False,
            "constraints": constraints.as_dict(),
            "rows": rows,
            "days": rows,
            "recommendations": rows,
            "summary": self._summary(rows),
            "metrics_by_group": _extract_metrics(calibration),
            "revisions": [],
        }
        result["explanation"] = explain_plan(result)
        return result

    @staticmethod
    def _overall_status(rows: Sequence[Mapping[str, Any]]) -> str:
        if not rows:
            return "insufficient_history"
        statuses = {row["status"] for row in rows}
        # Insufficient history dominates: with an incomplete evidence base we
        # will not present the plan as a confident capacity verdict.
        if "insufficient_history" in statuses:
            return "insufficient_history"
        if "capacity_shortage" in statuses:
            return "capacity_shortage"
        if "forecast_uncertainty" in statuses:
            return "forecast_uncertainty"
        if "capacity_pressure" in statuses:
            return "capacity_pressure"
        if "capacity_unknown" in statuses:
            return "capacity_unknown"
        return "sufficient"

    @staticmethod
    def _summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        summary = _aggregate(rows)
        return {
            "days": summary["days"],
            "recommended_agents_total": summary["recommended_agents_total"],
            "available_agents_total": summary["available_agents_total"],
            "shortfall_total": summary["shortfall_total"],
            "overloaded_days": summary["overloaded_days"],
            "status_counts": summary["status_counts"],
            "overload_risk": summary["overload_risk"],
            "max_utilization": summary["max_utilization"],
            "mean_utilization": summary["mean_utilization"],
        }

    # -- revisions and comparison ---------------------------------------
    def revise(
        self,
        plan: Mapping[str, Any],
        adjustments: Any = None,
        **options: Any,
    ) -> dict[str, Any]:
        """Apply roster/shift adjustments to an existing plan.

        ``adjustments`` accepts a number (delta for every row), ``{date:
        delta_or_agents}``, ``{"site|service|date": ...}``,
        ``{"site|service": ...}``, ``{"site": {"service": ...}}``, or a list of
        ``{"site_id", "service_type", "date", "agents"|"delta"}``.  A plain
        number is a delta; ``{"agents": n}`` is an absolute override.
        """

        rows: list[dict[str, Any]] = []
        for row in plan.get("rows", ()):
            item = dict(row)
            base = item.get("available_agents")
            base_value = as_decimal(base, "available_agents") if base is not None else None
            delta, override = self._adjustment_for(adjustments, item)
            if override is not None:
                available = override
            elif base_value is not None:
                available = max(decimal.Decimal("0"), base_value + (delta or decimal.Decimal("0")))
            elif delta is not None:
                available = max(decimal.Decimal("0"), delta)
            else:
                available = None
            forecast = {
                "date": item["date"],
                "demand": item["demand"],
                "lower": item["demand_lower"],
                "upper": item["demand_upper"],
                "calendar_factor": item.get("calendar_factor", "1"),
            }
            decision = self._decision(
                forecast,
                available,
                bool(item.get("deterministic", True)),
                item.get("status") == "closed",
            )
            item.update(decision)
            item["available_agents_before_adjustment"] = base
            if available is not None:
                item["available_agents"] = _ceil(available)
            item["adjustment"] = format_decimal(delta) if delta is not None else None
            rows.append(item)
        revised = dict(plan)
        revised["rows"] = rows
        revised["days"] = rows
        revised["recommendations"] = rows
        revised["summary"] = self._summary(rows)
        revised["status"] = self._overall_status(rows)
        revised["deterministic"] = all(row["deterministic"] for row in rows) if rows else False
        revisions = list(plan.get("revisions", ()))
        revisions.append(
            {
                "adjustments": adjustments if isinstance(adjustments, Mapping) else str(adjustments),
                "rows_adjusted": sum(1 for row in rows if row.get("adjustment") not in (None, "0")),
            }
        )
        revised["revisions"] = revisions
        revised["explanation"] = explain_plan(revised)
        return revised

    @staticmethod
    def _adjustment_for(
        adjustments: Any, row: Mapping[str, Any]
    ) -> tuple[decimal.Decimal | None, decimal.Decimal | None]:
        if adjustments is None:
            return None, None
        site = row["site_id"]
        service = row["service_type"]
        day = row["date"]
        key = _row_key(site, service, day)
        if isinstance(adjustments, Mapping):
            for candidate in (key, f"{site}|{service}", f"{site}:{service}", day):
                if candidate in adjustments:
                    return _parse_adjustment(adjustments[candidate])
            if site in adjustments and isinstance(adjustments[site], Mapping):
                nested = adjustments[site]
                for candidate in (f"{service}|{day}", service, day):
                    if candidate in nested:
                        return _parse_adjustment(nested[candidate])
            if "default" in adjustments:
                return _parse_adjustment(adjustments["default"])
            return None, None
        if isinstance(adjustments, (list, tuple)):
            for entry in adjustments:
                if not isinstance(entry, Mapping):
                    continue
                entry_site = entry.get("site_id", entry.get("site"))
                entry_service = entry.get("service_type", entry.get("service"))
                entry_day = entry.get("date", entry.get("day"))
                if entry_site not in (None, site, "*") and str(entry_site) != site:
                    continue
                if entry_service not in (None, service, "*") and str(entry_service) != service:
                    continue
                if entry_day not in (None, day, "*") and str(entry_day) != day:
                    continue
                return _parse_adjustment(entry)
            return None, None
        return _parse_adjustment(adjustments)

    def compare(
        self,
        base: Mapping[str, Any],
        revised: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Compare an adjusted plan against the original, row by row."""

        left = {
            _row_key(row["site_id"], row["service_type"], row["date"]): row
            for row in base.get("rows", ())
        }
        right = {
            _row_key(row["site_id"], row["service_type"], row["date"]): row
            for row in revised.get("rows", ())
        }
        changes = []
        comparable = True
        for key in sorted(set(left) | set(right)):
            old = left.get(key)
            new = right.get(key)
            if old is None or new is None:
                comparable = False
                changes.append(
                    {
                        "key": key,
                        "site_id": (new or old)["site_id"],
                        "service_type": (new or old)["service_type"],
                        "date": (new or old)["date"],
                        "change": "added" if old is None else "removed",
                    }
                )
                continue
            if not old.get("deterministic", True) or not new.get("deterministic", True):
                comparable = False
            fields = {}
            for field_name in (
                "available_agents",
                "recommended_agents",
                "required_agents_upper",
                "shortfall",
                "utilization",
                "status",
                "overloaded",
            ):
                before = old.get(field_name)
                after = new.get(field_name)
                if before != after:
                    fields[field_name] = {"from": before, "to": after}
            if not fields:
                continue
            before_status = old.get("status")
            after_status = new.get("status")
            change = "changed"
            if before_status in {"capacity_shortage", "forecast_uncertainty", "capacity_pressure"} and after_status in {"sufficient", "closed"}:
                change = "improved"
            elif before_status in {"sufficient", "closed"} and after_status in {"capacity_shortage", "forecast_uncertainty", "capacity_pressure"}:
                change = "worsened"
            changes.append(
                {
                    "key": key,
                    "site_id": old["site_id"],
                    "service_type": old["service_type"],
                    "date": old["date"],
                    "change": change,
                    "fields": fields,
                    "from_status": before_status,
                    "to_status": after_status,
                }
            )
        left_summary = base.get("summary", {})
        right_summary = revised.get("summary", {})
        return {
            "comparable": comparable,
            "changes": changes,
            "change_count": len(changes),
            "improved": sum(change.get("change") == "improved" for change in changes),
            "worsened": sum(change.get("change") == "worsened" for change in changes),
            "base": {
                "status": base.get("status"),
                "summary": left_summary,
                "constraints": base.get("constraints"),
            },
            "revised": {
                "status": revised.get("status"),
                "summary": right_summary,
                "constraints": revised.get("constraints"),
            },
            "constraints_unchanged": base.get("constraints") == revised.get("constraints"),
            "notes": (
                []
                if comparable
                else ["some rows are not comparable because history is insufficient"]
            ),
        }


def _parse_adjustment(value: Any) -> tuple[decimal.Decimal | None, decimal.Decimal | None]:
    if isinstance(value, Mapping):
        if "agents" in value:
            return None, as_decimal(value["agents"], "agents")
        if "available_agents" in value:
            return None, as_decimal(value["available_agents"], "available_agents")
        if "override" in value:
            return None, as_decimal(value["override"], "override")
        if "delta" in value:
            return as_decimal(value["delta"], "delta"), None
        return None, None
    if value is None:
        return None, None
    try:
        return as_decimal(value, "adjustment"), None
    except ForecastCalibrationError:
        return None, None


def _aggregate(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    shortfall = sum(
        as_decimal(row.get("shortfall", 0))
        for row in rows
        if row.get("shortfall") is not None
    )
    utilizations = [
        as_decimal(row["utilization"]) for row in rows if row.get("utilization") is not None
    ]
    overloaded_days = sum(bool(row.get("overloaded")) for row in rows)
    if not rows:
        risk = "unknown"
    elif counts.get("capacity_shortage"):
        risk = "high"
    elif counts.get("forecast_uncertainty") or overloaded_days:
        risk = "elevated"
    elif counts.get("capacity_pressure"):
        risk = "moderate"
    elif counts.get("insufficient_history") or counts.get("capacity_unknown"):
        risk = "unknown"
    else:
        risk = "low"
    return {
        "days": len(rows),
        "status_counts": dict(sorted(counts.items())),
        "capacity_shortage_days": counts.get("capacity_shortage", 0),
        "forecast_uncertainty_days": counts.get("forecast_uncertainty", 0),
        "capacity_pressure_days": counts.get("capacity_pressure", 0),
        "sufficient_days": counts.get("sufficient", 0),
        "closed_days": counts.get("closed", 0),
        "insufficient_history_days": counts.get("insufficient_history", 0),
        "capacity_unknown_days": counts.get("capacity_unknown", 0),
        "overloaded_days": overloaded_days,
        "overload_risk": risk,
        "max_utilization": format_decimal(max(utilizations)) if utilizations else None,
        "mean_utilization": (
            format_decimal(sum(utilizations, decimal.Decimal("0")) / decimal.Decimal(len(utilizations)))
            if utilizations
            else None
        ),
        "shortfall_total": format_decimal(shortfall),
        "max_shortfall": format_decimal(
            max(
                (as_decimal(row.get("shortfall", 0)) for row in rows if row.get("shortfall") is not None),
                default=decimal.Decimal("0"),
            )
        ),
        "recommended_agents_total": sum(
            int(row["recommended_agents"])
            for row in rows
            if isinstance(row.get("recommended_agents"), int)
        ),
        "available_agents_total": sum(
            int(row["available_agents"])
            for row in rows
            if isinstance(row.get("available_agents"), int)
        ),
    }


def explain_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Explain plan outcomes by site and by service type."""

    rows = list(plan.get("rows", ()))
    by_site: dict[str, list[Mapping[str, Any]]] = {}
    by_service: dict[str, list[Mapping[str, Any]]] = {}
    by_site_service: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        site = row["site_id"]
        service = row["service_type"]
        by_site.setdefault(site, []).append(row)
        by_service.setdefault(service, []).append(row)
        by_site_service.setdefault(f"{site}|{service}", []).append(row)
    metrics = plan.get("metrics_by_group", {})
    output: dict[str, Any] = {"by_site": {}, "by_service": {}, "by_site_service": {}}
    for name, groups in (
        ("by_site", by_site),
        ("by_service", by_service),
        ("by_site_service", by_site_service),
    ):
        for key, values in sorted(groups.items()):
            summary = _aggregate(values)
            if name == "by_site_service" and key in metrics:
                summary["calibration"] = metrics[key]
            output[name][key] = summary
    reasons: list[str] = []
    for row in rows:
        for reason in row.get("reasons", ()):
            if reason not in reasons:
                reasons.append(reason)
    output["reasons"] = reasons
    output["status"] = plan.get("status")
    output["summary"] = plan.get("summary", _aggregate(rows))
    return output


def compare_plans(base: Mapping[str, Any], revised: Mapping[str, Any]) -> dict[str, Any]:
    constraints = StaffingConstraints(
        **{
            key: value
            for key, value in (base.get("constraints") or {}).items()
            if key in {"service_minutes", "shift_minutes", "occupancy_target", "overload_threshold", "maximum_agents"}
        }
    )
    return CapacityPlanner(constraints).compare(base, revised)
