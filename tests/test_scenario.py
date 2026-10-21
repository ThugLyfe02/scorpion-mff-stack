"""The vertical slice: parser -> risk -> authorization -> outbox -> paper fill -> settlement.

These tests exist because well-tested parts can still compose badly. Each one asserts a
property of the *whole* loop, and two of them are regressions for defects the slice itself
exposed:

* effects created in the same second were never due, because `created_ts` carries
  microseconds and the dispatch clock came from a truncated message timestamp;
* a batch was authorized against a book that had not been settled yet, so a close in the
  same pass as its open was refused `zero_quantity`.
"""

from datetime import UTC, datetime
from decimal import Decimal

from scorpion.config import Policy
from scorpion.outbox import Receipt
from scorpion.paper import PaperConfig, PaperSender, quote
from scorpion.scenario import demo_messages, demo_quotes, run
from scorpion.store import Store

D = Decimal
START = datetime.now(UTC).replace(microsecond=0)


def scenario(tmp_path, *, sender=None, messages=None, quotes=None, policy=None, db="s.db"):
    return run(
        store=Store(tmp_path / db),
        messages=demo_messages(START) if messages is None else messages,
        quotes=demo_quotes() if quotes is None else quotes,
        policy=policy or Policy(live_submission_enabled=True),
        sender=sender or PaperSender(),
    )


def test_the_whole_loop_composes(tmp_path):
    result = scenario(tmp_path)
    assert result.sent == 3 and result.refused == 0
    assert result.tca.n == 3
    # bought 1 contract at the ask (1.02), sold at the bid (1.00): the spread is the cost
    assert result.realized_usd == D("-2.00")
    assert result.open_positions == 1          # the SPY entry stays open


def test_a_close_in_the_same_pass_as_its_open_is_not_authorized_against_zero(tmp_path):
    """The regression: without settling on the fill hook, this refused `zero_quantity`."""
    result = scenario(tmp_path)
    details = [i.detail for r in result.dispatches for i in r.items]
    assert "zero_quantity" not in details
    assert all(i.outcome == "SENT" for r in result.dispatches for i in r.items)


def test_every_sent_order_produced_an_execution_row(tmp_path):
    store = Store(tmp_path / "s.db")
    result = run(store=store, messages=demo_messages(START), quotes=demo_quotes(),
                 policy=Policy(live_submission_enabled=True))
    rows = store.load_executions()
    assert len(rows) == result.sent == 3
    assert {r["side"] for r in rows} == {"BUY", "SELL"}


def test_the_scenario_is_deterministic(tmp_path):
    first = scenario(tmp_path, db="a.db")
    second = scenario(tmp_path, db="b.db")
    assert (first.sent, first.refused, first.realized_usd) == (
        second.sent, second.refused, second.realized_usd
    )


def test_a_paper_venue_can_slip_and_tca_sees_it(tmp_path):
    result = scenario(tmp_path, sender=PaperSender(config=PaperConfig(slippage_bps=D("50"))))
    assert result.tca.mean_cost_bps > 0, "slippage must show up as positive cost"
    assert result.realized_usd < D("-2.00"), "slippage makes the round trip worse"


def test_a_partial_fill_settles_only_what_filled(tmp_path):
    result = scenario(tmp_path, sender=PaperSender(config=PaperConfig(partial_ratio=D("0.5"))))
    assert result.tca.partial_share > 0
    assert result.open_positions == 1


def test_a_retryable_failure_is_retried_and_then_succeeds(tmp_path):
    sender = PaperSender(script=[Receipt(ok=False, error="venue timeout", retryable=True)])
    result = scenario(tmp_path, sender=sender)
    outcomes = [i.outcome for r in result.dispatches for i in r.items]
    assert "RETRY" in outcomes
    # the scenario's second pass (next message) picks the retry up and sends it
    assert result.sent >= 1


def test_a_halted_book_refuses_to_open(tmp_path):
    store = Store(tmp_path / "s.db")
    store.set_halt(True, "operator_stop")
    result = run(store=store, messages=demo_messages(START)[:1], quotes=demo_quotes(),
                 policy=Policy(live_submission_enabled=True))
    assert result.refused == 1 and result.sent == 0


def test_a_persisted_halt_is_honoured_after_a_restart(tmp_path):
    """`set_halt` writes a durable flag; a new Pipeline must resume halted, not trade."""
    store = Store(tmp_path / "s.db")
    store.set_halt(True, "operator_stop")
    assert store.halt_state() == (True, "operator_stop")
    result = run(store=store, messages=demo_messages(START)[:1], quotes=demo_quotes(),
                 policy=Policy(live_submission_enabled=True))
    assert result.halted is True and result.sent == 0


def test_a_stale_quote_refuses_honestly(tmp_path):
    def stale(key: str, at: datetime):
        return quote("1.00", "1.02", at=at, age_ms=30_000)

    result = scenario(tmp_path, messages=demo_messages(START)[:1], quotes=stale)
    assert result.refused == 1 and result.sent == 0
    assert "stale_quote" in result.dispatches[0].items[0].detail


def test_the_trail_records_every_layer(tmp_path):
    result = scenario(tmp_path)
    labels = {s.label for s in result.steps}
    assert "effects" in labels and "dispatch" in labels
    assert any(s.label.startswith("message") for s in result.steps)
    assert result.to_dict()["sent"] == 3
    assert len(result.settlements) == 3


def test_an_empty_scenario_is_clean(tmp_path):
    result = scenario(tmp_path, messages=[])
    assert result.sent == 0 and result.refused == 0 and result.tca.n == 0
