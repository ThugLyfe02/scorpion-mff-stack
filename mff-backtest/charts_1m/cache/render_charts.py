#!/usr/bin/env python3
"""Render MFF option charts from Robinhood 5m bars (1m unavailable)."""
import json, os, re, glob
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pandas as pd
import mplfinance as mpf
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

ET = ZoneInfo("America/New_York")
ROOT = "/workspace/mff-backtest/charts_1m"
BARS_DIR = os.path.join(ROOT, "bars")
PNG_DIR = os.path.join(ROOT, "png")
RAW_DIR = "/workspace/agent-tools"
os.makedirs(BARS_DIR, exist_ok=True)
os.makedirs(PNG_DIR, exist_ok=True)

INSTRUMENTS = {
  "NVDA|call|225.0000|2026-08-28": "a047efdc-2fdd-457f-b903-6587cd42cd9f",
  "QQQ|call|706.0000|2026-08-24": "4402e722-03c5-43f6-8163-b5f99f4070bb",
  "QQQ|call|714.0000|2026-08-25": "2a7ea4e2-450f-4ed2-886b-4c6487d9f7c0",
  "NKE|call|42.5000|2026-12-18": "a85e7428-521a-42cf-ac5e-de23b344390f",
  "QQQ|call|711.0000|2026-08-25": "86d1e64d-9c8d-4b0f-a4fc-b2d46c0616f9",
  "SPX|call|7670.0000|2026-08-25": "78832d05-6c35-4046-9490-18f9969d1019",
  "TSLA|call|360.0000|2026-08-31": "48988873-2527-4263-9f71-b66b2a932ad2",
  "QQQ|call|712.0000|2026-08-26": "721ef24b-204d-498b-9956-aad4049a2cd3",
  "QQQ|call|720.0000|2026-08-27": "9bd44612-3af8-4340-8ced-8f82e172ed57",
  "NVDA|call|230.0000|2026-08-31": "9df07d7c-635b-4fcc-902b-6891959e8a68",
  "QQQ|call|720.0000|2026-08-28": "f9d1a68a-2a54-4bd2-b0e1-3831dd28accd",
  "TSLA|call|360.0000|2026-09-04": "39eddb1f-5486-4785-b39c-a193dbde28b6",
  "TSLL|call|10.0000|2026-10-16": "6e814299-5d01-42bd-8c5a-3297f00d072e",
  "NVDA|put|210.0000|2026-09-11": "2f3a9430-b001-4859-a3ad-6b5104c429f2",
  "QQQ|put|710.0000|2026-09-01": "ca5d39f3-aa9a-4563-bcae-cc75825fa564",
  "TSLA|call|355.0000|2026-09-02": "3f98187d-4560-42d0-b5c5-122c2d5e1613",
  "QQQ|put|705.0000|2026-09-02": "f64b55b8-ebf2-4492-a212-f2a5470bd179",
  "GOOGL|call|340.0000|2026-09-09": "065b2142-27c3-4726-8192-090371ac1b30",
  "QQQ|call|712.0000|2026-09-03": "f6740c29-86e6-4e95-9bb1-cce0a84d89e7",
  "TSLA|call|390.0000|2026-09-04": "dc18d5ae-32b9-4439-8902-e2c542320469",
  "TSLA|call|362.5000|2026-09-04": "036a9e3a-e20b-4958-8e31-ccee283dc89e",
  "TSLA|call|355.0000|2026-09-04": "cc95462c-e279-4b4e-b4c0-f94b8daa981f",
}

# Prefer 5m dumps (newer, useful); also allow prior week_bars
FIVE_M_FILES = [
  "70d03866-0234-440c-99db-da157843d037.txt",
  "38b376a3-87c4-4ae0-bae8-ef32d086fb0d.txt",
  "cc8e6363-0f1c-4f87-9edf-73bfd1c48003.txt",
  "84ae622a-afde-4451-b948-4e7a84962741.txt",
  "1c9eff8b-b0d3-4243-b189-ad9c59f368f1.txt",
  "b56d84b2-a195-470d-908c-628df062fb1e.txt",
  "114c0be2-7057-40a6-a3e3-d4e845329eee.txt",
  "f5a009f9-ca22-4343-920f-15e79b904ee1.txt",
  "caca8ee6-e3c4-4f00-978c-954f93f033bc.txt",
]
# Also scan for any other 5m results written today that contain "5minute"
EXTRA_GLOB = [
  "c7926c23-b6a5-4bfe-b1be-1f03583a1ceb.txt",  # may be minute junk
]


def parse_mcp_file(path):
    text = open(path, encoding="utf-8", errors="ignore").read()
    i = text.find("{"); j = text.rfind("}")
    if i < 0 or j < 0:
        return []
    try:
        obj = json.loads(text[i:j+1])
    except Exception:
        return []
    return obj.get("data", {}).get("results", []) or []

def bar_dt(b):
    return datetime.fromisoformat(b["begins_at"].replace("Z", "+00:00"))

def clean_bars(bars):
    out = []
    for b in bars:
        if b.get("interpolated"):
            continue
        # accept missing interpolated as real; require session reg when present
        sess = b.get("session")
        if sess is not None and sess != "" and sess != "reg":
            continue
        o = float(b.get("open_price") or b.get("open"))
        h = float(b.get("high_price") or b.get("high"))
        l = float(b.get("low_price") or b.get("low"))
        c = float(b.get("close_price") or b.get("close"))
        out.append({"begins_at": b["begins_at"], "open": o, "high": h, "low": l, "close": c})
    return out

def ingest():
    by_id_date = {}
    files = list(FIVE_M_FILES)
    # also include any agent-tools file mentioning 5minute from this session
    for fn in os.listdir(RAW_DIR):
        if not fn.endswith(".txt"):
            continue
        path = os.path.join(RAW_DIR, fn)
        # quick filter
        try:
            head = open(path, encoding="utf-8", errors="ignore").read(5000)
        except Exception:
            continue
        if "5minute" not in head and '"interval":"5minute"' not in head and "5minute" not in open(path, encoding="utf-8", errors="ignore").read(200000):
            # still try known files
            if fn not in FIVE_M_FILES:
                continue
        if fn not in files:
            files.append(fn)
    for fn in files:
        path = os.path.join(RAW_DIR, fn)
        if not os.path.exists(path):
            continue
        for r in parse_mcp_file(path):
            if not isinstance(r, dict):
                continue
            if r.get("interval") and r["interval"] != "5minute":
                continue
            iid = r.get("instrument_id") or r.get("id")
            if not iid:
                continue
            bars = clean_bars(r.get("bars") or [])
            if not bars:
                continue
            # split by date (ET)
            for b in bars:
                dt = bar_dt(b).astimezone(ET)
                day = dt.strftime("%Y-%m-%d")
                key = (iid, day)
                by_id_date.setdefault(key, {})
                by_id_date[key][b["begins_at"]] = b
    # also merge week_bars.json if present
    wb = "/workspace/mff-backtest/week_bars.json"
    if os.path.exists(wb):
        data = json.load(open(wb))
        for k, v in data.items():
            iid = v.get("id") or v.get("instrument_id")
            for b in v.get("bars") or []:
                if b.get("interpolated"):
                    continue
                bb = {
                    "begins_at": b["begins_at"],
                    "open": float(b.get("open") or b.get("open_price")),
                    "high": float(b.get("high") or b.get("high_price")),
                    "low": float(b.get("low") or b.get("low_price")),
                    "close": float(b.get("close") or b.get("close_price")),
                }
                day = bar_dt(bb).astimezone(ET).strftime("%Y-%m-%d")
                key = (iid, day)
                by_id_date.setdefault(key, {})
                by_id_date[key][bb["begins_at"]] = bb
    # write
    for (iid, day), mp in by_id_date.items():
        bars = [mp[k] for k in sorted(mp)]
        out = {"instrument_id": iid, "date": day, "interval": "5minute", "bars": bars}
        path = os.path.join(BARS_DIR, f"{day}_{iid}.json")
        json.dump(out, open(path, "w"), indent=2)
    print(f"ingested {len(by_id_date)} instrument-days")
    return by_id_date


def strike_fmt(s):
    f = float(s)
    return f"{int(f)}.0000" if f == int(f) else f"{f:.4f}"

def ikey(t):
    return f"{t['ticker']}|{t['side']}|{strike_fmt(t['strike'])}|{t['expiry']}"

def load_bars_for(iid, date, extra_dates=None):
    bars = []
    dates = [date] + (extra_dates or [])
    for d in dates:
        path = os.path.join(BARS_DIR, f"{d}_{iid}.json")
        if os.path.exists(path):
            bars.extend(json.load(open(path))["bars"])
    # dedupe
    mp = {b["begins_at"]: b for b in bars}
    return [mp[k] for k in sorted(mp)]

def find_entry(bars, bid):
    if not bars or not bid:
        return None
    bid = float(bid)
    # try 5% then 10% then 20% then in-range
    for thr in (0.05, 0.10, 0.20):
        for b in bars:
            c = b["close"]
            if abs(c - bid) / bid <= thr:
                fill = bid if (b["low"] <= bid <= b["high"]) else c
                return {"time": bar_dt(b), "fill": fill, "bar": b, "match": f"close_within_{int(thr*100)}pct"}
    for b in bars:
        if b["low"] <= bid <= b["high"]:
            return {"time": bar_dt(b), "fill": bid, "bar": b, "match": "bid_in_range"}
    # closest close; for sparse (<=2 bars) accept Discord bid as fill
    best = min(bars, key=lambda b: abs(b["close"] - bid))
    if len(bars) <= 2:
        fill = bid if bid > 0 else best["close"]
        return {"time": bar_dt(best), "fill": fill, "bar": best, "match": "sparse_discord_bid"}
    # otherwise closest if within 35%
    if abs(best["close"] - bid) / bid <= 0.35:
        return {"time": bar_dt(best), "fill": best["close"], "bar": best, "match": "closest_within_35pct"}
    # last resort: first RTH bar with Discord bid fill (annotate)
    b0 = bars[0]
    return {"time": bar_dt(b0), "fill": bid, "bar": b0, "match": "first_bar_discord_bid"}

def find_exit(bars, entry_time, fill, exit_pct):
    if exit_pct is None or str(exit_pct).strip() == "":
        return {"status": "no_discord_exit_pct"}
    try:
        pct = float(exit_pct)
    except Exception:
        return {"status": "bad_exit_pct", "raw": exit_pct}
    target = fill * (1.0 + pct / 100.0)
    after = [b for b in bars if bar_dt(b) > entry_time]
    if pct >= 0:
        for b in after:
            if b["high"] >= target:
                return {"status": "hit", "time": bar_dt(b), "px": target, "touch": b["high"], "pct": pct, "bar": b}
    else:
        for b in after:
            if b["low"] <= target:
                return {"status": "hit", "time": bar_dt(b), "px": target, "touch": b["low"], "pct": pct, "bar": b}
    # best effort
    if after:
        if pct >= 0:
            best = max(after, key=lambda b: b["high"])
            return {"status": "miss", "time": bar_dt(best), "px": target, "best": best["high"], "pct": pct, "bar": best}
        else:
            best = min(after, key=lambda b: b["low"])
            return {"status": "miss", "time": bar_dt(best), "px": target, "best": best["low"], "pct": pct, "bar": best}
    return {"status": "miss_no_bars", "px": target, "pct": pct}

def to_df(bars):
    rows = []
    for b in bars:
        dt = bar_dt(b).astimezone(ET).replace(tzinfo=None)
        rows.append({"Date": dt, "Open": b["open"], "High": b["high"], "Low": b["low"], "Close": b["close"]})
    df = pd.DataFrame(rows).set_index("Date")
    return df


TV = mpf.make_mpf_style(
    base_mpf_style="nightclouds",
    marketcolors=mpf.make_marketcolors(
        up="#26a69a", down="#ef5350", edge="inherit", wick="inherit", volume="in",
        ohlc="inherit"
    ),
    facecolor="#131722", edgecolor="#131722", figcolor="#131722",
    gridcolor="#2a2e39", gridstyle="--",
    rc={"axes.labelcolor": "#d1d4dc", "xtick.color": "#d1d4dc", "ytick.color": "#d1d4dc",
        "axes.edgecolor": "#2a2e39", "font.size": 9}
)

def render_chart(trade, iid, bars, entry, exit_info, png_path):
    if not entry or not bars:
        return False, "no_entry_or_bars"
    entry_t = entry["time"].astimezone(ET).replace(tzinfo=None)
    # window
    win_start = entry_t - timedelta(minutes=10)
    win_end = entry_t + timedelta(minutes=110)
    outside = False
    exit_t_naive = None
    if exit_info and exit_info.get("status") in ("hit", "miss") and exit_info.get("time"):
        exit_t_naive = exit_info["time"].astimezone(ET).replace(tzinfo=None)
        if exit_t_naive > win_end:
            # extend up to 4h if same calendar day
            if exit_t_naive.date() == entry_t.date() and (exit_t_naive - entry_t) <= timedelta(hours=4):
                win_end = exit_t_naive + timedelta(minutes=5)
            else:
                outside = True
    df_all = to_df(bars)
    df = df_all[(df_all.index >= win_start) & (df_all.index <= win_end)]
    if df.empty:
        # fallback: first 24 bars of day
        df = df_all.iloc[:24]
        if df.empty:
            return False, "empty_window"
    addplots = []
    # entry hline via alines later
    title = f"{trade['ticker']} ${trade['strike']} {trade['side'].upper()} exp {trade['expiry']} | {trade['channel']} | {trade['date']}"
    notes = (trade.get("notes") or "")[:80]
    subtitle = f"Discord BID ${trade['entry_bid']} · posted {trade.get('time_et')} ET (label only) · outcome {trade.get('outcome')} · {notes}"

    fig, axes = mpf.plot(
        df, type="candle", style=TV, returnfig=True, figsize=(14, 7),
        title=title, datetime_format="%H:%M", xrotation=0,
        tight_layout=True, warn_too_much_data=10000,
    )
    ax = axes[0]
    # entry marker
    ax.axvline(df.index.get_indexer([entry_t], method="nearest")[0], color="#2196f3", ls="--", lw=1.2, alpha=0.9)
    # use price line
    ax.axhline(entry["fill"], color="#2196f3", ls=":", lw=1, alpha=0.7)
    # annotate entry at last matching index
    try:
        xi = df.index.get_indexer([entry_t], method="nearest")[0]
        ax.scatter([xi], [entry["fill"]], color="#2196f3", s=60, zorder=5, marker="^")
        ax.annotate(f"ENTRY @ ${entry['fill']:.2f}", xy=(xi, entry["fill"]),
                    xytext=(8, 18), textcoords="offset points", color="#64b5f6", fontsize=9,
                    arrowprops=dict(arrowstyle="->", color="#64b5f6"))
    except Exception:
        pass

    if exit_info and exit_info.get("status") == "no_discord_exit_pct":
        ax.text(0.01, 0.02, "no Discord exit %", transform=ax.transAxes, color="#ffb74d", fontsize=9)
    elif exit_info and exit_info.get("px") is not None:
        tgt = exit_info["px"]
        ax.axhline(tgt, color="#ff9800", ls="--", lw=1, alpha=0.8)
        if exit_info.get("status") == "hit" and exit_t_naive is not None and not outside:
            try:
                xi = df.index.get_indexer([exit_t_naive], method="nearest")[0]
                ax.scatter([xi], [tgt], color="#ff9800", s=60, zorder=5, marker="v")
                ax.annotate(f"EXIT {exit_info['pct']:+.0f}% @ ${tgt:.2f}", xy=(xi, tgt),
                            xytext=(8, -28), textcoords="offset points", color="#ffb74d", fontsize=9,
                            arrowprops=dict(arrowstyle="->", color="#ffb74d"))
            except Exception:
                pass
        elif outside or exit_info.get("status") in ("hit", "miss"):
            et_str = exit_t_naive.strftime("%H:%M") if exit_t_naive else "?"
            label = f"EXIT TARGET {exit_info['pct']:+.0f}% @ ${tgt:.2f} · {et_str} ET"
            if outside:
                label += " (outside 2h window)"
            if exit_info.get("status") == "miss":
                label += " · NOT REACHED"
            ax.text(0.99, 0.98, label, transform=ax.transAxes, ha="right", va="top",
                    color="#ffb74d", fontsize=8, bbox=dict(boxstyle="round", facecolor="#1e222d", edgecolor="#ff9800", alpha=0.9))

    # subtitle box
    ax.text(0.01, 0.98, subtitle, transform=ax.transAxes, ha="left", va="top",
            color="#d1d4dc", fontsize=8,
            bbox=dict(boxstyle="round", facecolor="#1e222d", edgecolor="#2a2e39", alpha=0.9))
    ax.text(0.99, 0.02, "RH 5m RTH (1m unavailable)", transform=ax.transAxes, ha="right", va="bottom",
            color="#787b86", fontsize=7)

    fig.savefig(png_path, dpi=140, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    return True, "ok"


def next_trading_day(d):
    dt = datetime.strptime(d, "%Y-%m-%d")
    while True:
        dt += timedelta(days=1)
        if dt.weekday() < 5:
            return dt.strftime("%Y-%m-%d")

def main():
    json.dump(INSTRUMENTS, open(os.path.join(ROOT, "cache", "instruments.json"), "w"), indent=2)
    by = ingest()
    trades = json.load(open(os.path.join(ROOT, "trades_2w.json")))
    manifest = []
    used_names = set()
    ok = 0
    for idx, t in enumerate(trades):
        key = ikey(t)
        iid = INSTRUMENTS.get(key)
        entry = None
        exit_info = None
        png_rel = None
        err = None
        window = None
        try:
            if not iid:
                raise RuntimeError(f"no instrument for {key}")
            extra = []
            # GOOGL 100% may hit next day
            if t.get("exit_pct") and float(t["exit_pct"] or 0) >= 100:
                extra.append(next_trading_day(t["date"]))
            bars = load_bars_for(iid, t["date"], extra)
            if not bars:
                raise RuntimeError("no_real_bars")
            # save trade-specific bars copy
            tpath = os.path.join(BARS_DIR, f"trade_{idx:02d}_{t['date']}_{t['ticker']}_{t['side']}_{t['strike']}.json")
            json.dump({"trade_index": idx, "instrument_id": iid, "bars": bars}, open(tpath, "w"), indent=2)
            entry = find_entry(bars, t.get("entry_bid"))
            if not entry:
                raise RuntimeError("entry_not_found")
            exit_info = find_exit(bars, entry["time"], entry["fill"], t.get("exit_pct"))
            strike_token = str(t["strike"]).replace(".", "p")
            base = f"{t['date']}_{t['ticker']}_{t['side']}_{strike_token}"
            name = base + ".png"
            if name in used_names:
                name = f"{base}_{t['channel']}.png"
            used_names.add(name)
            png_path = os.path.join(PNG_DIR, name)
            success, msg = render_chart(t, iid, bars, entry, exit_info, png_path)
            if not success:
                raise RuntimeError(msg)
            png_rel = f"png/{name}"
            window = {
                "start_et": (entry["time"].astimezone(ET) - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M"),
                "end_et": (entry["time"].astimezone(ET) + timedelta(minutes=110)).strftime("%Y-%m-%d %H:%M"),
                "interval": "5minute",
            }
            ok += 1
            status = "ok"
        except Exception as e:
            status = "error"
            err = str(e)
        rec = {
            "index": idx,
            "date": t["date"],
            "ticker": t["ticker"],
            "side": t["side"],
            "strike": t["strike"],
            "expiry": t["expiry"],
            "channel": t["channel"],
            "outcome": t.get("outcome"),
            "entry_bid": t.get("entry_bid"),
            "exit_pct_discord": t.get("exit_pct"),
            "instrument_id": iid,
            "status": status,
            "error": err,
            "png": png_rel,
            "entry_time_et": entry["time"].astimezone(ET).strftime("%Y-%m-%d %H:%M") if entry else None,
            "entry_fill": entry["fill"] if entry else None,
            "entry_match": entry.get("match") if entry else None,
            "exit_status": exit_info.get("status") if exit_info else None,
            "exit_time_et": exit_info["time"].astimezone(ET).strftime("%Y-%m-%d %H:%M") if exit_info and exit_info.get("time") else None,
            "exit_px": exit_info.get("px") if exit_info else None,
            "exit_pct": exit_info.get("pct") if exit_info else None,
            "window": window,
            "note": "RH option 1m bars flat/interpolated; charts use 5m RTH non-interpolated",
        }
        manifest.append(rec)
        print(idx, status, t["ticker"], t["strike"], err or png_rel)

    json.dump(manifest, open(os.path.join(ROOT, "manifest.json"), "w"), indent=2)
    # README
    lines = ["# MoneyForFun 2-week option charts (5-minute RTH)", "", 
             "Robinhood `interval=minute` option historicals return flat interpolated bars; charts use **5-minute** `bounds=24_5` non-interpolated RTH bars.",
             "",
             f"OK: {ok} / {len(trades)}",
             "",
             "| Date | Ticker | Side | Strike | Outcome | Entry ET | Fill | Exit | PNG | Status |",
             "|---|---|---|---|---|---|---:|---|---|---|"]
    for r in manifest:
        exit_s = r.get("exit_status") or ""
        if r.get("exit_pct") is not None and r.get("exit_status") == "hit":
            exit_s = f"{r['exit_pct']:+.0f}% @ {r.get('exit_time_et')}"
        elif r.get("exit_status") == "no_discord_exit_pct":
            exit_s = "no Discord exit %"
        elif r.get("exit_status") == "miss":
            exit_s = f"TARGET miss {r.get('exit_pct')}"
        lines.append(
            f"| {r['date']} | {r['ticker']} | {r['side']} | {r['strike']} | {r.get('outcome')} | {r.get('entry_time_et')} | {r.get('entry_fill')} | {exit_s} | {r.get('png') or ''} | {r['status']} |"
        )
    open(os.path.join(ROOT, "README.md"), "w").write("\n".join(lines) + "\n")
    print("DONE ok=", ok, "total=", len(trades))

if __name__ == "__main__":
    main()
