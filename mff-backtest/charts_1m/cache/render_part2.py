
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
            if r.get("interval") and r["interval"] != "5minute":
                continue
            iid = r["instrument_id"]
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
