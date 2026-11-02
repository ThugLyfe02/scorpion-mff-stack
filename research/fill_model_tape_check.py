#!/usr/bin/env python3
"""Check the 1-minute-bar fill model against the Databento XNAS.ITCH trade tape (research only).

For every counted call in the parity replay (var/parity/*_exec_core.csv) it pulls the trades in
[entry minute - 2 min, entry minute + 16 min] through scorpion's cost guard (estimate first,
refuse > $30), caches them under var/tape/ (gitignored, never committed), and measures:
  * limit-fill agreement: bar rule (low <= L*(1-2bps) within 3 min) vs any tape print there;
  * queue realism: shares printed strictly below the limit vs a $1,000 paper order;
  * chase price: modeled minute high vs the first print after the chase time;
  * live 'current price' (last print before the entry minute) vs the bar open the model uses;
  * time from entry minute to first trade-through.

  PYTHONPATH=src python research/fill_model_tape_check.py --estimate-only
  PYTHONPATH=src python research/fill_model_tape_check.py --out var/tape_check.json
"""

from __future__ import annotations

import argparse
import json
import statistics
from decimal import Decimal
from pathlib import Path

import pandas as pd

from scorpion.databento_feed import PullRequest, guarded_pull, historical_client

TAPE = Path("var/tape")


def calls() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for name in ("disc", "ho"):
        frame = pd.read_csv(f"var/parity/{name}_exec_core.csv", dtype=str)
        rows += frame[frame.counted == "1"].to_dict("records")
    return rows


def request_for(row: dict[str, str]) -> PullRequest:
    start = pd.Timestamp(row["entry_minute"]).tz_convert("UTC")
    return PullRequest(
        "XNAS.ITCH", "trades", (row["ticker"],),
        (start - pd.Timedelta(minutes=2)).isoformat(), (start + pd.Timedelta(minutes=16)).isoformat(),
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--estimate-only", action="store_true")
    ap.add_argument("--out", type=Path, default=Path("var/tape_check.json"))
    a = ap.parse_args()
    client = historical_client()
    rows = calls()
    TAPE.mkdir(parents=True, exist_ok=True)
    total = Decimal("0")
    for row in rows:
        total += Decimal(str(client.metadata.get_cost(**request_for(row).params())))
    print(json.dumps({"calls": len(rows), "estimated_total_usd": str(total)}))
    if a.estimate_only or total > Decimal("30"):
        return
    results = []
    for row in rows:
        path = TAPE / f"{row['call_id']}.dbn.zst"
        if not path.exists():
            guarded_pull(client, request_for(row), sink=lambda data, p=path: data.to_file(str(p)))
        import databento

        trades = databento.DBNStore.from_file(str(path)).to_df()
        if trades.empty:
            results.append({"call_id": row["call_id"], "tape": "empty"})
            continue
        ts = trades.index.tz_convert("America/New_York")
        px = trades["price"].astype(float).values
        sz = trades["size"].astype(float).values
        entry = pd.Timestamp(row["entry_minute"])
        limit = float(row["limit_px"])
        through = limit * (1 - 0.0002)
        window = (ts >= entry) & (ts < entry + pd.Timedelta(minutes=3))
        hit = window & (px <= through)
        tape_fill = bool(hit.any())
        before = ts < entry
        shares_needed = int(1000 // limit)
        res = {
            "call_id": row["call_id"], "ticker": row["ticker"], "tier": row["tier"],
            "bar_fill": row["fill_type"] == "limit", "tape_fill": tape_fill,
            "shares_below_limit": float(sz[window & (px < limit)].sum()),
            "shares_needed_1000usd": shares_needed,
            "first_through_s": float((ts[hit][0] - entry).total_seconds()) if tape_fill else None,
            "bar_open_vs_tape_first_bps": (
                (limit / px[ts >= entry][0] - 1) * 1e4 if (ts >= entry).any() else None),
            "last_print_before_entry_vs_bar_open_bps": (
                (px[before][-1] / limit - 1) * 1e4 if before.any() else None),
        }
        if row["fill_type"] != "limit":
            chase_t = entry + pd.Timedelta(minutes=3)
            after = ts >= chase_t
            if after.any():
                res["chase_model_px"] = float(row["entry_px"])
                res["chase_tape_first_px"] = float(px[after][0])
                res["chase_model_minus_tape_bps"] = (float(row["entry_px"]) / px[after][0] - 1) * 1e4
        results.append(res)
    ok = [r for r in results if "bar_fill" in r]

    def med(key: str, subset: list[dict[str, object]] | None = None) -> float | None:
        vals = [float(r[key]) for r in (subset or ok) if r.get(key) is not None]
        return round(statistics.median(vals), 3) if vals else None

    filled = [r for r in ok if r["bar_fill"]]
    chases = [r for r in ok if "chase_model_minus_tape_bps" in r]
    summary = {
        "calls_with_tape": len(ok),
        "estimated_total_usd": str(total),
        "fill_agreement": sum(r["bar_fill"] == r["tape_fill"] for r in ok),
        "bar_fill_tape_nofill": sum(r["bar_fill"] and not r["tape_fill"] for r in ok),
        "bar_nofill_tape_fill": sum((not r["bar_fill"]) and r["tape_fill"] for r in ok),
        "filled_with_size_strictly_below_limit_ge_order": sum(
            r["shares_below_limit"] >= r["shares_needed_1000usd"] for r in filled),
        "filled_n": len(filled),
        "median_first_through_s": med("first_through_s", filled),
        "median_bar_open_vs_first_print_bps": med("bar_open_vs_tape_first_bps"),
        "median_last_print_before_entry_vs_bar_open_bps": med("last_print_before_entry_vs_bar_open_bps"),
        "chase_n": len(chases),
        "median_chase_model_minus_tape_bps": med("chase_model_minus_tape_bps", chases),
        "mean_chase_model_minus_tape_bps": (
            round(statistics.fmean(r["chase_model_minus_tape_bps"] for r in chases), 3) if chases else None),
    }
    a.out.write_text(json.dumps({"summary": summary, "rows": results}, indent=1, default=str))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
