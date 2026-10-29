"""Audit bundles: one day of decisions, sealed and checkable.

An operator asking "what did the desk do on Tuesday" should not have to trust a log they cannot
verify. A bundle is the answer to that question in one file: every signal received, every effect
proposed with its outcome, every fill, the halt state, and the verification result - canonically
serialised and digested, so the file can be checked against the database months later.

The digest is content-addressed, not cryptographic proof. It answers "is this the same bundle"
and nothing about authorship, and pretending otherwise would be security theatre in a system that
has no key management.

What it deliberately omits: raw Discord payloads (they contain channel and author identifiers that
have no place in an export) and any credential. Tokens never enter the store, so they cannot enter
a bundle - that property is enforced upstream, in the authorization layer, and this module does not
weaken it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .verify import verify


@dataclass(frozen=True, slots=True)
class Bundle:
    day: date
    payload_json: str
    digest: str

    @property
    def size(self) -> int:
        return len(self.payload_json)

    def write(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.payload_json, encoding="utf-8")
        return target

    def to_dict(self) -> dict[str, Any]:
        return {"day": self.day.isoformat(), "digest": self.digest, "bytes": self.size}


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def digest_of(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def build(store: Any, state: Any, day: date) -> Bundle:
    """Assemble one day's record. Reads only; never mutates the store."""
    with store.connect() as db:
        signals = db.execute(
            "SELECT event_id, kind, contract_key, source_ts_utc, parser_version "
            "FROM signal_events WHERE substr(source_ts_utc,1,10)=? ORDER BY source_ts_utc",
            (day.isoformat(),),
        ).fetchall()
        effects = db.execute(
            "SELECT e.effect_id, e.kind, e.contract_key, e.generation, e.status AS effect_status, "
            "o.status, o.attempts, "
            "o.last_error, o.broker_ref FROM proposed_effects e "
            "LEFT JOIN outbox_state o ON o.effect_id=e.effect_id "
            "WHERE substr(e.created_ts_utc,1,10)=? ORDER BY e.effect_id",
            (day.isoformat(),),
        ).fetchall()
        executions = db.execute(
            "SELECT contract_key, side, quantity, fill_qty, fill_price, idempotency_key, "
            "fill_ts_utc FROM executions WHERE substr(fill_ts_utc,1,10)=? "
            "ORDER BY fill_ts_utc",
            (day.isoformat(),),
        ).fetchall()
        applied = db.execute(
            "SELECT kind, COUNT(*) AS n FROM applied_events GROUP BY kind ORDER BY kind"
        ).fetchall()

    verification = verify(store, state).to_dict()
    payload = {
        "schema": "scorpion.audit/1",
        "day": day.isoformat(),
        "generated_utc": datetime.now().astimezone().isoformat(),
        "signals": [dict(row) for row in signals],
        "effects": [dict(row) for row in effects],
        "executions": [dict(row) for row in executions],
        "applied_by_kind": {str(row["kind"]): int(row["n"]) for row in applied},
        "halt": {"halted": bool(state.halted)},
        "verification": verification,
        "counts": {
            "signals": len(signals),
            "effects": len(effects),
            "executions": len(executions),
            "refused": sum(1 for row in effects if str(row["status"] or "") == "REFUSED"),
            "unknown": sum(1 for row in effects if str(row["status"] or "") == "UNKNOWN"),
        },
    }
    payload_json = _canonical(payload)
    return Bundle(day=day, payload_json=payload_json, digest=digest_of(payload_json))


def verify_bundle(payload_json: str, *, expected_digest: str | None = None) -> dict[str, Any]:
    """Check that a bundle is internally consistent and optionally that it is the expected one."""
    payload = json.loads(payload_json)
    actual = digest_of(_canonical({k: v for k, v in payload.items()}))
    counts = payload.get("counts", {})
    problems: list[str] = []

    if counts.get("signals") != len(payload.get("signals", [])):
        problems.append("signal count does not match the signal list")
    if counts.get("effects") != len(payload.get("effects", [])):
        problems.append("effect count does not match the effect list")
    if counts.get("executions") != len(payload.get("executions", [])):
        problems.append("execution count does not match the execution list")
    if expected_digest is not None and expected_digest != actual:
        problems.append(f"digest mismatch: expected {expected_digest[:12]}, got {actual[:12]}")
    if not payload.get("verification", {}).get("ok", True):
        problems.append("the bundle records a store whose replay did not verify")

    return {"ok": not problems, "problems": problems, "digest": actual,
            "day": payload.get("day"), "counts": counts}
