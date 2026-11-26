"""Offline, descriptive walk-forward correction of paper fill-model residuals.

Inputs must be obtained from calibrate_paper_worker, not arbitrary reports.
This module cannot certify inputs, trade, promote, or establish causal alpha.
The fixed-tape model uses the realized tape; this is not a live forecast.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from .config import MARKET_TZ
from .paper_worker_calibration import WorkerCalibrationReport
from .provenance import stable_hash

_MIN_SESSIONS = 5
_PRIOR_WEIGHT = Decimal(5)
_ROLES = ("INITIAL", "CHASE")


@dataclass(frozen=True, slots=True)
class ShadowFold:
    session_date: str
    role: str
    report_sha256: str
    training_report_sha256: tuple[str, ...]
    candidates: int
    comparable: int
    correction: Decimal | None
    baseline_mae: Decimal | None
    challenger_mae: Decimal | None
    status: str


@dataclass(frozen=True, slots=True)
class ShadowStudy:
    folds: tuple[ShadowFold, ...]
    report_sha256: str
    authority: str = "RESEARCH_ONLY"
    estimand: str = "RETROSPECTIVE_PAPER_REPLAY_CALIBRATION"
    authorizes_training: bool = False
    authorizes_promotion: bool = False
    authorizes_live_orders: bool = False


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / len(values)


def evaluate_shadow_correction(
    reports: tuple[WorkerCalibrationReport, ...],
) -> ShadowStudy:
    """Compare a frozen baseline against prior-session shrinkage on paired rows.

    Equal weight per training session, fixed role buckets, 24-hour embargo before
    the target session's local midnight. Unqualified sessions remain visible.
    Caller must enforce the complete prospective session roster externally.
    """
    if len(reports) > 366:
        raise ValueError("shadow study exceeds bounded session roster")
    sessions = sorted(reports, key=lambda report: date.fromisoformat(report.session_date))
    for field in ("session_date", "source_sha256", "outcome_report_sha256", "report_sha256"):
        if len({getattr(report, field) for report in sessions}) != len(sessions):
            raise ValueError("duplicate shadow session or evidence")
    history: dict[str, list[tuple[datetime, str, Decimal]]] = {
        role: [] for role in _ROLES
    }
    folds: list[ShadowFold] = []
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        for report in sessions:
            session = date.fromisoformat(report.session_date)
            midnight = datetime.combine(session, time.min, tzinfo=MARKET_TZ)
            reviewed = report.reviewed_ts_utc
            if reviewed is not None and (
                reviewed.tzinfo is None or reviewed.utcoffset() is None
                or reviewed < midnight
            ):
                raise ValueError("invalid review timestamp")
            if (
                report.authority != "RESEARCH_ONLY" or report.authorizes_training
                or report.authorizes_promotion or report.authorizes_live_orders
                or report.candidates != len(report.rows)
                or len({row.intent_seq for row in report.rows}) != len(report.rows)
            ):
                raise ValueError("invalid shadow report boundary")
            for role in _ROLES:
                rows = [row for row in report.rows if row.role == role]
                pairs: list[tuple[Decimal, Decimal]] = []
                for row in rows:
                    if row.signed_fraction_error is None:
                        continue
                    observed, modeled, qty = (
                        row.observed_quantity, row.modeled_quantity, row.requested_quantity,
                    )
                    if (
                        row.disposition != "DURABLE_DISPATCH" or observed is None
                        or modeled is None or qty <= 0
                        or not 0 <= observed <= qty or not 0 <= modeled <= qty
                        or row.signed_fraction_error != Decimal(observed - modeled) / qty
                    ):
                        raise ValueError("invalid paired shadow row")
                    pairs.append((Decimal(observed) / qty, Decimal(modeled) / qty))
                eligible = [
                    (digest, bias) for stamp, digest, bias in history[role]
                    if stamp <= midnight - timedelta(hours=24)
                ]
                correction: Decimal | None = None
                base_mae: Decimal | None = None
                challenger_mae: Decimal | None = None
                status = "INSUFFICIENT_PRIOR_SESSIONS"
                clean = not report.blockers and reviewed is not None and bool(pairs)
                if not clean:
                    status = "UNQUALIFIED_OR_UNOBSERVED"
                elif len(eligible) >= _MIN_SESSIONS:
                    correction = (
                        sum((bias for _, bias in eligible), Decimal(0))
                        / (len(eligible) + _PRIOR_WEIGHT)
                    )
                    base_mae = _mean([abs(actual - model) for actual, model in pairs])
                    challenger_mae = _mean([
                        abs(actual - max(Decimal(0), min(Decimal(1), model + correction)))
                        for actual, model in pairs
                    ])
                    status = "PAIRED_DESCRIPTIVE_ONLY"
                folds.append(ShadowFold(
                    report.session_date, role, report.report_sha256,
                    tuple(digest for digest, _ in eligible), len(rows), len(pairs),
                    correction, base_mae, challenger_mae, status,
                ))
                if clean and reviewed is not None:
                    residuals = [
                        row.signed_fraction_error for row in rows
                        if row.signed_fraction_error is not None
                    ]
                    history[role].append((reviewed, report.report_sha256, _mean(residuals)))
    material = tuple(folds)
    return ShadowStudy(material, stable_hash(("paper-shadow-correction-v1", material)))
