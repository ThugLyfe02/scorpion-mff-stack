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
]
# Also scan for any other 5m results written today that contain "5minute"
EXTRA_GLOB = [
  "c7926c23-b6a5-4bfe-b1be-1f03583a1ceb.txt",  # may be minute junk
]
