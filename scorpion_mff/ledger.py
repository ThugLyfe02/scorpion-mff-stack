"""The order ledger: match an intent to its terminal outcome.

An `intent` row is the start of an order's life in the fill log; one of the
terminal events - `ack`, `reject`, `recovered`, `not_sent` - closes it. Every
one of those rows carries the same idempotency key in its `note`, so the ledger
can answer the one question a crash leaves behind: did this intent ever resolve?

A transport failure is explicitly NOT a terminal. The order may have reached the
venue after the socket died, so the listener writes a `transport_unknown`
quarantine row instead of closing the intent. That keeps the ambiguous order
visible to both `verify` and `recover`, which is the whole point: the dangerous
case is an exit the local book considers flat while the venue still holds it.

`Ledger.unresolved(rows)` is pure: it reads rows and returns the intents whose
key has no later terminal. It never writes, never calls a broker, and never
invents a terminal. The CLI's `recover` command is the only caller that turns an
unresolved intent into a terminal, and it only ever does so from a broker lookup
result (SEEN -> recovered, NOT_SEEN -> not_sent, CANNOT_SAY -> still unresolved).
`Ledger.quarantine_reasons` names which unresolved intents are venue ambiguities
rather than crash-left rows, without changing that resolution rule.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable

from .types import Fill

INTENT = "intent"
TRANSPORT_UNKNOWN = "transport_unknown"
TERMINALS = frozenset({"ack", "reject", "recovered", "not_sent"})


def key_of(fill: Fill) -> str:
    """The idempotency key a row is matched on. The intent row's note IS the key."""
    return fill.note


class Ledger:
    """Pure matching between intent rows and their terminal rows."""

    INTENT = INTENT
    TRANSPORT_UNKNOWN = TRANSPORT_UNKNOWN
    TERMINALS = TERMINALS

    @staticmethod
    def unresolved(rows: Iterable[Fill]) -> list[Fill]:
        """Return intents with no *later* terminal row sharing their key.

        Order is preserved from the input. An intent is resolved only by a
        terminal row whose sequence number is strictly greater than the intent's,
        so a duplicated or out-of-order key cannot be closed by its own past.
        Terminal rows with no preceding intent are ignored: the ledger judges
        intents, not orphaned terminals, which is what keeps a pre-ledger
        (intent-less) log clean.
        """
        rows_list = list(rows)
        terminal_seqs: dict[str, list[int]] = {}
        for fill in rows_list:
            if fill.event in TERMINALS:
                terminal_seqs.setdefault(key_of(fill), []).append(fill.seq)
        for seqs in terminal_seqs.values():
            seqs.sort()

        unresolved: list[Fill] = []
        for fill in rows_list:
            if fill.event != INTENT:
                continue
            later = terminal_seqs.get(key_of(fill))
            # bisect_right(later, fill.seq) is the count of terminals at <= fill.seq;
            # any terminal past that index is strictly later and resolves the intent.
            if later is None or bisect_right(later, fill.seq) == len(later):
                unresolved.append(fill)
        return unresolved

    @staticmethod
    def quarantine_reasons(rows: Iterable[Fill]) -> dict[str, str]:
        """key -> why the venue answer is unknown, for quarantined intents.

        A transport failure leaves the intent unresolved by design. This map lets
        `verify` say *which* unresolved intents are venue ambiguities (a named
        transport failure) instead of unexplained crash-left rows, without changing
        the resolution rule: both kinds stay unresolved until a lookup decides.
        The last recorded reason wins, so a retried probe's freshest error is shown.
        """
        reasons: dict[str, str] = {}
        for fill in rows:
            if fill.event == TRANSPORT_UNKNOWN:
                reasons[fill.note] = fill.reason
        return reasons
