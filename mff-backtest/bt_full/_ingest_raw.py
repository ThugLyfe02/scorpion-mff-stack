"""Ingest MCP option historical dumps from /workspace/agent-tools into bt_full/bars/."""
import json, os, re, glob
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
RAW = Path("/workspace/agent-tools")
OUT = Path("/workspace/mff-backtest/bt_full/bars")
OUT.mkdir(parents=True, exist_ok=True)
SEEN = Path("/workspace/mff-backtest/bt_full/ingested_raw.txt")
seen = set(SEEN.read_text().splitlines()) if SEEN.exists() else set()

def parse_file(path):
    text = open(path, encoding="utf-8", errors="ignore").read()
    # strip custom instructions preamble if present
    i = text.find('{"data"')
    if i < 0:
        i = text.find("{")
    j = text.rfind("}")
    if i < 0 or j < 0:
        return []
    try:
        obj = json.loads(text[i:j+1])
    except Exception:
        return []
    return obj.get("data", {}).get("results", []) or []

def is_rth(begins_at):
    dt = datetime.fromisoformat(begins_at.replace("Z","+00:00")).astimezone(ET)
    if dt.weekday() >= 5:
        return False
    t = dt.hour*60 + dt.minute
    return 9*60+30 <= t < 16*60

n_files=0; n_inst=0; n_real=0
for path in sorted(RAW.glob("*.txt")):
    p = str(path)
    if p in seen:
        continue
    results = parse_file(path)
    if not results:
        continue
    # only ingest if looks like option historicals
    if not isinstance(results, list) or not results or "bars" not in results[0]:
        continue
    n_files += 1
    for r in results:
        iid = r.get("instrument_id")
        if not iid:
            continue
        bars = []
        for b in r.get("bars") or []:
            if b.get("interpolated"):
                continue
            ba = b.get("begins_at")
            if not ba or not is_rth(ba):
                continue
            bars.append({
                "begins_at": ba,
                "open": float(b["open_price"]),
                "high": float(b["high_price"]),
                "low": float(b["low_price"]),
                "close": float(b["close_price"]),
            })
        # merge with existing
        outp = OUT / ("%s.json" % iid)
        existing = []
        if outp.exists():
            existing = json.load(open(outp)).get("bars", [])
        by_t = {b["begins_at"]: b for b in existing}
        for b in bars:
            by_t[b["begins_at"]] = b
        merged = [by_t[k] for k in sorted(by_t)]
        json.dump({"instrument_id": iid, "interval": "5minute", "bars": merged}, open(outp,"w"))
        n_inst += 1
        n_real += len(bars)
    seen.add(p)

SEEN.write_text("\n".join(sorted(seen))+"\n")
# summary
files = list(OUT.glob("*.json"))
with_bars = 0
empty = 0
for f in files:
    d=json.load(open(f))
    if d.get("bars"): with_bars += 1
    else: empty += 1
print("ingested_files", n_files, "results", n_inst, "real_rth_bars_added", n_real)
print("bar_files", len(files), "with_bars", with_bars, "empty", empty)
