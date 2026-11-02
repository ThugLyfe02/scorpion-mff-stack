#!/usr/bin/env python3
"""Regenerate tests/fixtures/replay_fixture.json: synthetic (redistributable) calls + 1-min bars.

Prices are a seeded random walk, not market data. Run from the repo root; then regenerate the
goldens with scripts/make_replay_golden.sh (needs the frozen research logger for the v2.1 file).
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SYMBOLS = {"AAA": 7.40, "BBB": 15.20, "CCC": 81.30, "SPY": 640.0, "QQQ": 600.0, "THIN": 4.0}
DATES = ("2026-10-29", "2026-10-30")


def bars_for(symbol: str, start: float, rng: random.Random) -> list[list[object]]:
    out: list[list[object]] = []
    price = start
    for day in DATES:
        y, m, d = (int(x) for x in day.split("-"))
        t = datetime(y, m, d, 9, 0, tzinfo=ET)
        end = datetime(y, m, d, 16, 30, tzinfo=ET)
        drift = rng.choice((-1, 1)) * 0.0004
        while t < end:
            if symbol == "THIN" and not (t.minute % 3 == 0):
                t += timedelta(minutes=1)
                continue
            if t.minute % 37 == 0:
                drift = rng.choice((-1, 1)) * rng.uniform(0.0002, 0.0012)
            step = price * (drift + rng.gauss(0, 0.0015))
            o = round(price, 4)
            c = round(max(0.5, price + step), 4)
            hi = round(max(o, c) * (1 + abs(rng.gauss(0, 0.0008))), 4)
            lo = round(min(o, c) * (1 - abs(rng.gauss(0, 0.0008))), 4)
            out.append([t.isoformat(), o, hi, lo, c])
            price = c
            t += timedelta(minutes=1)
    return out


def main() -> None:
    rng = random.Random(20261102)
    bars = {s: bars_for(s, p, rng) for s, p in SYMBOLS.items()}
    ok = "options-king"
    calls = [
        {"channel": ok, "date": "2026-10-29", "time_et": "09:41", "ticker": "AAA", "side": "call",
         "strike": "7.5", "expiry": "2026-10-31"},
        {"channel": ok, "date": "2026-10-29", "time_et": "13:05", "ticker": "CCC", "side": "call",
         "strike": "85", "expiry": "2026-11-06"},
        {"channel": ok, "date": "2026-10-30", "time_et": "09:36", "ticker": "AAA", "side": "call",
         "strike": "7.5", "expiry": "2026-11-06"},
        {"channel": ok, "date": "2026-10-30", "time_et": "09:52", "ticker": "BBB", "side": "call",
         "strike": "16", "expiry": "2026-11-06"},
        {"channel": ok, "date": "2026-10-30", "time_et": "10:14", "ticker": "CCC", "side": "call",
         "strike": "82", "expiry": "2026-11-06", "seen_time_et": "10:20"},
        {"channel": ok, "date": "2026-10-30", "time_et": "10:31", "ticker": "AAA", "side": "call",
         "strike": "8", "expiry": "2026-11-06", "actual_fill": "7.61"},
        {"channel": ok, "date": "2026-10-30", "time_et": "10:45", "ticker": "CCC", "side": "call",
         "strike": "83", "expiry": "2026-11-06", "seen_time_et": "11:05"},
        {"channel": ok, "date": "2026-10-30", "time_et": "11:20", "ticker": "BBB", "side": "call",
         "strike": "16", "expiry": "2026-11-06"},
        {"channel": ok, "date": "2026-10-30", "time_et": "12:02", "ticker": "SPY", "side": "call",
         "strike": "645", "expiry": "2026-10-30"},
        {"channel": ok, "date": "2026-10-30", "time_et": "12:40", "ticker": "CCC", "side": "put",
         "strike": "80", "expiry": "2026-11-06"},
        {"channel": ok, "date": "2026-10-30", "time_et": "13:15", "ticker": "THIN", "side": "call",
         "strike": "4", "expiry": "2026-11-06"},
        {"channel": ok, "date": "2026-10-30", "time_et": "13:30", "ticker": "ZZZ", "side": "call",
         "strike": "10", "expiry": "2026-11-06"},
        {"channel": ok, "date": "2026-10-30", "time_et": "14:58", "ticker": "BBB", "side": "call",
         "strike": "16", "expiry": "2026-11-06"},
        {"channel": ok, "date": "2026-10-30", "time_et": "14:59", "ticker": "CCC", "side": "call",
         "strike": "84", "expiry": "2026-11-06"},
        {"channel": ok, "date": "2026-10-30", "time_et": "15:20", "ticker": "AAA", "side": "call",
         "strike": "8", "expiry": "2026-11-06"},
        {"channel": "lotto-friday", "date": "2026-10-30", "time_et": "10:00", "ticker": "QQQ",
         "side": "call", "strike": "600", "expiry": "2026-10-30"},
    ]
    payload = {
        "note": "SYNTHETIC seeded random-walk bars and invented calls; not market or Discord data.",
        "calls": calls,
        "bars": bars,
    }
    with open("tests/fixtures/replay_fixture.json", "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))
        handle.write("\n")


if __name__ == "__main__":
    main()
