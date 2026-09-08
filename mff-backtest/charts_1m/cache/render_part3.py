
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
    # try 5% then 10% then in-range
    for thr in (0.05, 0.10):
        for b in bars:
            c = b["close"]
            if abs(c - bid) / bid <= thr:
                fill = bid if (b["low"] <= bid <= b["high"]) else c
                return {"time": bar_dt(b), "fill": fill, "bar": b, "match": f"close_within_{int(thr*100)}pct"}
    for b in bars:
        if b["low"] <= bid <= b["high"]:
            return {"time": bar_dt(b), "fill": bid, "bar": b, "match": "bid_in_range"}
    return None

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
