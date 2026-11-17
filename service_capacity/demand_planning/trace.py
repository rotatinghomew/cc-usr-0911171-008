"""Traceable records for forecast calibration and capacity decisions.

Every calibration answers the question "which evidence did we actually know
when we made this call?".  The ledger makes that answer durable: each run is
stored with the decision date, the knowledge cutoff, the fingerprint of the
visible observations, the excluded late records, the scored folds and the
resulting calibration factor.  A later reader can therefore audit the decision
without replaying the whole upstream database.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..contracts import Event, fingerprint
from ..storage import Database
from .forecast_calibration import format_decimal

__all__ = [
    "TraceError",
    "CalibrationRun",
    "CalibrationLedger",
]


class TraceError(RuntimeError):
    pass


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return format_decimal(value)
    return value


@dataclass(frozen=True)
class CalibrationRun:
    run_id: str
    as_of: str
    knowledge_cutoff: str
    source_fingerprint: str
    status: str
    deterministic: bool
    calibration_factor: str
    mae: str | None
    bias: str | None
    fold_count: int
    excluded_late: tuple[Mapping[str, Any], ...] = ()
    missing_dates: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    sites: tuple[str, ...] = ()
    services: tuple[str, ...] = ()
    recorded_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    result_digest: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "as_of": self.as_of,
            "knowledge_cutoff": self.knowledge_cutoff,
            "source_fingerprint": self.source_fingerprint,
            "status": self.status,
            "deterministic": self.deterministic,
            "calibration_factor": self.calibration_factor,
            "mae": self.mae,
            "bias": self.bias,
            "fold_count": self.fold_count,
            "excluded_late": [dict(item) for item in self.excluded_late],
            "missing_dates": list(self.missing_dates),
            "reasons": list(self.reasons),
            "sites": list(self.sites),
            "services": list(self.services),
            "recorded_at": self.recorded_at.isoformat(),
            "result_digest": self.result_digest,
        }


class CalibrationLedger:
    """Append-only ledger of calibration and plan runs."""

    kind = "revision"

    def __init__(self, database: Database | None = None):
        self.database = database
        self._runs: list[CalibrationRun] = []
        self._plans: list[dict[str, Any]] = []

    # -- recording -------------------------------------------------------
    def record(self, result: Mapping[str, Any]) -> CalibrationRun:
        if not isinstance(result, Mapping):
            raise TraceError("calibration result is required")
        for name in ("as_of", "source_fingerprint", "status"):
            if name not in result:
                raise TraceError(f"calibration result is missing {name}")
        run = CalibrationRun(
            run_id=str(result.get("run_id") or uuid.uuid4()),
            as_of=str(result["as_of"]),
            knowledge_cutoff=str(result.get("knowledge_cutoff", "")),
            source_fingerprint=str(result["source_fingerprint"]),
            status=str(result["status"]),
            deterministic=bool(result.get("deterministic", False)),
            calibration_factor=str(result.get("calibration_factor", "1")),
            mae=result.get("mae"),
            bias=result.get("bias"),
            fold_count=int(result.get("fold_count", 0)),
            excluded_late=tuple(result.get("excluded_late", ())),
            missing_dates=tuple(result.get("missing_dates", ())),
            reasons=tuple(result.get("reasons", ())),
            sites=tuple(
                sorted({str(group.get("site_id")) for group in result.get("groups", ())})
            ),
            services=tuple(
                sorted(
                    {str(group.get("service_type")) for group in result.get("groups", ())}
                )
            ),
            result_digest=fingerprint(_jsonable(dict(result))),
        )
        self._runs.append(run)
        if self.database is not None:
            self._persist(run)
        return run

    def record_plan(self, plan: Mapping[str, Any], calibration: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(plan, Mapping):
            raise TraceError("capacity plan is required")
        entry = {
            "plan_id": str(plan.get("plan_id") or uuid.uuid4()),
            "status": plan.get("status"),
            "deterministic": bool(plan.get("deterministic", False)),
            "constraints": dict(plan.get("constraints", {})),
            "summary": dict(plan.get("summary", {})),
            "as_of": (calibration or plan).get("as_of"),
            "source_fingerprint": (calibration or plan).get("source_fingerprint"),
            "result_digest": fingerprint(_jsonable(dict(plan))),
            "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        self._plans.append(entry)
        return entry

    def _persist(self, run: CalibrationRun) -> None:
        assert self.database is not None
        payload = run.as_dict()
        record = {
            "revision_id": run.run_id,
            "owner_id": "calibration",
            "category": "forecast_calibration",
            "status": "active" if run.status == "ok" else "received",
            "effective_from": run.as_of,
            "quantity": run.fold_count,
            "amount": "0",
            "attributes": payload,
        }
        with self.database.transaction() as connection:
            version = self.database.put_record(connection, self.kind, record)
            event = Event(
                str(uuid.uuid4()),
                "calibration",
                run.run_id,
                "calibrated",
                dt.datetime.now(dt.timezone.utc),
                payload,
                version,
            )
            self.database.append_event(connection, event)
            self.database.enqueue(
                connection, "calibration:" + run.run_id, "forecast.calibration", run.run_id, payload
            )

    # -- reading ---------------------------------------------------------
    def runs(self) -> tuple[CalibrationRun, ...]:
        return tuple(self._runs)

    def plans(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._plans)

    def latest(self) -> CalibrationRun | None:
        return self._runs[-1] if self._runs else None

    def for_site(self, site_id: str) -> tuple[CalibrationRun, ...]:
        return tuple(
            run
            for run in self._runs
            if str(site_id) in run.sites
            or any(
                str(item.get("site_id")) == str(site_id) for item in run.excluded_late
            )
        )

    def audit(self) -> dict[str, Any]:
        problems: list[str] = []
        previous_time: dt.datetime | None = None
        for run in self._runs:
            if previous_time is not None and run.recorded_at < previous_time:
                problems.append(f"out_of_order:{run.run_id}")
            if not run.source_fingerprint:
                problems.append(f"missing_fingerprint:{run.run_id}")
            if run.status == "ok" and not run.deterministic:
                problems.append(f"ok_but_not_deterministic:{run.run_id}")
            previous_time = run.recorded_at
        return {
            "runs": len(self._runs),
            "plans": len(self._plans),
            "problems": problems,
            "latest": self._runs[-1].as_dict() if self._runs else None,
        }

    def restore(self) -> tuple[CalibrationRun, ...]:
        """Reload runs persisted to the database (for audit across restarts)."""

        if self.database is None:
            return ()
        runs = []
        for row in self.database.list_records(self.kind, owner_id="calibration"):
            attributes = row.get("attributes", {})
            if attributes.get("run_id") and attributes.get("source_fingerprint"):
                runs.append(
                    CalibrationRun(
                        run_id=str(attributes["run_id"]),
                        as_of=str(attributes.get("as_of")),
                        knowledge_cutoff=str(attributes.get("knowledge_cutoff", "")),
                        source_fingerprint=str(attributes["source_fingerprint"]),
                        status=str(attributes.get("status", "unknown")),
                        deterministic=bool(attributes.get("deterministic", False)),
                        calibration_factor=str(attributes.get("calibration_factor", "1")),
                        mae=attributes.get("mae"),
                        bias=attributes.get("bias"),
                        fold_count=int(attributes.get("fold_count", 0)),
                        excluded_late=tuple(attributes.get("excluded_late", ())),
                        missing_dates=tuple(attributes.get("missing_dates", ())),
                        reasons=tuple(attributes.get("reasons", ())),
                        sites=tuple(attributes.get("sites", ())),
                        services=tuple(attributes.get("services", ())),
                        result_digest=str(attributes.get("result_digest", "")),
                    )
                )
        self._runs = runs
        return tuple(runs)
