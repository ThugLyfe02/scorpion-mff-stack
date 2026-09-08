
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
