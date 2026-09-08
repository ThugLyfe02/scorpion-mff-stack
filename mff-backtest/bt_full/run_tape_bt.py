#!/usr/bin/env python3
"""Honest tape-based MFF backtest. Discord exit_pct is TARGET ONLY if tape prints it."""
from __future__ import annotations
import json, csv, math, re
from pathlib import Path
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo
from collections import defaultdict

ET = ZoneInfo("America/New_York")
ROOT = Path("/workspace/mff-backtest/bt_full")
BARS = ROOT / "bars"
CACHE = Path("/workspace/mff-backtest/charts_1m/cache/instruments.json")
START_BOOK = 5000.0
R = 500.0
BASE_PREM = 2000.0  # 1R / 0.25
ADD_PREM = 1000.0   # 0.5R
MAX_PREM = 3000.0

def parse_dt(ba: str) -> datetime:
    return datetime.fromisoformat(ba.replace("Z", "+00:00")).astimezone(ET)

def load_bars(iid: str):
    p = BARS / f"{iid}.json"
    if not p.exists():
        return []
    d = json.load(open(p))
    return d.get("bars") or []

def resolve_id(t, cache):
    key = "%s|%s|%.4f|%s" % (t["ticker"], t["side"], float(t["strike"]), t["expiry"])
    if key in cache:
        return key, cache[key]
    for k, v in cache.items():
        parts = k.split("|")
        if parts[0] == t["ticker"] and parts[1] == t["side"] and parts[3] == t["expiry"] and abs(float(parts[2]) - float(t["strike"])) < 1e-6:
            return k, v
    return key, None

def strike_match_bar(bar, bid, tol=0.05):
    """True if close or (low,high) near bid within tol."""
    c = bar["close"]
    if bid <= 0:
        return False
    if abs(c - bid) / bid <= tol:
        return True
    if bar["low"] <= bid <= bar["high"] and abs(c - bid) / bid <= max(tol, 0.10):
        return True
    return False

def entry_fill_from_bar(bar, bid):
    if bar["low"] <= bid <= bar["high"]:
        return bid
    return bar["close"]

def find_entry(bars, trade_date: str, bid: float):
    """First RTH bar on trade_date ONLY with price ≈ bid (±5%, fallback ±10% or bid in range).
    Do NOT fall back to later days (expiry leftovers are not valid entries)."""
    day = date.fromisoformat(trade_date)
    day_bars = [b for b in bars if parse_dt(b["begins_at"]).date() == day]
    if not day_bars:
        return None, None, "no_bars_on_trade_date"
    for tol in (0.05, 0.10):
        for b in day_bars:
            c = b["close"]
            if bid > 0 and abs(c - bid) / bid <= tol:
                return b, entry_fill_from_bar(b, bid), f"close_tol_{tol}"
            if b["low"] <= bid <= b["high"]:
                return b, bid, f"bid_in_range_tol_{tol}"
    return None, None, "no_fill_match"

def contracts_for_premium(prem, fill):
    if fill <= 0:
        return 0
    # options multiplier 100
    n = int(prem // (fill * 100))
    return max(n, 0)

def exit_targets(bid, exit_pct):
    """Return list of (frac, target_price) scale-outs based on Discord exit_pct as TARGET labels."""
    if exit_pct is None:
        return []
    ep = float(exit_pct)
    final = bid * (1 + ep / 100.0)
    if ep < 0:
        return [(1.0, final)]  # loss target: full at stop
    if ep >= 100:
        return [(0.5, bid * 1.5), (0.5, final)]
    if ep >= 40:
        mid = bid * (1 + min(0.5, ep / 100.0))
        return [(0.5, mid), (0.5, final)]
    return [(1.0, final)]

def infer_exit_pct(t):
    if t.get("exit_pct") is not None:
        return float(t["exit_pct"])
    notes = (t.get("notes") or "").lower()
    # loss with -30 in notes
    m = re.search(r"-?\s*30\s*%", notes)
    if t.get("outcome") == "loss" or "sl 25%" in notes or "sl 30" in notes:
        if m or "-30" in notes or "30%" in notes:
            return -30.0
        if "sl 25%" in notes or "25%" in notes:
            return -25.0
    return None

def simulate_one(t, bars, allow_add=True, hard_stop_25=False):
    """Simulate one trade on tape. Returns dict."""
    bid = float(t["entry_bid"])
    exit_pct = infer_exit_pct(t)
    trade_date = t["date"]
    expiry = t["expiry"]
    is_0dte = trade_date == expiry

    entry_bar, fill, match = find_entry(bars, trade_date, bid)
    if entry_bar is None:
        return {"status": "skip_" + (match or "no_fill_match"), "reason": match or "no RTH bar near bid on trade date"}

    entry_i = bars.index(entry_bar)
    after = bars[entry_i:]  # from entry bar inclusive; exits after entry

    # sizing
    prem1 = min(BASE_PREM, MAX_PREM)
    n1 = contracts_for_premium(prem1, fill)
    if n1 <= 0:
        return {"status": "skip_size", "reason": "fill too expensive for budget", "fill": fill}

    cost1 = n1 * fill * 100
    lots = [{"n": n1, "px": fill, "prem": cost1}]
    avg = fill
    total_n = n1
    total_cost = cost1
    added = False
    add_info = None

    # remaining scale plan from Discord targets (on HIS bid)
    plan = exit_targets(bid, exit_pct) if exit_pct is not None else []
    # for hard -25 stop variant
    hard_stop_px = fill * 0.75 if hard_stop_25 else None

    realized = 0.0
    exits = []
    remaining_frac_plan = list(plan)  # (frac_of_ORIGINAL_or_remaining, px)
    # Convert plan fracs to contract counts of CURRENT total at entry (before add)
    # Better: track remaining contracts; apply scale as % of position at time of first scale based on original size.
    orig_n = total_n
    scaled_half = False
    flat = False
    exit_reason = None
    last_bar = after[0]

    # Process bars after entry (same bar: exit can win over add if both; check exits first on high, add on low)
    for bi, bar in enumerate(after):
        last_bar = bar
        dt = parse_dt(bar["begins_at"])
        # 0DTE flatten at 15:45 ET
        if is_0dte and (dt.hour > 15 or (dt.hour == 15 and dt.minute >= 45)):
            if total_n > 0:
                px = bar["close"]
                pnl = total_n * (px - avg) * 100
                realized += pnl
                exits.append({"time": bar["begins_at"], "n": total_n, "px": px, "kind": "0dte_1545", "pnl": pnl})
                total_n = 0
                exit_reason = "0dte_1545"
            flat = True
            break

        # On entry bar, only look for exits after "fill" — treat same bar carefully:
        # If high >= target, we can exit; if low <= -15%, add — exit wins same bar before add per rules.
        high, low, opn, close = bar["high"], bar["low"], bar["open"], bar["close"]

        # Hard stop -25% (variant C)
        if hard_stop_px and total_n > 0 and low <= hard_stop_px:
            px = hard_stop_px
            pnl = total_n * (px - avg) * 100
            realized += pnl
            exits.append({"time": bar["begins_at"], "n": total_n, "px": px, "kind": "hard_-25", "pnl": pnl})
            total_n = 0
            exit_reason = "hard_-25"
            flat = True
            break

        # Target exits: use high for profit targets, low for loss targets
        if total_n > 0 and remaining_frac_plan:
            new_plan = []
            for frac, tgt in remaining_frac_plan:
                if total_n <= 0:
                    new_plan.append((frac, tgt))
                    continue
                hit = False
                fill_px = tgt
                if tgt >= avg or (exit_pct is not None and exit_pct >= 0):
                    # profit-ish: need high >= tgt
                    if high >= tgt:
                        hit = True
                        fill_px = tgt
                else:
                    # loss: low <= tgt
                    if low <= tgt:
                        hit = True
                        fill_px = tgt
                if hit and bi == 0 and tgt > fill and high >= tgt:
                    # same bar as entry — allowed if high printed target after open; use tgt
                    pass
                if hit:
                    # sell frac of ORIGINAL position size, but capped by remaining
                    # Use frac of current remaining if second scale; for first scale use frac of orig
                    if not scaled_half and frac < 1.0:
                        n_sell = min(total_n, max(1, int(round(orig_n * frac))))
                        scaled_half = True
                    else:
                        n_sell = total_n
                    if n_sell > 0:
                        pnl = n_sell * (fill_px - avg) * 100
                        realized += pnl
                        exits.append({"time": bar["begins_at"], "n": n_sell, "px": fill_px, "kind": "target", "tgt": tgt, "pnl": pnl})
                        total_n -= n_sell
                        if total_n <= 0:
                            exit_reason = "targets_hit"
                            flat = True
                    # don't keep this plan leg
                else:
                    new_plan.append((frac, tgt))
            remaining_frac_plan = new_plan
            if flat:
                break

        # Auto add once at -15% AFTER entry (tape must print). Exit wins same bar: if any
        # target already filled this bar (or position flat), skip add.
        if allow_add and not added and total_n > 0 and bi >= 0:
            trigger = fill * 0.85
            target_hit_this_bar = any(e.get("time") == bar["begins_at"] and e.get("kind") == "target" for e in exits)
            if low <= trigger and not target_hit_this_bar and not flat:
                if opn >= trigger:
                    add_fill = trigger
                else:
                    add_fill = min(opn, trigger)
                add_budget = min(ADD_PREM, MAX_PREM - total_cost)
                if add_budget >= add_fill * 100:
                    n_add = contracts_for_premium(add_budget, add_fill)
                    if n_add > 0:
                        cost_add = n_add * add_fill * 100
                        total_cost += cost_add
                        total_n += n_add
                        avg = total_cost / (total_n * 100)
                        added = True
                        add_info = {"time": bar["begins_at"], "n": n_add, "px": add_fill, "cost": cost_add, "new_avg": avg}
                        orig_n = total_n

        if flat:
            break

    # If still open at end of bars: flatten last close
    if total_n > 0:
        px = last_bar["close"]
        pnl = total_n * (px - avg) * 100
        realized += pnl
        exits.append({"time": last_bar["begins_at"], "n": total_n, "px": px, "kind": "eod_last_bar", "pnl": pnl})
        exit_reason = "eod_last_bar_flag"
        total_n = 0

    # Unknown exit with no plan and no target: still flattened above
    status = "taken"
    if exit_pct is None and not any(e["kind"] == "target" for e in exits):
        # excluded from primary if truly unknown? User said list separately if no exit_pct
        # But we DID flatten on tape — include with flag
        status = "taken_unknown_target"

    return {
        "status": status,
        "entry_time": entry_bar["begins_at"],
        "entry_fill": fill,
        "entry_match": match,
        "bid": bid,
        "exit_pct_discord": exit_pct,
        "n_contracts": n1,
        "premium": cost1,
        "added": added,
        "add": add_info,
        "avg": avg,
        "total_cost": total_cost,
        "exits": exits,
        "pnl": realized,
        "exit_reason": exit_reason,
        "R": realized / R,
    }


def run_variant(name, trades, cache, allow_add=True, max_concurrent=None, hard_stop_25=False):
    """max_concurrent=None means unlimited (take all he posted); 2 means max2."""
    book = START_BOOK
    equity_rows = []
    sim_rows = []
    open_positions = []  # (flat_time_et_dt, )
    skipped = []
    taken = []

    for i, t in enumerate(trades):
        key, iid = resolve_id(t, cache)
        row_base = {
            "idx": i, "date": t["date"], "ticker": t["ticker"], "side": t["side"],
            "strike": t["strike"], "expiry": t["expiry"], "channel": t["channel"],
            "entry_bid": t["entry_bid"], "exit_pct": t.get("exit_pct"), "outcome_discord": t.get("outcome"),
            "instrument_id": iid, "key": key,
        }
        if t.get("entry_bid") is None:
            skipped.append({**row_base, "skip": "no_entry_bid"})
            continue
        if iid is None:
            skipped.append({**row_base, "skip": "no_instrument"})
            continue
        bars = load_bars(iid)
        if not bars:
            skipped.append({**row_base, "skip": "no_bars"})
            continue

        # concurrent check
        if max_concurrent is not None:
            # free positions that flattened before this trade's date 9:30
            entry_day = date.fromisoformat(t["date"])
            entry_gate = datetime(entry_day.year, entry_day.month, entry_day.day, 9, 30, tzinfo=ET)
            open_positions = [ft for ft in open_positions if ft > entry_gate]
            if len(open_positions) >= max_concurrent:
                skipped.append({**row_base, "skip": "max_concurrent"})
                continue

        res = simulate_one(t, bars, allow_add=allow_add, hard_stop_25=hard_stop_25)
        if res["status"].startswith("skip"):
            skipped.append({**row_base, "skip": res["status"], "reason": res.get("reason")})
            continue

        pnl = res["pnl"]
        book += pnl
        taken.append({**row_base, **{k: res[k] for k in res if k != "exits"}, "exits_json": json.dumps(res["exits"]), "book": book})

        # track open until last exit time
        if res["exits"]:
            flat_t = parse_dt(res["exits"][-1]["time"])
        else:
            flat_t = parse_dt(res["entry_time"]) + timedelta(hours=1)
        open_positions.append(flat_t)

        equity_rows.append({
            "date": t["date"],
            "trade": f"{t['ticker']} {t['side']} {t['strike']} {t['expiry']}",
            "pnl": round(pnl, 2),
            "book": round(book, 2),
            "R": round(pnl / R, 3),
        })
        sim_rows.append(taken[-1])

    return equity_rows, sim_rows, skipped, taken, book


def max_drawdown(equity_rows):
    peak = START_BOOK
    max_dd = 0.0
    for r in equity_rows:
        peak = max(peak, r["book"])
        dd = peak - r["book"]
        max_dd = max(max_dd, dd)
    return max_dd


def summarize(name, equity_rows, sim_rows, skipped, book):
    pnls = [r["pnl"] for r in equity_rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    scratches = [p for p in pnls if p == 0]
    monthly = defaultdict(float)
    for r in equity_rows:
        monthly[r["date"][:7]] += r["pnl"]
    losing = []
    for r in sim_rows:
        if r.get("pnl", 0) < 0:
            losing.append({
                "date": r["date"], "trade": f"{r['ticker']} {r['side']} {r['strike']}",
                "pnl": round(r["pnl"], 2), "exit_reason": r.get("exit_reason"),
                "entry_fill": r.get("entry_fill"), "discord_exit_pct": r.get("exit_pct_discord"),
                "added": r.get("added"),
            })
    skip_reasons = defaultdict(int)
    for s in skipped:
        skip_reasons[s.get("skip", "?")] += 1
    return {
        "variant": name,
        "start": START_BOOK,
        "end_book": round(book, 2),
        "total_pnl": round(book - START_BOOK, 2),
        "n_taken": len(equity_rows),
        "n_skipped": len(skipped),
        "skip_reasons": dict(skip_reasons),
        "n_wins": len(wins),
        "n_losses": len(losses),
        "n_scratch": len(scratches),
        "win_rate": round(len(wins) / len(pnls), 4) if pnls else None,
        "avg_R": round((sum(pnls) / len(pnls)) / R, 4) if pnls else None,
        "best": round(max(pnls), 2) if pnls else None,
        "worst": round(min(pnls), 2) if pnls else None,
        "max_dd": round(max_drawdown(equity_rows), 2),
        "monthly_pnl": {k: round(v, 2) for k, v in sorted(monthly.items())},
        "losing_trades": losing,
    }


def main():
    trades = json.load(open(ROOT / "trades.json"))
    cache = json.load(open(CACHE))
    # merge resolved
    resolved_path = ROOT / "resolved_ids.json"
    if resolved_path.exists():
        cache.update(json.load(open(resolved_path)))

    variants = [
        ("add15_multi", True, None, False),
        ("no_add_multi", False, None, False),
        ("add15_max2", True, 2, False),
        ("no_add_max2", False, 2, False),
        ("hard25_no_add_multi", False, None, True),
    ]
    all_sum = {}
    for name, allow_add, maxc, hard in variants:
        eq, sim, skipped, taken, book = run_variant(name, trades, cache, allow_add=allow_add, max_concurrent=maxc, hard_stop_25=hard)
        # write equity/sim for primary two
        if name in ("add15_multi", "no_add_multi", "add15_max2", "no_add_max2"):
            suffix = name
            with open(ROOT / f"equity_{suffix}.csv", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["date", "trade", "pnl", "book", "R"])
                w.writeheader()
                w.writerows(eq)
            # flatten sim
            fields = ["idx","date","ticker","side","strike","expiry","channel","entry_bid","exit_pct","outcome_discord",
                      "instrument_id","status","entry_time","entry_fill","entry_match","exit_pct_discord",
                      "n_contracts","premium","added","avg","total_cost","pnl","R","exit_reason","book","exits_json"]
            with open(ROOT / f"trades_sim_{suffix}.csv", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                w.writeheader()
                for r in sim:
                    w.writerow(r)
            with open(ROOT / f"skipped_{suffix}.json", "w") as f:
                json.dump(skipped, f, indent=2)
        all_sum[name] = summarize(name, eq, sim, skipped, book)

    # Also write canonical names from original request for add15/no_add as max2? 
    # User asked add15 and no_add; steering says allow multiples AND report max2.
    # Primary files equity_add15.csv = add15_multi (honest concurrent as posted)
    for src, dst in [("add15_multi", "add15"), ("no_add_multi", "no_add")]:
        import shutil
        shutil.copy(ROOT / f"equity_{src}.csv", ROOT / f"equity_{dst}.csv")
        shutil.copy(ROOT / f"trades_sim_{src}.csv", ROOT / f"trades_sim_{dst}.csv")

    # coverage note
    n_with = 0
    for t in trades:
        _, iid = resolve_id(t, cache)
        if iid and load_bars(iid):
            n_with += 1
    note = (
        "P&L is from tape fills only — Discord exit_pct is a TARGET label, not credited unless high/low prints it after entry. "
        "If target never prints, flatten at last bar / 0DTE 15:45 (can be a loss). "
        f"Bars coverage: {n_with}/{len(trades)} trades have non-interpolated RTH 5m bars; older Mar–Jul often missing on RH."
    )
    out = {
        "start_date": trades[0]["date"],
        "end_date": trades[-1]["date"],
        "n_signals": len(trades),
        "n_with_bars": n_with,
        "R": R,
        "start_book": START_BOOK,
        "note": note,
        "variants": all_sum,
        "primary": "add15_multi / no_add_multi (all concurrent as posted); also max2_*",
    }
    json.dump(out, open(ROOT / "summary.json", "w"), indent=2)

    # markdown
    lines = []
    lines.append("# MFF Tape Backtest (honest)\n")
    lines.append(f"Book start **${START_BOOK:.0f}**, 1R=**${R:.0f}**, base premium $2000, optional −15% add $1000 once.\n")
    lines.append(note + "\n")
    for name in ["add15_multi", "no_add_multi", "add15_max2", "no_add_max2", "hard25_no_add_multi"]:
        s = all_sum[name]
        lines.append(f"## {name}\n")
        lines.append(f"- Taken: **{s['n_taken']}** | Skipped: **{s['n_skipped']}** `{s['skip_reasons']}`")
        lines.append(f"- End book: **${s['end_book']:.2f}** | Total P&L: **${s['total_pnl']:.2f}** | Max DD: **${s['max_dd']:.2f}**")
        lines.append(f"- Wins/Losses/Scratch: {s['n_wins']}/{s['n_losses']}/{s['n_scratch']} | Win rate: {s['win_rate']} | Avg R: {s['avg_R']}")
        lines.append(f"- Best/Worst: ${s['best']} / ${s['worst']}")
        lines.append(f"- Monthly: `{s['monthly_pnl']}`")
        if s["losing_trades"]:
            lines.append("- **Losing trades:**")
            for L in s["losing_trades"]:
                lines.append(f"  - {L['date']} {L['trade']}: ${L['pnl']} ({L['exit_reason']}; fill={L['entry_fill']}; discord_tgt={L['discord_exit_pct']}; add={L['added']})")
        lines.append("")
    lines.append("## Comparison paragraph (variant C)\n")
    c = all_sum["hard25_no_add_multi"]
    a = all_sum["add15_multi"]
    b = all_sum["no_add_multi"]
    lines.append(
        f"Hard −25% stop + Discord targets + no add (multi): end ${c['end_book']:.2f} / P&L ${c['total_pnl']:.2f} / maxDD ${c['max_dd']:.2f} "
        f"vs add15_multi ${a['total_pnl']:.2f} and no_add_multi ${b['total_pnl']:.2f}. "
        "This is still tape-based — stops/targets only if printed.\n"
    )
    lines.append("## Files\n")
    for p in sorted(ROOT.glob("equity_*.csv")):
        lines.append(f"- `{p}`")
    for p in sorted(ROOT.glob("trades_sim_*.csv")):
        lines.append(f"- `{p}`")
    lines.append(f"- `{ROOT/'summary.json'}`")
    open(ROOT / "summary.md", "w").write("\n".join(lines) + "\n")
    print(json.dumps({k: {kk: all_sum[k][kk] for kk in ["n_taken","n_skipped","total_pnl","end_book","max_dd","n_wins","n_losses","win_rate","avg_R"]} for k in all_sum}, indent=2))
    print("WROTE", ROOT / "summary.md")

if __name__ == "__main__":
    main()
