"""Forward fill logger: fwd-v2.1 columns, produced by the repo's single rule engine.

Columns 1-40 reproduce ``fwd_logger_v2.py`` + ``fwd_logger_v2_1.py`` cell-for-cell (except the
two self-hash/version cells, which name the producing code). Columns after that are exec-core
reporting tags; none of them feeds back into a rule.

Output files belong outside git (see .gitignore ``var/``); this module never writes Discord text.
"""

from __future__ import annotations

import csv
import io
import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import ForwardRules
from .execution_model import (
    BarStore,
    CallSpec,
    ChaseModel,
    Outcome,
    evaluate_call,
    track_for,
)

EXEC_CORE_VERSION = "exec-core-v1"

V2_COLUMNS: tuple[str, ...] = (
    "call_id", "date", "post_time_et", "seen_time_et", "lag_min", "ticker", "side", "strike",
    "expiry", "skip_reason", "counted", "entry_minute", "limit_px", "window_lows", "fill_type",
    "entry_ts", "entry_px", "exit_ts", "exit_px", "exit_type", "target", "trail", "tier",
    "net_pct", "acct_cash_before", "acct_equity_before", "acct_action", "acct_shares",
    "rules_version", "logger_sha256",
)  # fmt: skip
V21_COLUMNS: tuple[str, ...] = (
    "sub10", "post_price", "seen_time_used", "intended_fill", "actual_fill", "actual_fill_src",
    "slip_vs_post_pct", "slip_vs_intended_pct", "reporting_version", "v21_sha256",
)  # fmt: skip
TAG_COLUMNS: tuple[str, ...] = (
    "track", "traded", "qqq_vs_open", "repeat_same_day", "called_prior_5d",
    "close_shadow_net_pct", "exec_core_version",
)  # fmt: skip
ALL_COLUMNS = V2_COLUMNS + V21_COLUMNS + TAG_COLUMNS
# Cells that identify the producing program rather than the trade; excluded from parity diffs.
PROVENANCE_COLUMNS = frozenset({"logger_sha256", "v21_sha256", "reporting_version"})
V21_REPORTING_VERSION = "fwd-v2.1-reporting (rules = fwd-v2-FINAL unchanged)"


def _numpy_round(value: float, digits: int) -> float:
    # np.round semantics (scale, round-half-even, unscale) so cells match the numpy-based logger.
    scale = 10.0**digits
    return float(round(value * scale)) / scale


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _sort_key(row: Outcome) -> str:
    entry = row.get("entry_ts")
    return str(entry) if entry else f"{row['date']} {row['post_time_et']}"


def _business_days_back(day: date, count: int) -> date:
    current = day
    remaining = count
    while remaining:
        current -= timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def build_rows(
    calls: Sequence[Mapping[str, object]],
    bars: BarStore,
    rules: ForwardRules,
    *,
    only_date: str | None = None,
    start_equity: float | None = None,
    chase_model: ChaseModel = ChaseModel.MINUTE_HIGH,
    rules_label: str | None = None,
) -> list[dict[str, str]]:
    specs = [CallSpec.from_mapping(raw) for raw in calls]
    by_id = {spec.call_id: spec for spec in specs}
    selected = [
        spec
        for spec in specs
        if spec.channel == rules.channel and (only_date is None or spec.date == only_date)
    ]
    outcomes = sorted(
        (evaluate_call(spec, bars, rules, chase_model=chase_model) for spec in selected),
        key=_sort_key,
    )
    _account_track(
        outcomes,
        float(rules.account_track_start_equity_usd) if start_equity is None else start_equity,
        rules,
    )
    first_seen: dict[tuple[str, str], str] = {}
    for spec in sorted(selected, key=lambda item: (item.date, item.time_et)):
        first_seen.setdefault((spec.date, spec.ticker), spec.time_et)
    called_dates: dict[str, set[date]] = {}
    for spec in selected:
        called_dates.setdefault(spec.ticker, set()).add(date.fromisoformat(spec.date))

    rows: list[dict[str, str]] = []
    for outcome in outcomes:
        spec = by_id[str(outcome["call_id"])]
        cells = {key: _cell(outcome.get(key)) for key in V2_COLUMNS}
        cells["rules_version"] = rules_label or rules.rules_version
        cells["logger_sha256"] = f"{EXEC_CORE_VERSION}:{rules.sha256}"
        cells.update(_v21_cells(outcome, spec, bars))
        cells.update(_tag_cells(outcome, spec, bars, rules, first_seen, called_dates))
        rows.append(cells)
    return rows


def _account_track(outcomes: list[Outcome], start_equity: float, rules: ForwardRules) -> None:
    """R6: $150 whole-share track, same order of float operations as the frozen logger."""

    fee = 1 - rules.cost_pct / 100
    cash = start_equity
    open_positions: list[tuple[datetime, int, float, float]] = []
    for row in outcomes:
        if not row.get("counted"):
            continue
        entered = datetime.fromisoformat(str(row["entry_ts"]))
        entry_px = float(str(row["entry_px"]))
        for position in [p for p in open_positions if p[0] <= entered]:
            cash += position[1] * position[3] * fee
            open_positions.remove(position)
        equity = cash + sum(p[1] * p[2] for p in open_positions)
        shares = math.floor(cash / entry_px) if cash >= 1 else 0
        row["acct_equity_before"] = _numpy_round(equity, 2)
        row["acct_cash_before"] = _numpy_round(cash, 2)
        if cash < 1:
            row["acct_action"] = "skipped_cash_in_open_trade"
        elif shares < 1:
            row["acct_action"] = "skipped_price_gt_cash"
        else:
            row["acct_action"] = "bought"
            row["acct_shares"] = shares
            cash -= shares * entry_px
            open_positions.append(
                (
                    datetime.fromisoformat(str(row["exit_ts"])),
                    shares,
                    entry_px,
                    float(str(row["exit_px"])),
                )
            )


def _v21_cells(outcome: Outcome, spec: CallSpec, bars: BarStore) -> dict[str, str]:
    cells = dict.fromkeys(V21_COLUMNS, "")
    cells["seen_time_used"] = spec.seen_time_et or "default_post+2"
    post_price: float | str = ""
    days = bars.get(spec.ticker)
    if days is not None:
        day = days.get(spec.date)
        found = day.open_at(spec.post_ts()) if day is not None else None
        post_price = found if found is not None else "UNKNOWN_no_bar_at_post_minute"
    cells["post_price"] = _cell(post_price)
    if outcome.get("counted") == 1:
        entry_px = float(str(outcome["entry_px"]))
        limit = float(str(outcome["limit_px"]))
        cells["sub10"] = str(int(entry_px < 10))
        cells["intended_fill"] = repr(limit)
        if spec.actual_fill is not None:
            actual, source = spec.actual_fill, "supplied"
        else:
            actual, source = entry_px, "modeled"
        cells["actual_fill"] = repr(actual)
        cells["actual_fill_src"] = source
        if isinstance(post_price, float):
            cells["slip_vs_post_pct"] = repr(round((actual / post_price - 1) * 100, 4))
        cells["slip_vs_intended_pct"] = repr(round((actual / limit - 1) * 100, 4))
    cells["reporting_version"] = V21_REPORTING_VERSION
    cells["v21_sha256"] = EXEC_CORE_VERSION
    return cells


def _tag_cells(
    outcome: Outcome,
    spec: CallSpec,
    bars: BarStore,
    rules: ForwardRules,
    first_seen: Mapping[tuple[str, str], str],
    called_dates: Mapping[str, set[date]],
) -> dict[str, str]:
    track = track_for(spec.post_ts(), rules)
    counted = outcome.get("counted") == 1
    cells = dict.fromkeys(TAG_COLUMNS, "")
    cells["track"] = track.value
    cells["traded"] = str(int(counted and track.value == "main"))
    first = first_seen.get((spec.date, spec.ticker))
    cells["repeat_same_day"] = str(int(first is not None and spec.time_et > first))
    day = date.fromisoformat(spec.date)
    floor = _business_days_back(day, 5)
    prior = called_dates.get(spec.ticker, set())
    cells["called_prior_5d"] = str(int(any(floor <= other < day for other in prior)))
    if counted:
        qqq_days = bars.get("QQQ")
        qqq = qqq_days.get(spec.date) if qqq_days is not None else None
        index = outcome.get("_entry_index")
        if qqq is not None and qqq.session and isinstance(index, int):
            entry_ts = datetime.fromisoformat(str(outcome["entry_minute"]))
            at = qqq.session_index(entry_ts)
            if at < len(qqq.session):
                cells["qqq_vs_open"] = (
                    "above" if qqq.session[at].open >= qqq.session[0].open else "below"
                )
        if outcome.get("tier") == rules.hold_to_close_shadow_tier:
            close_px = float(str(outcome["_close_px"]))
            entry_px = float(str(outcome["entry_px"]))
            cells["close_shadow_net_pct"] = repr((close_px / entry_px - 1) * 100 - rules.cost_pct)
    cells["exec_core_version"] = EXEC_CORE_VERSION
    return cells


def write_csv(rows: Iterable[Mapping[str, str]], path: str | Path | None = None) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(ALL_COLUMNS), lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in ALL_COLUMNS})
    text = buffer.getvalue()
    if path is not None:
        Path(path).write_text(text, encoding="utf-8", newline="")
    return text


def parity_diff(
    ours: Sequence[Mapping[str, str]],
    reference: Sequence[Mapping[str, str]],
    columns: Sequence[str] = V2_COLUMNS + V21_COLUMNS,
) -> list[str]:
    """Cell-level differences keyed by call_id, ignoring provenance cells."""

    problems: list[str] = []
    ref = {row["call_id"]: row for row in reference}
    mine = {row["call_id"]: row for row in ours}
    for call_id in sorted(set(ref) ^ set(mine)):
        problems.append(f"{call_id}:row_missing_on_{'ours' if call_id in ref else 'reference'}")
    for call_id in sorted(set(ref) & set(mine)):
        for column in columns:
            if column in PROVENANCE_COLUMNS or column == "rules_version":
                continue
            left, right = mine[call_id].get(column, ""), ref[call_id].get(column, "")
            if left != right:
                problems.append(f"{call_id}:{column}:{left!r}!={right!r}")
    if [row["call_id"] for row in ours] != [row["call_id"] for row in reference]:
        problems.append("row_order_differs")
    return problems
