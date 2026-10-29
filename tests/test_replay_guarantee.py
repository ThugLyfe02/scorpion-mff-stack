"""The replay guarantee, held by test rather than by hope.

`verify` is the only component that can tell an operator the book has drifted from the decisions
that produced it. These tests exist so the guarantee is enforced at corpus scale, not just on a
three-message demo - the defect they were written after (a sweep that mutated the book outside
the event stream) was invisible to every reducer-level test and only appeared when a second
consumer rebuilt the book from the log and disagreed.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from scorpion.config import Policy
from scorpion.corpus_replay import report
from scorpion.ledger import load_csv
from scorpion.outbox import Dispatcher, Receipt
from scorpion.paper import quote
from scorpion.pipeline import Pipeline
from scorpion.scenario import message
from scorpion.store import Store
from scorpion.verify import verify

START = datetime.now(UTC).replace(microsecond=0)


class Ok:
    def send(self, envelope, *, now):  # noqa: ANN001
        return Receipt(ok=True, broker_ref="ord-1", fill_price=envelope.limit_price,
                       fill_qty=1)


def test_a_release_the_reducer_performed_is_written_to_the_log(tmp_path):
    """A position closed by the sweep must appear in the applied log, or replay cannot see it."""
    store = Store(tmp_path / "s.db")
    pipeline = Pipeline(store=store)

    asyncio.run(pipeline.handle(message("QQQ 719C TODAY @ 1.01", message_id="m1", at=START)))
    asyncio.run(pipeline.handle(
        message("SPY 600P TODAY @ 0.90", message_id="m2", at=START + timedelta(days=1))
    ))

    kinds = store.applied_counts()
    assert kinds.get("expire", 0) >= 1, "the day roll released a 0DTE position"
    result = verify(store, pipeline.state)
    assert result.ok, [d.describe() for d in result.divergences]


def test_an_expire_row_frees_quantity_as_well_as_the_slot(tmp_path):
    """A released position must not keep its quantity: that quantity is what blocks a day roll."""
    store = Store(tmp_path / "s.db")
    pipeline = Pipeline(store=store)
    asyncio.run(pipeline.handle(message("QQQ 719C TODAY @ 1.01", message_id="m1", at=START)))
    t = START + timedelta(seconds=1)
    key = next(iter(pipeline.state.positions))
    Dispatcher(store=store, sender=Ok(), policy=Policy(live_submission_enabled=True)).dispatch_once(
        state=pipeline.state, now=t, quotes={key: quote("1.00", "1.02", at=t)}
    )
    pipeline.settle(contract_key=key, generation=1, quantity_delta=1, fill_price=D("1.02"),
                    market_day=START.date())
    assert pipeline.state.positions[key].quantity >= 1

    asyncio.run(pipeline.handle(
        message("SPY 600P TODAY @ 0.90", message_id="m2", at=START + timedelta(days=1))
    ))
    released = pipeline.state.positions.get(key)
    assert released is None or released.quantity == 0
    assert verify(store, pipeline.state).ok


@pytest.mark.parametrize("corpus", ["mff-backtest/trades.csv"])
def test_the_corpus_replay_is_verified_end_to_end(tmp_path, corpus):
    """The milestone: 107 trades through the production stack, and the log explains the book."""
    out = report(load_csv(corpus), db_path=tmp_path / "r.db")
    summary, verification = out["summary"], out["verification"]

    assert summary["corpus"] == 107
    assert summary["taken"] == 101, "coverage drift means a rule or a parser changed"
    assert summary["contracts"] == 373
    assert verification["ok"] is True, verification["divergences"]
    assert verification["divergences"] == []
    assert verification["signals"] == 107
    assert verification["fills"] > 150
