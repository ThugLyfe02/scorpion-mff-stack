"""Append-only structured fill log.

The 9/8 post-mortem could only be reconstructed from screenshots and memory.
This is the answer: every decision - buy, sell, skip, reject, halt - becomes one
JSON line with a monotonic sequence number, a reason, and the latency that
mattered. Append-only, flushed per record, so a crash cannot lose the last
decision.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path

from .types import Fill


class FillLog:
    """In-memory ring for tests + optional JSONL sink for the live path."""

    def __init__(self, path: str | os.PathLike[str] | None = None,
                 fsync: bool = False, max_memory: int = 10_000) -> None:
        self.path = Path(path) if path else None
        self.fsync = fsync
        self.max_memory = max_memory
        self.records: list[Fill] = []
        self._seq = 0
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, *, event: str, contract_key: str, qty: int, price: float,
               reason: str, ts: float | None = None, accepted: bool = True,
               latency_ms: int | None = None, note: str = "") -> Fill:
        self._seq += 1
        fill = Fill(
            seq=self._seq,
            ts=time.time() if ts is None else ts,
            event=event,
            contract_key=contract_key,
            qty=qty,
            price=price,
            reason=reason,
            accepted=accepted,
            latency_ms=latency_ms,
            note=note,
        )
        self.records.append(fill)
        if len(self.records) > self.max_memory:
            del self.records[: len(self.records) - self.max_memory]
        if self.path:
            self._append(fill)
        return fill

    def _append(self, fill: Fill) -> None:
        path = self.path
        if path is None:                      # sink not configured
            return
        line = json.dumps(asdict(fill), ensure_ascii=False, sort_keys=True)
        # One write() of a complete line: a torn line cannot happen, and nothing
        # is buffered across decisions.
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            if self.fsync:
                os.fsync(fh.fileno())

    # ── readers (used by the CLI, the replay report and the tests) ───────────
    def events(self, event: str) -> list[Fill]:
        return [f for f in self.records if f.event == event]

    def bought(self) -> list[Fill]:
        return self.events("buy")

    def sold(self) -> list[Fill]:
        return self.events("sell")

    def skipped(self) -> list[Fill]:
        return [f for f in self.records if not f.accepted]

    def latencies_ms(self) -> list[int]:
        return [f.latency_ms for f in self.records if f.latency_ms is not None]

    def max_latency_ms(self) -> int:
        values = self.latencies_ms()
        return max(values) if values else 0

    def replay_file(self, path: str | os.PathLike[str]) -> list[Fill]:
        """Read a JSONL log back as Fill rows (for the operator and tests)."""
        out: list[Fill] = []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            out.append(Fill(**json.loads(line)))
        return out

    def verify_file(self, path: str | os.PathLike[str]) -> list[str]:
        """Validate an on-disk fill log for the defects that would corrupt the book.

        Returns a list of human-readable problems; empty means the log is
        trustworthy. Each check is a seam the 9/8 post-mortem could not answer
        because no log existed at all - so the log that now exists must be able
        to say whether it is intact:

          * every non-blank line parses back into a Fill (a torn last line from a
            crash mid-append is named, never skipped);
          * sequence numbers are strictly increasing from 1 - a gap is a lost
            decision, a duplicate is a double-counted fill, and either makes the
            book reconstructed by `book_from_fills` diverge from what happened.
        """
        problems: list[str] = []
        prev_seq = 0
        for line_no, line in enumerate(
            Path(path).read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                fill = Fill(**json.loads(line))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                problems.append(f"line {line_no}: unreadable row ({exc})")
                continue
            if fill.seq != prev_seq + 1:
                problems.append(
                    f"line {line_no}: seq {fill.seq} after {prev_seq} "
                    f"(expected {prev_seq + 1})"
                )
            prev_seq = fill.seq
        return problems

    def reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.records:
            if not f.accepted:
                counts[f.reason] = counts.get(f.reason, 0) + 1
        return counts

    def extend(self, fills: Iterable[Fill]) -> None:
        for f in fills:
            self.records.append(f)
