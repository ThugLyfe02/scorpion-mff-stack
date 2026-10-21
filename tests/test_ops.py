"""Supervision: liveness from the log, and the two refusals that protect a stopped desk."""

import asyncio
from datetime import UTC, datetime, timedelta

from scorpion.ops import Metrics, assess, collect, liveness, plan, render
from scorpion.pipeline import Pipeline
from scorpion.scenario import message
from scorpion.store import Store

START = datetime.now(UTC).replace(microsecond=0)


def seeded(tmp_path):
    store = Store(tmp_path / "s.db")
    pipeline = Pipeline(store=store)
    asyncio.run(pipeline.handle(message("QQQ 719C TODAY @ 1.01", message_id="m1", at=START)))
    return store, pipeline


def test_an_empty_log_is_down_not_ok(tmp_path):
    store = Store(tmp_path / "s.db")
    assert liveness(store).status == "down"
    assert assess(store).exit_code == 1


def test_a_recently_applied_log_is_ok(tmp_path):
    store, _ = seeded(tmp_path)
    state = liveness(store, now=START + timedelta(seconds=5))
    assert state.status == "ok" and state.rows >= 2


def test_an_idle_log_is_stale(tmp_path):
    store, _ = seeded(tmp_path)
    state = liveness(store, now=START + timedelta(hours=2))
    assert state.status == "stale"
    assert assess(store, now=START + timedelta(hours=2)).exit_code == 1


def test_a_halt_is_refused_before_anything_else(tmp_path):
    store, pipeline = seeded(tmp_path)
    pipeline.halt("drawdown")
    decision = plan(store, now=START + timedelta(hours=5))
    assert decision.action == "refuse_halted"
    assert "drawdown" in decision.detail


def test_unknown_sends_block_resumption(tmp_path):
    """A restart with an unresolved send is how one order becomes two."""
    from scorpion.config import Policy
    from scorpion.outbox import Dispatcher
    from scorpion.paper import quote

    store, pipeline = seeded(tmp_path)

    class Boom:
        def send(self, envelope, *, now):
            raise TimeoutError("no answer")

    now = datetime.now(UTC) + timedelta(seconds=1)   # effects are stamped at wall clock
    key = next(iter(pipeline.state.positions))
    Dispatcher(store=store, sender=Boom(), policy=Policy(live_submission_enabled=True)
               ).dispatch_once(state=pipeline.state, now=now,
                               quotes={key: quote("1.00", "1.02", at=now)})
    assert plan(store, now=now).action == "refuse_unknown"


def test_restart_is_proposed_for_a_stale_log(tmp_path):
    store, _ = seeded(tmp_path)
    assert plan(store, now=START + timedelta(hours=3)).action == "restart"


def test_metrics_count_applied_kinds_and_halt(tmp_path):
    store, pipeline = seeded(tmp_path)
    pipeline.halt("test")
    metrics = collect(store)
    assert metrics.gauges["applied{kind=signal}"] == 1
    assert metrics.gauges["halted"] == 1
    assert metrics.gauges["applied_rows"] >= 2


def test_percentiles_over_observations():
    m = Metrics()
    for value in (10, 20, 30, 40, 50):
        m.observe("dispatch_ms", value)
    assert m.percentile("dispatch_ms", 0.5) == 30
    assert m.percentile("dispatch_ms", 0.95) is not None
    assert m.percentile("absent", 0.5) is None


def test_the_render_is_operator_readable(tmp_path):
    store, _ = seeded(tmp_path)
    from scorpion.ops import report
    text = render(report(store, now=START + timedelta(seconds=2)))
    assert "liveness" in text and "exit" in text
