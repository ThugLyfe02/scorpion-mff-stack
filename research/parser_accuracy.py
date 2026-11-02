#!/usr/bin/env python3
"""Parser accuracy on the private Options King callout history (research box only; not committed
data). Compares parse_message() with the scrape's structured columns for every 'callout' row.

  PYTHONPATH=src python research/parser_accuracy.py /workspace/discord-history/calls_options-king.csv
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from scorpion.config import CHANNELS, GUILD_ID
from scorpion.domain import RawDiscordMessage
from scorpion.parser import parse_message

ET = ZoneInfo("America/New_York")


def dec(value: str) -> Decimal | None:
    try:
        return Decimal(value.strip().lstrip("$")) if value.strip() else None
    except InvalidOperation:
        return None


def main(path: str, since: str = "") -> None:
    with open(path, newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r["outcome"] == "callout" and r["date"] >= since]
    tally: Counter[str] = Counter()
    by_year: dict[str, Counter[str]] = {}
    for r in rows:
        y, m, d = (int(x) for x in r["date"].split("-"))
        hh, mm = (int(x) for x in r["time_et"].split(":"))
        ts = datetime(y, m, d, hh, mm, tzinfo=ET)
        raw = RawDiscordMessage("1", GUILD_ID, CHANNELS["options-king"], "a", r["raw_text"], ts, ts)
        e = parse_message(raw)
        side = "CALL" if r["side"].upper().startswith("C") else "PUT"
        entry = e.kind.value == "ENTRY"
        exact = (
            entry and e.ticker == r["ticker"] and e.option_side == side
            and e.strike == dec(r["strike"])
            and (e.expiry.isoformat() if e.expiry else "") == r["expiry"]
            and e.referenced_price == dec(r["entry_price"])
        )
        yr = by_year.setdefault(r["date"][:4], Counter())
        for c in (tally, yr):
            c["callouts"] += 1
            c["entry"] += entry
            c["ticker_side_ok"] += entry and e.ticker == r["ticker"] and e.option_side == side
            c["all_fields_exact"] += exact
            c["kind_" + e.kind.value] += 1
    print(json.dumps({"all": tally, "by_year": by_year}, indent=1, sort_keys=True))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "")
