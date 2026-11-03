"""M2: time-in-force, the working-order state machine, the durable order registry,
the stale-limit sweep, and the broker's cancel/replace/status/open-orders surface.

Each test pins a legal transition, a refused transition (with its named reason), or a
venue payload/behaviour. The asymmetry that matters is tested explicitly: a stale
**entry** may be auto-cancelled, a stale **exit** is only ever paged.
"""

from __future__ import annotations

import json

import pytest

from scorpion_mff.broker import (
    BrokerError,
    PaperBroker,
    RobinhoodAgenticBroker,
    TransportError,
)
from scorpion_mff.orders import (
    BAD_FILLED_QTY,
    BAD_LIMIT,
    BAD_QUANTITY,
    BAD_SIDE,
    BAD_TIF,
    BAD_TIMESTAMP,
    CANCEL_FAILED,
    CANCEL_UNAVAILABLE,
    EXIT_UNFILLED_PAGE,
    STALE_ENTRY,
    IllegalTransition,
    OrderError,
    OrderLifecycle,
    OrderRegistry,
    OrderRegistryError,
    OrderState,
    StaleLimitGuard,
    SweepKind,
    TimeInForce,
    WorkingOrder,
    apply_event,
    render_registry,
    render_registry_verify,
    verify_orders_file,
)
from scorpion_mff.types import OrderIntent

T0 = 1_757_600_000.0
CONTRACT = "QQQ|CALL|719|2026-09-11"


def order(**overrides) -> WorkingOrder:
    base = dict(
        client_order_id="cid-1",
        contract=CONTRACT,
        side="buy",
        qty=5,
        limit=1.05,
        created_at=T0,
        updated_at=T0,
    )
    base.update(overrides)
    return WorkingOrder(**base)


def working(**overrides) -> WorkingOrder:
    """An order the venue has acknowledged."""
    base = dict(state=OrderState.WORKING, venue_order_id="rh-1", updated_at=T0 + 1)
    base.update(overrides)
    return order(**base)


def intent(**overrides) -> OrderIntent:
    base = dict(action="buy", qty=2, limit=1.06, contract_key=CONTRACT,
                reason="entry:standard", idempotency_key="cid-1:buy:1")
    base.update(overrides)
    return OrderIntent(**base)


# ── construction and derived answers ────────────────────────────────────────
def test_a_fresh_order_is_pending_and_not_working():
    fresh = order()
    assert fresh.state is OrderState.PENDING
    assert fresh.active and not fresh.working and not fresh.terminal
    assert fresh.remaining == 5
    assert fresh.is_entry and not fresh.is_exit


@pytest.mark.parametrize("overrides, reason", [
    ({"client_order_id": ""}, "bad_client_order_id"),
    ({"contract": ""}, "bad_contract"),
    ({"side": "hold"}, BAD_SIDE),
    ({"qty": 0}, BAD_QUANTITY),
    ({"qty": 2.5}, BAD_QUANTITY),
    ({"qty": True}, BAD_QUANTITY),
    ({"limit": 0}, BAD_LIMIT),
    ({"limit": float("nan")}, BAD_LIMIT),
    ({"filled_qty": 6}, BAD_FILLED_QTY),
    ({"filled_qty": -1}, BAD_FILLED_QTY),
    ({"tif": "weekly"}, BAD_TIF),
    ({"state": "limbo"}, "bad_state"),
    ({"updated_at": T0 - 10}, BAD_TIMESTAMP),
    ({"created_at": -1}, BAD_TIMESTAMP),
])
def test_construction_refuses_a_bad_order(overrides, reason):
    with pytest.raises(OrderError) as caught:
        order(**overrides)
    assert caught.value.reason == reason


def test_construction_accepts_strings_for_tif_and_state():
    parsed = order(tif="DAY", state="WORKING", venue_order_id="rh-1")
    assert parsed.tif is TimeInForce.DAY
    assert parsed.state is OrderState.WORKING


def test_construction_stamps_updated_at_from_created_at_when_omitted():
    fresh = WorkingOrder(client_order_id="cid-9", contract=CONTRACT, side="buy",
                         qty=1, limit=1.0, created_at=T0)
    assert fresh.updated_at == T0
    assert fresh.age(T0 + 60) == 60


def test_pending_quantity_cannot_be_below_the_filled_quantity():
    with pytest.raises(OrderError) as caught:
        order(filled_qty=3, pending_qty=2, state=OrderState.PARTIAL)
    assert caught.value.reason == "bad_pending"


# ── legal transitions ────────────────────────────────────────────────────────
def test_ack_moves_pending_to_working_and_keeps_the_venue_id():
    updated = apply_event(order(), "ack", ts=T0 + 1, venue_order_id="rh-1")
    assert updated.state is OrderState.WORKING and updated.venue_order_id == "rh-1"
    assert updated.reason == "acknowledged"


def test_ack_requires_a_venue_order_id():
    with pytest.raises(OrderError) as caught:
        apply_event(order(), "ack", ts=T0 + 1)
    assert caught.value.reason == "missing_venue_order_id"


def test_partial_fills_accumulate_and_a_final_fill_completes():
    first = apply_event(working(), "partial", ts=T0 + 2, filled_qty=2)
    assert first.state is OrderState.PARTIAL and first.filled_qty == 2
    second = apply_event(first, "partial", ts=T0 + 3, filled_qty=4)
    assert second.state is OrderState.PARTIAL and second.filled_qty == 4
    done = apply_event(second, "fill", ts=T0 + 4)
    assert done.state is OrderState.FILLED and done.filled_qty == 5
    assert done.remaining == 0


def test_fill_accepts_an_explicit_cumulative_quantity():
    done = apply_event(working(), "fill", ts=T0 + 2, filled_qty=5)
    assert done.state is OrderState.FILLED and done.filled_qty == 5


def test_a_repeat_partial_is_a_named_duplicate():
    partial = apply_event(working(), "partial", ts=T0 + 2, filled_qty=2)
    with pytest.raises(OrderError) as caught:
        apply_event(partial, "partial", ts=T0 + 3, filled_qty=2)
    assert caught.value.reason == "duplicate_fill"


def test_a_regressing_fill_is_refused():
    partial = apply_event(working(), "partial", ts=T0 + 2, filled_qty=3)
    with pytest.raises(OrderError) as caught:
        apply_event(partial, "partial", ts=T0 + 3, filled_qty=2)
    assert caught.value.reason == "regressing_fill"


def test_a_fill_larger_than_the_order_is_refused():
    with pytest.raises(OrderError) as caught:
        apply_event(working(), "partial", ts=T0 + 2, filled_qty=6)
    assert caught.value.reason == "fill_exceeds_qty"


def test_a_partial_that_fills_the_order_is_refused_by_name():
    with pytest.raises(OrderError) as caught:
        apply_event(working(), "partial", ts=T0 + 2, filled_qty=5)
    assert caught.value.reason == "partial_fills_the_order"


def test_a_short_fill_is_refused_and_names_the_alternative():
    with pytest.raises(OrderError) as caught:
        apply_event(working(), "fill", ts=T0 + 2, filled_qty=2)
    assert caught.value.reason == "fill_short_of_qty"


def test_a_late_reject_is_terminal_but_keeps_the_filled_quantity():
    partial = apply_event(working(), "partial", ts=T0 + 2, filled_qty=2)
    rejected = apply_event(partial, "reject", ts=T0 + 3)
    assert rejected.state is OrderState.REJECTED and rejected.filled_qty == 2


@pytest.mark.parametrize("terminal", [
    OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED,
])
def test_a_terminal_state_is_terminal(terminal):
    terminal_order = order(state=terminal)
    with pytest.raises(OrderError) as caught:
        apply_event(terminal_order, "ack", ts=T0 + 2, venue_order_id="rh-9")
    assert caught.value.reason == "terminal_state"


# ── cancel ───────────────────────────────────────────────────────────────────
def test_cancel_requested_records_the_request_without_confirming_it():
    requested = apply_event(working(), "cancel_requested", ts=T0 + 2)
    assert requested.state is OrderState.WORKING and requested.cancel_requested
    assert requested.reason == "cancel_requested"


def test_a_second_cancel_request_is_refused():
    requested = apply_event(working(), "cancel_requested", ts=T0 + 2)
    with pytest.raises(OrderError) as caught:
        apply_event(requested, "cancel_requested", ts=T0 + 3)
    assert caught.value.reason == "duplicate_cancel_request"


def test_cancel_confirmed_is_terminal_and_keeps_the_fill():
    partial = apply_event(working(), "partial", ts=T0 + 2, filled_qty=2)
    cancelled = apply_event(partial, "cancel_confirmed", ts=T0 + 3)
    assert cancelled.state is OrderState.CANCELLED and cancelled.filled_qty == 2
    assert not cancelled.cancel_requested


# ── replace ──────────────────────────────────────────────────────────────────
def test_replace_requested_does_not_move_the_limit_until_confirmed():
    requested = apply_event(working(), "replace_requested", ts=T0 + 2, limit=1.25)
    assert requested.limit == 1.05 and requested.pending_limit == 1.25
    confirmed = apply_event(requested, "replace_confirmed", ts=T0 + 3)
    assert confirmed.limit == 1.25 and confirmed.pending_limit is None
    assert confirmed.state is OrderState.WORKING


def test_replace_can_change_quantity_below_the_original():
    requested = apply_event(working(), "replace_requested", ts=T0 + 2, qty=3)
    confirmed = apply_event(requested, "replace_confirmed", ts=T0 + 3)
    assert confirmed.qty == 3 and confirmed.remaining == 3


def test_replace_is_refused_on_an_order_the_venue_has_not_acked():
    with pytest.raises(OrderError) as caught:
        apply_event(order(), "replace_requested", ts=T0 + 2, limit=1.5)
    assert caught.value.reason == "illegal_replace_requested_from_pending"


def test_replace_confirmed_without_a_request_is_refused():
    with pytest.raises(OrderError) as caught:
        apply_event(working(), "replace_confirmed", ts=T0 + 2)
    assert caught.value.reason == "no_pending_replace"


def test_replace_below_the_filled_quantity_is_refused():
    partial = apply_event(working(), "partial", ts=T0 + 2, filled_qty=3)
    with pytest.raises(OrderError) as caught:
        apply_event(partial, "replace_requested", ts=T0 + 3, qty=2)
    assert caught.value.reason == "replace_below_filled"


def test_a_replace_that_changes_nothing_is_refused():
    with pytest.raises(OrderError) as caught:
        apply_event(working(), "replace_requested", ts=T0 + 2, limit=1.05)
    assert caught.value.reason == "replace_changes_nothing"


def test_replace_after_cancel_request_is_refused():
    requested = apply_event(working(), "cancel_requested", ts=T0 + 2)
    with pytest.raises(OrderError) as caught:
        apply_event(requested, "replace_requested", ts=T0 + 3, limit=1.5)
    assert caught.value.reason == "replace_after_cancel"


def test_replace_on_a_partial_order_keeps_it_partial():
    partial = apply_event(working(), "partial", ts=T0 + 2, filled_qty=2)
    requested = apply_event(partial, "replace_requested", ts=T0 + 3, limit=1.5)
    confirmed = apply_event(requested, "replace_confirmed", ts=T0 + 4)
    assert confirmed.state is OrderState.PARTIAL and confirmed.filled_qty == 2


# ── reject / expire / timeout ───────────────────────────────────────────────
def test_a_pending_send_can_be_rejected():
    rejected = apply_event(order(), "reject", ts=T0 + 2, reason="buying_power")
    assert rejected.state is OrderState.REJECTED and rejected.reason == "buying_power"


def test_a_working_order_expires_only_by_name():
    expired = apply_event(working(), "expire", ts=T0 + 2)
    assert expired.state is OrderState.EXPIRED


def test_timeout_turns_a_pending_send_into_unknown_not_a_reject():
    timed_out = apply_event(order(), "timeout", ts=T0 + 2)
    assert timed_out.state is OrderState.UNKNOWN and timed_out.active
    assert timed_out.reason == "timeout"


def test_a_late_ack_resolves_an_unknown_send():
    timed_out = apply_event(order(), "timeout", ts=T0 + 2)
    acked = apply_event(timed_out, "ack", ts=T0 + 3, venue_order_id="rh-7")
    assert acked.state is OrderState.WORKING and acked.venue_order_id == "rh-7"


def test_a_late_ack_cannot_rename_the_venue_order():
    timed_out = order(state=OrderState.UNKNOWN, venue_order_id="rh-7", updated_at=T0 + 2)
    with pytest.raises(OrderError) as caught:
        apply_event(timed_out, "ack", ts=T0 + 3, venue_order_id="rh-8")
    assert caught.value.reason == "venue_order_id_mismatch"


def test_timeout_on_a_working_order_is_refused():
    with pytest.raises(OrderError) as caught:
        apply_event(working(), "timeout", ts=T0 + 2)
    assert caught.value.reason == "illegal_timeout_from_working"


def test_an_event_before_the_order_existed_is_a_timestamp_refusal():
    with pytest.raises(OrderError) as caught:
        apply_event(order(), "ack", ts=T0 - 1, venue_order_id="rh-1")
    assert caught.value.reason == BAD_TIMESTAMP


def test_an_unknown_event_name_is_refused():
    with pytest.raises(OrderError) as caught:
        apply_event(order(), "teleport", ts=T0 + 1)
    assert caught.value.reason == "bad_event"


# ── OrderLifecycle: tracking and recording ──────────────────────────────────
def test_lifecycle_tracks_an_order_once():
    lifecycle = OrderLifecycle()
    tracked = lifecycle.track(order(), ts=T0)
    assert lifecycle.get("cid-1") is tracked
    with pytest.raises(OrderError) as caught:
        lifecycle.track(order(), ts=T0 + 1)
    assert caught.value.reason == "duplicate_client_order_id"


def test_lifecycle_refuses_an_unknown_order():
    with pytest.raises(OrderError) as caught:
        OrderLifecycle().ack("nope", "rh-1", ts=T0 + 1)
    assert caught.value.reason == "unknown_order"


def test_lifecycle_records_every_transition_in_the_registry(tmp_path):
    path = tmp_path / "orders.jsonl"
    lifecycle = OrderLifecycle(registry=OrderRegistry(path))
    lifecycle.track(order(), ts=T0)
    lifecycle.ack("cid-1", "rh-1", ts=T0 + 1)
    lifecycle.partial("cid-1", 2, ts=T0 + 2)

    replayed = OrderRegistry.load(path)
    assert replayed.seq == 3
    restored = replayed.get("cid-1")
    assert restored is not None
    assert restored.state is OrderState.PARTIAL and restored.filled_qty == 2
    assert restored.venue_order_id == "rh-1"
    assert replayed.torn_lines == 0


# ── stale-limit sweep ────────────────────────────────────────────────────────
def test_a_stale_entry_is_cancelled_when_the_policy_allows_it():
    cancelled: list[str] = []
    lifecycle = OrderLifecycle(
        guard=StaleLimitGuard(max_working_seconds=300, cancel_stale=True),
        cancel_order=cancelled.append)
    lifecycle.track(order(state=OrderState.PENDING, updated_at=T0), ts=T0)
    lifecycle.ack("cid-1", "rh-1", ts=T0 + 1)

    actions = lifecycle.sweep(T0 + 301)
    assert [action.kind for action in actions] == [SweepKind.CANCEL]
    assert actions[0].reason == STALE_ENTRY
    assert cancelled == ["rh-1"]
    tracked = lifecycle.get("cid-1")
    assert tracked is not None and tracked.cancel_requested
    assert tracked.state is OrderState.WORKING


def test_a_stale_entry_only_pages_when_auto_cancel_is_off():
    lifecycle = OrderLifecycle(
        guard=StaleLimitGuard(max_working_seconds=300, cancel_stale=False),
        cancel_order=lambda _order_id: pytest.fail("must not cancel"))
    lifecycle.track(order(updated_at=T0), ts=T0)
    lifecycle.ack("cid-1", "rh-1", ts=T0 + 1)
    actions = lifecycle.sweep(T0 + 301)
    assert [action.kind for action in actions] == [SweepKind.PAGE]
    assert actions[0].reason == STALE_ENTRY


def test_a_stale_exit_is_never_cancelled_into_nothing():
    cancelled: list[str] = []
    lifecycle = OrderLifecycle(
        guard=StaleLimitGuard(max_working_seconds=300, cancel_stale=True),
        cancel_order=cancelled.append)
    lifecycle.track(order(side="sell", updated_at=T0), ts=T0)
    lifecycle.ack("cid-1", "rh-1", ts=T0 + 1)
    actions = lifecycle.sweep(T0 + 301)
    assert [action.kind for action in actions] == [SweepKind.PAGE]
    assert actions[0].reason == EXIT_UNFILLED_PAGE
    assert cancelled == []


def test_a_fresh_working_order_produces_no_action():
    lifecycle = OrderLifecycle(guard=StaleLimitGuard(max_working_seconds=300))
    lifecycle.track(order(updated_at=T0), ts=T0)
    lifecycle.ack("cid-1", "rh-1", ts=T0 + 1)
    assert lifecycle.sweep(T0 + 10) == []


def test_a_replacement_refreshes_the_stale_clock():
    cancelled: list[str] = []
    lifecycle = OrderLifecycle(guard=StaleLimitGuard(max_working_seconds=300,
                                                     cancel_stale=True),
                               cancel_order=cancelled.append)
    lifecycle.track(order(updated_at=T0), ts=T0)
    lifecycle.ack("cid-1", "rh-1", ts=T0 + 1)
    lifecycle.replace_requested("cid-1", ts=T0 + 200, limit=1.25)
    lifecycle.replace_confirmed("cid-1", ts=T0 + 201)
    assert lifecycle.sweep(T0 + 400) == []
    assert lifecycle.sweep(T0 + 502) != []
    assert cancelled == ["rh-1"]


def test_a_failed_cancel_is_paged_not_pretended():
    def dead(_order_id: str) -> None:
        raise TransportError("socket died")

    lifecycle = OrderLifecycle(
        guard=StaleLimitGuard(max_working_seconds=300, cancel_stale=True),
        cancel_order=dead)
    lifecycle.track(order(updated_at=T0), ts=T0)
    lifecycle.ack("cid-1", "rh-1", ts=T0 + 1)
    actions = lifecycle.sweep(T0 + 301)
    assert [action.kind for action in actions] == [SweepKind.PAGE]
    assert actions[0].reason.startswith(CANCEL_FAILED)
    assert actions[0].reason != ""


def test_a_cancel_with_no_transport_is_paged_not_pretended():
    lifecycle = OrderLifecycle(
        guard=StaleLimitGuard(max_working_seconds=300, cancel_stale=True))
    lifecycle.track(order(updated_at=T0), ts=T0)
    lifecycle.ack("cid-1", "rh-1", ts=T0 + 1)
    actions = lifecycle.sweep(T0 + 301)
    assert [action.kind for action in actions] == [SweepKind.PAGE]
    assert actions[0].reason == CANCEL_UNAVAILABLE


def test_the_guard_answers_none_for_an_order_that_is_not_working():
    guard = StaleLimitGuard(max_working_seconds=300)
    assert guard.evaluate(order(), T0 + 10_000).kind is SweepKind.NONE
    assert guard.evaluate(order(state=OrderState.FILLED), T0 + 10_000).kind is SweepKind.NONE


def test_the_guard_refuses_a_nonsense_window():
    with pytest.raises(OrderError) as caught:
        StaleLimitGuard(max_working_seconds=0)
    assert caught.value.reason == "bad_guard_window"


# ── registry replay / integrity ──────────────────────────────────────────────
def test_a_missing_registry_file_is_an_empty_registry(tmp_path):
    registry = OrderRegistry.load(tmp_path / "missing.jsonl")
    assert registry.orders == {} and registry.seq == 0


def test_registry_rows_are_contiguous_and_one_line_each(tmp_path):
    path = tmp_path / "orders.jsonl"
    lifecycle = OrderLifecycle(registry=OrderRegistry(path))
    lifecycle.track(order(), ts=T0)
    lifecycle.ack("cid-1", "rh-1", ts=T0 + 1)
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [line["seq"] for line in lines] == [1, 2]
    assert [line["event"] for line in lines] == ["track", "ack"]
    assert all(line["v"] == 1 for line in lines)


def test_a_torn_final_line_is_ignored_and_counted(tmp_path):
    path = tmp_path / "orders.jsonl"
    lifecycle = OrderLifecycle(registry=OrderRegistry(path))
    lifecycle.track(order(), ts=T0)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"v": 1, "seq": 2, "client_order_id": "cid-1"')
    replayed = OrderRegistry.load(path)
    assert replayed.torn_lines == 1
    assert replayed.seq == 1
    assert replayed.get("cid-1") is not None


def test_corruption_in_the_middle_is_refused(tmp_path):
    path = tmp_path / "orders.jsonl"
    lifecycle = OrderLifecycle(registry=OrderRegistry(path))
    lifecycle.track(order(), ts=T0)
    lifecycle.ack("cid-1", "rh-1", ts=T0 + 1)
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], "{not json", lines[1]]) + "\n", encoding="utf-8")
    with pytest.raises(OrderRegistryError) as caught:
        OrderRegistry.load(path)
    assert caught.value.reason == "registry_corrupt"


def test_a_sequence_gap_is_refused(tmp_path):
    path = tmp_path / "orders.jsonl"
    lifecycle = OrderLifecycle(registry=OrderRegistry(path))
    lifecycle.track(order(), ts=T0)
    lifecycle.ack("cid-1", "rh-1", ts=T0 + 1)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[1]["seq"] = 5
    path.write_text(json.dumps(rows[0]) + "\n" + json.dumps(rows[1]) + "\n",
                    encoding="utf-8")
    with pytest.raises(OrderRegistryError) as caught:
        OrderRegistry.load(path)
    assert caught.value.reason == "registry_sequence"


def test_an_unknown_schema_version_is_refused(tmp_path):
    path = tmp_path / "orders.jsonl"
    lifecycle = OrderLifecycle(registry=OrderRegistry(path))
    lifecycle.track(order(), ts=T0)
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    row["v"] = 99
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(OrderRegistryError) as caught:
        OrderRegistry.load(path)
    assert caught.value.reason == "registry_schema"


def test_verify_reports_no_problem_for_a_clean_log(tmp_path):
    path = tmp_path / "orders.jsonl"
    lifecycle = OrderLifecycle(registry=OrderRegistry(path))
    lifecycle.track(order(), ts=T0)
    lifecycle.ack("cid-1", "rh-1", ts=T0 + 1)
    registry = OrderRegistry.load(path)
    assert registry.problems() == []
    assert verify_orders_file(path) == []


def test_verify_names_a_defect_instead_of_loading(tmp_path):
    path = tmp_path / "orders.jsonl"
    path.write_text('{"v": 1, "seq": 2}\n', encoding="utf-8")
    problems = verify_orders_file(path)
    assert problems and any("seq" in problem for problem in problems)


def test_open_orders_keeps_pending_working_and_unknown(tmp_path):
    registry = OrderRegistry(tmp_path / "orders.jsonl")
    registry.append(order(client_order_id="a", state=OrderState.PENDING), event="track", ts=T0)
    registry.append(order(client_order_id="b", state=OrderState.WORKING,
                          venue_order_id="rh-2"), event="ack", ts=T0)
    registry.append(order(client_order_id="c", state=OrderState.UNKNOWN), event="timeout",
                    ts=T0)
    registry.append(order(client_order_id="d", state=OrderState.CANCELLED), event="expire",
                    ts=T0)
    assert {item.client_order_id for item in registry.open_orders()} == {"a", "b", "c"}
    assert [item.client_order_id for item in registry.working_orders()] == ["b"]


def test_render_helpers_name_the_state_and_the_integrity():
    registry = OrderRegistry()
    registry.append(order(state=OrderState.WORKING, venue_order_id="rh-1"),
                    event="ack", ts=T0 + 1)
    lines = render_registry(registry, now=T0 + 10)
    assert any("state=working" in line for line in lines)
    assert render_registry_verify(registry)[0].startswith("orders: OK")


# ── paper broker working-order surface ───────────────────────────────────────
def test_paper_broker_keeps_a_working_order_when_asked_to():
    broker = PaperBroker(fill_immediately=False)
    ack = broker.place(intent())
    assert ack.ok
    assert broker.order_status(ack.broker_order_id).state is OrderState.WORKING
    assert [item.venue_order_id for item in broker.open_orders()] == [ack.broker_order_id]


def test_paper_broker_cancel_is_idempotent_and_closes_the_order():
    broker = PaperBroker(fill_immediately=False)
    ack = broker.place(intent())
    cancelled = broker.cancel_order(ack.broker_order_id)
    assert cancelled.state is OrderState.CANCELLED
    assert broker.cancel_order(ack.broker_order_id).state is OrderState.CANCELLED
    assert broker.open_orders() == []


def test_paper_broker_cannot_cancel_a_filled_order():
    broker = PaperBroker()
    ack = broker.place(intent())
    with pytest.raises(BrokerError) as caught:
        broker.cancel_order(ack.broker_order_id)
    assert "order_terminal" in str(caught.value)


def test_paper_broker_replace_changes_the_working_order():
    broker = PaperBroker(fill_immediately=False)
    ack = broker.place(intent())
    replaced = broker.replace_order(ack.broker_order_id, limit=1.25, qty=3)
    assert replaced.limit == 1.25 and replaced.qty == 3
    assert broker.order_status(ack.broker_order_id).limit == 1.25


def test_paper_broker_refuses_a_replace_of_a_filled_order():
    broker = PaperBroker()
    ack = broker.place(intent())
    with pytest.raises(BrokerError) as caught:
        broker.replace_order(ack.broker_order_id, limit=1.25, qty=3)
    assert "order_not_working" in str(caught.value)


def test_paper_broker_refuses_an_unknown_order():
    with pytest.raises(BrokerError) as caught:
        PaperBroker().order_status("paper-99")
    assert "unknown_order" in str(caught.value)


# ── Robinhood adapter: dry run ───────────────────────────────────────────────
def test_dry_run_cancel_replace_and_status_are_local_and_marked():
    broker = RobinhoodAgenticBroker(token="", dry_run=True)
    ack = broker.place(intent())
    status = broker.order_status(ack.broker_order_id)
    assert status.dry_run and status.state is OrderState.WORKING
    replaced = broker.replace_order(ack.broker_order_id, limit=1.25, qty=3)
    assert replaced.dry_run and replaced.limit == 1.25
    cancelled = broker.cancel_order(ack.broker_order_id)
    assert cancelled.state is OrderState.CANCELLED and cancelled.dry_run
    assert broker.open_orders() == []


def test_dry_run_refuses_an_unknown_order():
    with pytest.raises(BrokerError) as caught:
        RobinhoodAgenticBroker(token="", dry_run=True).cancel_order("dry-99")
    assert "unknown_order" in str(caught.value)


# ── Robinhood adapter: live HTTP shape, idempotency, taxonomy ───────────────
TOKEN = "secret-token-value-1234567890"


class Transport:
    """A scripted transport: records requests and answers in order."""

    def __init__(self, *responses: tuple[int, dict]) -> None:
        self.responses = list(responses) or [(200, {})]
        self.requests: list = []
        self.bodies: list = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        self.bodies.append(json.loads(request.data) if request.data else None)
        status, body = self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]
        return status, body


def live(transport: Transport) -> RobinhoodAgenticBroker:
    return RobinhoodAgenticBroker(base_url="http://localhost:1", token=TOKEN,
                                  dry_run=False, transport=transport, retries=3,
                                  sleep=lambda _s: None)


def header(request, name: str) -> str:
    """Read a request header case-insensitively (urllib title-cases its own dict)."""
    for key, value in request.headers.items():
        if key.lower() == name.lower():
            return value
    raise AssertionError(f"no {name} header on {request.full_url}")


def test_cancel_payload_path_and_idempotency_key():
    transport = Transport((200, {"id": "rh-1", "state": "cancelled"}))
    response = live(transport).cancel_order("rh-1")
    assert response.state is OrderState.CANCELLED
    request = transport.requests[0]
    assert request.get_method() == "POST"
    assert request.full_url == "http://localhost:1/orders/rh-1/cancel"
    assert header(request, "Idempotency-Key") == "cancel:rh-1"
    assert header(request, "Authorization") == f"Bearer {TOKEN}"
    assert transport.bodies[0] == {"account": "agentic"}


def test_replace_payload_and_deterministic_idempotency_key():
    transport = Transport((200, {"id": "rh-1", "state": "working", "limit_price": "1.25"}))
    response = live(transport).replace_order("rh-1", limit=1.25, qty=3)
    assert response.state is OrderState.WORKING and response.limit == 1.25
    request = transport.requests[0]
    assert request.full_url == "http://localhost:1/orders/rh-1/replace"
    assert header(request, "Idempotency-Key") == "replace:rh-1:3:1.25"
    assert transport.bodies[0]["quantity"] == 3
    assert transport.bodies[0]["limit_price"] == 1.25


def test_a_repeated_replace_uses_the_same_idempotency_key():
    transport = Transport((200, {"id": "rh-1", "state": "working"}))
    broker = live(transport)
    broker.replace_order("rh-1", limit=1.25, qty=3)
    broker.replace_order("rh-1", limit=1.25, qty=3)
    assert [header(request, "Idempotency-Key") for request in transport.requests] == [
        "replace:rh-1:3:1.25", "replace:rh-1:3:1.25"]


def test_status_reads_the_venue_order_fields():
    transport = Transport((200, {
        "id": "rh-9", "state": "partially_filled", "quantity": 5,
        "cumulative_quantity": 2, "limit_price": "1.06", "time_in_force": "day",
        "contract_key": CONTRACT, "side": "buy",
    }))
    response = live(transport).order_status("rh-9")
    assert response.state is OrderState.PARTIAL
    assert (response.qty, response.filled_qty) == (5, 2)
    assert response.limit == 1.06
    assert response.tif is TimeInForce.DAY
    assert transport.requests[0].get_method() == "GET"


def test_a_status_answer_without_a_state_is_unknown_not_invented():
    transport = Transport((200, {"id": "rh-9"}))
    response = live(transport).order_status("rh-9")
    assert response.state is OrderState.UNKNOWN and response.reason == "state_not_reported"


def test_an_unfamiliar_venue_state_is_refused():
    transport = Transport((200, {"id": "rh-9", "state": "teleported"}))
    with pytest.raises(BrokerError) as caught:
        live(transport).order_status("rh-9")
    assert "unknown_venue_state" in str(caught.value)


def test_cancel_404_is_a_named_unknown_order():
    transport = Transport((404, {"detail": "not found"}))
    with pytest.raises(BrokerError) as caught:
        live(transport).cancel_order("rh-404")
    assert "unknown_order" in str(caught.value)


def test_cancel_5xx_is_retried_then_raises():
    transport = Transport((500, {"detail": "boom"}), (200, {"id": "rh-1",
                                                           "state": "cancelled"}))
    response = live(transport).cancel_order("rh-1")
    assert response.state is OrderState.CANCELLED
    assert len(transport.requests) == 2


def test_a_transport_death_stays_a_transport_error():
    def dead(request, timeout):
        raise TransportError("socket died")

    broker = RobinhoodAgenticBroker(base_url="http://localhost:1", token=TOKEN,
                                    dry_run=False, transport=dead, retries=1,
                                    sleep=lambda _s: None)
    with pytest.raises(TransportError):
        broker.cancel_order("rh-1")


def test_the_token_never_leaks_into_a_working_order_error():
    transport = Transport((500, {"detail": "boom"}))
    with pytest.raises(BrokerError) as caught:
        live(transport).order_status("rh-1")
    assert TOKEN not in str(caught.value)


def test_open_orders_parses_a_list_and_a_wrapped_list():
    rows = [{"id": "rh-1", "state": "working"}, {"id": "rh-2", "state": "filled"}]
    transport = Transport((200, {"orders": rows}))
    orders = live(transport).open_orders()
    assert [venue.state for venue in orders] == [OrderState.WORKING, OrderState.FILLED]
    transport = Transport((200, rows))
    assert len(live(transport).open_orders()) == 2


def test_open_orders_refuses_a_body_without_a_list():
    transport = Transport((200, {"detail": "nope"}))
    with pytest.raises(BrokerError) as caught:
        live(transport).open_orders()
    assert "malformed_open_orders" in str(caught.value)


# ── time-in-force in the order body ─────────────────────────────────────────
@pytest.mark.parametrize("tif", ["day", "gtc", "ioc", "fok"])
def test_tif_is_carried_into_the_order_body(tif):
    transport = Transport((200, {"id": "rh-1", "average_price": 1.06}))
    live(transport).place(intent(time_in_force=tif))
    assert transport.bodies[0]["time_in_force"] == tif


def test_an_unknown_tif_is_refused_before_send():
    transport = Transport((200, {}))
    with pytest.raises(BrokerError) as caught:
        live(transport).place(intent(time_in_force="forever"))
    assert "bad_tif" in str(caught.value)
    assert transport.requests == []


def test_orderintent_defaults_to_gtc_so_existing_callers_are_unchanged():
    assert intent().time_in_force == "gtc"


# ── IllegalTransition placeholder is covered by OrderError (same base class) ─
def test_illegal_transition_alias_is_the_same_refusal_type():
    assert issubclass(IllegalTransition, OrderError)
