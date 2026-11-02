#!/usr/bin/env python3
"""Deterministic backtest-parity replay: exec-core rule engine vs the frozen research loggers.

Research-only (needs pandas/pyarrow and the private edge_study tape on the research box; nothing
here is committed data). Usage:

  python research/exec_core_parity.py --edge-study /workspace/edge_study \
      --fwd-logger /workspace/scorpion_access/fwd_logger --out var/parity

Checks, per call set (discovery = calls.json + stk_1m.parquet, held-out = holdout.json +
oos114/stk_1m_ho.parquet):
  1. every fwd-v2.1 cell (except provenance hashes) equals the frozen fwd_logger_v2_1.py output;
  2. per-call net_pct equals oos128 rows.csv ch0_3 - 0.10 (the oos128 chase variant);
  3. aggregate mean and n reproduce oos128's published +0.39% / n=197.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

from scorpion.config import load_forward_rules
from scorpion.execution_model import ChaseModel, DayBars, MinuteBar
from scorpion.fill_logger import build_rows, parity_diff, write_csv


def _load_v2(fwd_dir: Path):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("fwd_v2", fwd_dir / "fwd_logger_v2.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bar_store(frames: dict[str, pd.DataFrame], rules) -> dict[str, dict[str, DayBars]]:  # type: ignore[no-untyped-def]
    store: dict[str, dict[str, DayBars]] = {}
    for symbol, frame in frames.items():
        days: dict[str, list[MinuteBar]] = {}
        for ts, o, h, low, c in zip(
            frame.index,
            frame.open.values,
            frame.high.values,
            frame.low.values,
            frame.close.values,
            strict=True,
        ):
            days.setdefault(ts.strftime("%Y-%m-%d"), []).append(
                MinuteBar(ts.to_pydatetime(), float(o), float(h), float(low), float(c))
            )
        store[symbol] = {
            day: DayBars(
                bars, session_open=rules.session_open_et, session_last=rules.session_last_bar_et
            )
            for day, bars in days.items()
        }
    return store


def run_set(
    name: str, calls: list[dict], bars_file: Path, fwd_dir: Path, out: Path, qqq_file: Path | None
) -> dict[str, object]:  # type: ignore[type-arg]
    rules = load_forward_rules()
    v2 = _load_v2(fwd_dir)
    selected = [c for c in calls if c.get("channel", "options-king") == "options-king"]
    frames = v2.load_bars(
        [str(bars_file)], {c["ticker"] for c in selected}, {c["date"] for c in selected}
    )
    if qqq_file is not None:
        q = v2.load_bars([str(qqq_file)], {"QQQ"}, {c["date"] for c in selected})
        frames.setdefault("QQQ", q.get("QQQ")) if "QQQ" in q else None
    store = bar_store({k: v for k, v in frames.items() if v is not None}, rules)
    if "QQQ" in store and "QQQ" not in {c["ticker"] for c in selected}:
        pass
    ours = build_rows(calls, store, rules)
    strict = build_rows(calls, store, rules, chase_model=ChaseModel.LIMIT_THROUGH)
    write_csv(ours, out / f"{name}_exec_core.csv")

    with tempfile.TemporaryDirectory() as tmp:
        calls_path = Path(tmp) / "calls.json"
        calls_path.write_text(json.dumps(calls))
        ref_path = out / f"{name}_frozen_v2_1.csv"
        subprocess.run(
            [
                sys.executable,
                str(fwd_dir / "fwd_logger_v2_1.py"),
                "--calls",
                str(calls_path),
                "--bars",
                str(bars_file),
                "--log",
                str(ref_path),
            ],
            check=True,
            capture_output=True,
        )
    reference = list(csv.DictReader(ref_path.open()))
    # QQQ bars change only the exec-core tag column, never a fwd-v2.1 cell.
    diffs = parity_diff(ours, reference)
    counted = [r for r in ours if r["counted"] == "1"]
    net = [float(r["net_pct"]) for r in counted]
    main = [float(r["net_pct"]) for r in counted if r["track"] == "main"]
    shadow = [float(r["net_pct"]) for r in counted if r["track"] != "main"]
    sub20_main = [
        float(r["net_pct"]) for r in counted if r["track"] == "main" and r["tier"] == "sub20"
    ]
    return {
        "set": name,
        "rows": len(ours),
        "reference_rows": len(reference),
        "counted": len(counted),
        "cell_diffs": len(diffs),
        "first_diffs": diffs[:10],
        "mean_net_pct": statistics.fmean(net) if net else None,
        "main_track": {"n": len(main), "mean": statistics.fmean(main) if main else None},
        "shadow_post1100": {"n": len(shadow), "mean": statistics.fmean(shadow) if shadow else None},
        "main_sub20": {
            "n": len(sub20_main),
            "mean": statistics.fmean(sub20_main) if sub20_main else None,
        },
        "limit_through_chase_sensitivity": _sens(strict),
        "_rows": ours,
    }


def _sens(rows: list[dict[str, str]]) -> dict[str, object]:
    counted = [r for r in rows if r["counted"] == "1"]
    main = [float(r["net_pct"]) for r in counted if r["track"] == "main"]
    skipped = sum(1 for r in rows if r["skip_reason"] == "chase_unfilled_by_skip_deadline")
    return {"counted": len(counted), "chase_skips": skipped,
            "main_n": len(main), "main_mean": statistics.fmean(main) if main else None,
            "all_mean": statistics.fmean([float(r["net_pct"]) for r in counted]) if counted else None}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge-study", type=Path, required=True)
    ap.add_argument("--fwd-logger", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("var/parity"))
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    es = a.edge_study
    disc_calls = json.loads((es / "calls.json").read_text())
    ho_calls = json.loads((es / "holdout.json").read_text())["rows"]
    results = [
        run_set(
            "disc", disc_calls, es / "stk_1m.parquet", a.fwd_logger, a.out, es / "mkt_1m.parquet"
        ),
        run_set(
            "ho",
            ho_calls,
            es / "oos114" / "stk_1m_ho.parquet",
            a.fwd_logger,
            a.out,
            es / "mkt_1m_ho.parquet",
        ),
    ]
    # oos128 per-call cross-check (chase variant, 10 bps) on (set, date, ticker, entry px).
    oos = pd.read_csv(es / "oos128" / "rows.csv")
    expected = sorted(
        (("disc" if s == "disc" else "ho"), d, t, round(px, 6), round(ch - 0.1, 9))
        for s, d, t, px, ch in zip(oos.s, oos.d, oos.tk, oos.px, oos.ch0_3, strict=True)
    )
    got = sorted(
        (
            res["set"],
            r["date"],
            r["ticker"],
            round(float(r["limit_px"]), 6),
            round(float(r["net_pct"]), 9),
        )
        for res in results
        for r in res["_rows"]
        if r["counted"] == "1"  # type: ignore[union-attr]
    )
    all_net = [x[4] for x in got]
    summary = {
        "sets": [{k: v for k, v in res.items() if k != "_rows"} for res in results],
        "oos128": {
            "expected_n": len(expected),
            "got_n": len(got),
            "per_call_exact_matches": sum(1 for x in got if x in set(expected)),
            "published_mean_pct": round(float((oos.ch0_3 - 0.1).mean()), 4),
            "replayed_mean_pct": round(statistics.fmean(all_net), 4),
        },
    }
    (a.out / "parity_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
