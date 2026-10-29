"""The risk/sizing/settlement/expiry/authorization layer.

Written to the standard the rest of the package holds itself to: every test names the
money-losing behaviour it prevents, and the reduce-only invariant is asserted from both
directions (a config that tries to size up must fail at load; a decision that does must be
clamped at apply).
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from scorpion.authorization import authorize, idempotency_key
from scorpion.broker import Quote
from scorpion.config import DEFAULT_POLICY, Policy
from scorpion.domain import (
    BookState,
    Effect,
    EffectKind,
    EventKind,
    PositionState,
    PositionStatus,
    SignalEvent,
)
from scorpion.expiry import expire_due, expire_on_roll, expiry_of, unparseable_keys
from scorpion.reducer import reduce_book
from scorpion.risk import (
    RiskConfigError,
    RiskLimits,
    RiskState,
    apply_factor,
    decide,
)
from scorpion.settlement import SettlementError, settle_fill
from scorpion.sizing import ClipTable, SizingError, contracts_for, size_effect

D = Decimal
REF_PRICE = Decimal("1.05")
NOW = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
DAY = date(2026, 9, 8)
KEY = "QQQ|CALL|719|2026-09-08"


def event(kind: EventKind = EventKind.ENTRY, eid: str = "e1", ts: datetime = NOW) -> SignalEvent:
    return SignalEvent(
        event_id=eid,
        message_id=eid,
        kind=kind,
        channel_id="1231301953972207667",
        author_id="a",
        source_ts_utc=ts,
        received_ts_utc=ts,
        ticker="QQQ",
        option_side="CALL",
        strike=D("719"),
        expiry=date(2026, 9, 8),
        referenced_price=D("1.05"),
    )


def position(qty: int = 10, avg: str = "1.00", status: PositionStatus = PositionStatus.OPEN) -> PositionState:
    return PositionState(KEY, status=status, generation=1, quantity=qty, average_price=D(avg))


def book(pos: PositionState | None = None, *, risk: RiskState | None = None) -> BookState:
    state = BookState(positions={KEY: pos} if pos else {})
    if risk is not None:
        state = BookState(positions=state.positions, risk=risk)
    return state


# ── sizing ─────────────────────────────────────────────────────────────────
def test_a_clip_buys_whole_contracts_and_never_rounds_up():
    # $1,500 at $1.00 = 15 contracts exactly; at $0.97 it is 15.46 -> 15
    assert contracts_for(D("1500"), D("1.00")) == 15
    assert contracts_for(D("1500"), D("0.97")) == 15
    assert contracts_for(D("1500"), D("1.01")) == 14


def test_a_decided_trade_is_never_sized_to_zero():
    assert contracts_for(D("10"), D("5.00")) == 1
    assert contracts_for(D("10"), D("5.00"), minimum=0) == 0


def test_sizing_refuses_nonsense_instead_of_guessing():
    with pytest.raises(SizingError):
        contracts_for(D("1500"), D("0"))
    with pytest.raises(SizingError):
        contracts_for(D("-1"), D("1.00"))
    with pytest.raises(SizingError):
        contracts_for(D("1500"), D("1.00"), minimum=5, maximum=2)


def test_the_clip_is_capped():
    assert contracts_for(D("100000"), D("0.50"), maximum=50) == 50


def test_high_confidence_channel_gets_the_bigger_clip():
    standard = size_effect(quantity_hint=None, limit_price=D("1.00"), channel_id="1231301953972207667", is_add=False)
    high = size_effect(quantity_hint=None, limit_price=D("1.00"), channel_id="1448448931116748993", is_add=False)
    assert standard == 15 and high == 30


def test_an_add_clip_is_larger_than_the_base_clip():
    base = size_effect(quantity_hint=None, limit_price=D("1.00"), channel_id="x", is_add=False)
    add = size_effect(quantity_hint=None, limit_price=D("1.00"), channel_id="x", is_add=True)
    assert add > base


def test_a_quantity_hint_wins_over_the_budget():
    assert size_effect(quantity_hint=1, limit_price=D("0.50"), channel_id="x", is_add=False) == 1


def test_a_clip_table_that_undercuts_the_base_clip_is_rejected():
    with pytest.raises(SizingError):
        ClipTable(standard_add_usd=D("100"), standard_usd=D("1500"))


# ── risk: the reduce-only invariant ────────────────────────────────────────
def test_a_configuration_that_could_size_up_is_rejected():
    with pytest.raises(RiskConfigError):
        RiskLimits(channel_multipliers=(("x", D("1.5")),))
    with pytest.raises(RiskConfigError):
        RiskLimits(ladder=((D("-1500"), D("2")),))
    with pytest.raises(RiskConfigError):
        RiskLimits(max_multiplier=D("1.5"))


def test_a_ladder_that_is_out_of_order_is_rejected():
    with pytest.raises(RiskConfigError):
        RiskLimits(ladder=((D("-3000"), D("0")), (D("-1500"), D("0.5"))))


def test_nothing_binding_means_full_size():
    decision, _ = decide(RiskLimits(), RiskState(), market_day=DAY, channel_id="x")
    assert decision.factor == D("1") and not decision.stand_down


def test_the_daily_stop_stands_down():
    state = RiskState(realized_today=D("-3000"), market_day=DAY)
    decision, _ = decide(RiskLimits(), state, market_day=DAY, channel_id="x")
    assert decision.stand_down and any("daily_stop" in r for r in decision.reasons)


def test_the_ladder_halves_before_it_stops():
    half, _ = decide(RiskLimits(), RiskState(realized_today=D("-1600"), market_day=DAY), market_day=DAY, channel_id="x")
    stop, _ = decide(RiskLimits(), RiskState(realized_today=D("-3000"), market_day=DAY), market_day=DAY, channel_id="x")
    assert half.factor == D("0.5") and not half.stand_down
    assert stop.factor == D("0")


def test_a_channel_tilt_cannot_cancel_a_stop():
    limits = RiskLimits(channel_multipliers=(("x", D("1")),))
    decision, _ = decide(limits, RiskState(realized_today=D("-5000"), market_day=DAY), market_day=DAY, channel_id="x")
    assert decision.stand_down


def test_a_losing_streak_halves_and_then_stops():
    two, _ = decide(RiskLimits(), RiskState(consecutive_losses=2, market_day=DAY), market_day=DAY, channel_id="x")
    three, _ = decide(RiskLimits(), RiskState(consecutive_losses=3, market_day=DAY), market_day=DAY, channel_id="x")
    assert two.factor == D("0.5") and three.stand_down


def test_a_win_breaks_the_streak():
    state = RiskState(market_day=DAY).settled(D("-100"), DAY).settled(D("500"), DAY)
    assert state.consecutive_losses == 0 and state.realized_today == D("400")


def test_a_new_day_resets_today_but_not_the_equity_path():
    state = RiskState(market_day=DAY).settled(D("-2000"), DAY).rolled(date(2026, 9, 9))
    assert state.realized_today == 0 and state.equity == D("-2000")


def test_a_backdated_day_cannot_reset_today_or_rearm_the_daily_stop():
    state = RiskState(market_day=DAY).settled(D("-3000"), DAY)
    earlier = state.rolled(date(2026, 8, 9))
    assert earlier == state, "a roll must never move the market day backwards"
    decision, after = decide(
        RiskLimits(), state, market_day=date(2026, 8, 9), channel_id="x"
    )
    assert decision.stand_down, "the daily stop that fired must stay fired"
    assert after == state and after.realized_today == D("-3000")


def test_a_backdated_day_cannot_reset_a_losing_streak():
    state = RiskState(market_day=DAY).settled(D("-100"), DAY).settled(D("-100"), DAY)
    decision, after = decide(
        RiskLimits(), state, market_day=date(2026, 8, 9), channel_id="x"
    )
    assert after.consecutive_losses == 2
    assert decision.factor == D("0.5"), "the streak must still de-size the next entry"


def test_max_drawdown_halts_and_the_halt_survives_the_day_roll():
    state = RiskState(market_day=DAY)
    state = state.settled(D("-10000"), DAY)
    decision, after = decide(RiskLimits(), state, market_day=DAY, channel_id="x")
    assert decision.stand_down and after.halted_reason
    later, _ = decide(RiskLimits(), after.rolled(date(2026, 9, 9)), market_day=date(2026, 9, 9), channel_id="x")
    assert later.stand_down, "a stand-down must not clear itself overnight"


def test_apply_factor_only_reduces():
    assert apply_factor(10, D("1")) == 10
    assert apply_factor(10, D("0.5")) == 5
    assert apply_factor(10, D("0")) == 0
    assert apply_factor(10, D("5")) == 10, "a hand-built decision cannot size up"
    assert apply_factor(1, D("0.99")) == 0


# ── settlement: realised P&L ───────────────────────────────────────────────
def test_a_partial_sale_realises_the_difference():
    settlement = settle_fill(state=book(position(10, "1.00")), contract_key=KEY, generation=1, quantity_delta=-4, fill_price=D("1.50"))
    assert settlement.realized == D("200")          # (1.50-1.00) * 4 * 100
    assert settlement.state.positions[KEY].quantity == 6
    assert settlement.state.positions[KEY].average_price == D("1.00")


def test_a_loss_is_negative():
    settlement = settle_fill(state=book(position(4, "2.00")), contract_key=KEY, generation=1, quantity_delta=-4, fill_price=D("1.50"))
    assert settlement.realized == D("-200") and settlement.is_loss
    assert settlement.state.positions[KEY].status is PositionStatus.CLOSED


def test_a_final_fill_closes_the_remainder():
    settlement = settle_fill(state=book(position(7, "1.00")), contract_key=KEY, generation=1, quantity_delta=0, fill_price=D("0.10"), final=True)
    assert settlement.realized == D("-630")         # (0.10-1.00) * 7 * 100
    assert settlement.closed and settlement.state.positions[KEY].quantity == 0


def test_an_add_realises_nothing_and_blends_the_average():
    settlement = settle_fill(state=book(position(10, "1.00")), contract_key=KEY, generation=1, quantity_delta=10, fill_price=D("2.00"))
    assert settlement.realized == 0
    assert settlement.state.positions[KEY].quantity == 20
    assert settlement.state.positions[KEY].average_price == D("1.50")


def test_impossible_fills_raise_rather_than_clamp():
    with pytest.raises(SettlementError):
        settle_fill(state=book(position(1)), contract_key=KEY, generation=1, quantity_delta=-5, fill_price=D("1"))
    with pytest.raises(SettlementError):
        settle_fill(state=book(position()), contract_key=KEY, generation=2, quantity_delta=1, fill_price=D("1"))
    with pytest.raises(SettlementError):
        settle_fill(state=book(position()), contract_key=KEY, generation=1, quantity_delta=1, fill_price=D("0"))
    with pytest.raises(SettlementError):
        settle_fill(state=book(), contract_key=KEY, generation=1, quantity_delta=1, fill_price=D("1"))


# ── expiry ─────────────────────────────────────────────────────────────────
def test_expiry_is_read_from_the_key():
    assert expiry_of(KEY) == DAY
    assert expiry_of("QQQ|CALL|719|0DTE") is None


def test_an_expired_position_is_released_with_a_review_effect():
    state, effects = expire_due(book(position(10)), date(2026, 9, 9))
    assert state.positions[KEY].status is PositionStatus.CLOSED
    assert len(effects) == 1 and effects[0].kind is EffectKind.REVIEW
    assert "expired_position_released" in effects[0].reason


def test_a_live_position_is_untouched():
    state, effects = expire_due(book(position(10)), DAY)
    assert state.positions[KEY].status is PositionStatus.OPEN and effects == ()


def test_an_unparseable_expiry_is_reported_never_released():
    odd = PositionState("QQQ|CALL|719|0DTE", status=PositionStatus.OPEN, generation=1, quantity=5)
    state, effects = expire_due(BookState(positions={odd.contract_key: odd}), DAY)
    assert state.positions["QQQ|CALL|719|0DTE"].status is PositionStatus.OPEN
    assert effects == () and unparseable_keys(state) == ("QQQ|CALL|719|0DTE",)


def test_the_sweep_only_runs_when_the_day_moves():
    state, effects = expire_on_roll(book(position()), DAY, DAY)
    assert effects == ()
    state, effects = expire_on_roll(book(position()), DAY, date(2026, 9, 9))
    assert len(effects) == 1


# ── authorization ──────────────────────────────────────────────────────────
def effect_for(kind: EffectKind = EffectKind.PROPOSE_OPEN, *, hint: int | None = 1) -> Effect:
    return Effect(kind, KEY, "e1", 1, "test", quantity_hint=hint)


def quote(ask: str = "1.10", bid: str = "1.05", age_s: int = 0) -> Quote:
    return Quote(bid=D(bid), ask=D(ask), observed_ts_utc=NOW - timedelta(seconds=age_s))


def auth(
    *,
    state: BookState | None = None,
    quote_: Quote | None = None,
    effect: Effect | None = None,
    now: datetime = NOW,
    reference_price: Decimal | None = REF_PRICE,
    live_enabled: bool | None = True,
    policy: Policy = DEFAULT_POLICY,
    channel_id: str = "1231301953972207667",
):
    return authorize(
        effect if effect is not None else effect_for(),
        state=state if state is not None else book(position()),
        quote=quote_ if quote_ is not None else quote(),
        policy=policy,
        now_utc=now,
        reference_price=reference_price,
        live_enabled=live_enabled,
        channel_id=channel_id,
    )


def test_the_happy_path_produces_an_envelope_at_the_markup_limit():
    result = auth()
    assert result.ok and result.envelope is not None
    # min(ask 1.10, 1.05 * 1.15 = 1.2075) = 1.10
    assert result.envelope.limit_price == D("1.10")
    assert result.envelope.quantity == 1
    assert result.envelope.side == "BUY"


def test_live_submission_is_off_by_default():
    assert auth(live_enabled=None) .reason == "live_submission_disabled"


def test_a_halted_book_cannot_authorize():
    assert auth(state=BookState(positions={KEY: position()}, halted=True)).reason == "system_halted"


def test_a_stale_quote_cannot_authorize():
    assert auth(quote_=quote(age_s=5)).reason.startswith("stale_quote")


def test_a_quote_from_the_future_cannot_authorize():
    future = Quote(bid=D("1.05"), ask=D("1.10"), observed_ts_utc=NOW + timedelta(seconds=5))
    assert auth(quote_=future).reason == "quote_from_future"


def test_a_crossed_quote_cannot_authorize():
    assert auth(quote_=quote(ask="1.00", bid="1.05")).reason == "invalid_quote"


def test_a_dislocated_ask_cannot_authorize():
    assert auth(quote_=quote(ask="1.40"), reference_price=D("1.05")).reason.startswith("price_dislocation")


def test_a_generation_mismatch_cannot_authorize():
    stale = Effect(EffectKind.PROPOSE_OPEN, KEY, "e1", 2, "test", quantity_hint=1)
    assert auth(effect=stale).reason.startswith("stale_generation")


def test_an_unknown_position_cannot_authorize():
    assert auth(state=BookState()).reason == "unknown_position"


def test_a_review_effect_is_never_an_order():
    review = Effect(EffectKind.REVIEW, KEY, "e1", 1, "test")
    assert authorize(review, state=book(position()), quote=quote(), policy=DEFAULT_POLICY, now_utc=NOW, live_enabled=True).reason == "not_an_order_effect"


def test_an_unsized_buy_is_sized_by_the_clip_table():
    result = auth(effect=effect_for(hint=None), quote_=quote(ask="1.00", bid="0.99"))
    assert result.ok and result.envelope is not None
    assert result.envelope.quantity == 15, "$1500 / ($1.00 * 100)"


def test_a_sell_is_limited_to_what_is_held():
    sell = Effect(EffectKind.PROPOSE_CLOSE, KEY, "e1", 1, "test", quantity_hint=99)
    result = auth(effect=sell, state=book(position(4)))
    assert result.ok and result.envelope is not None
    assert result.envelope.quantity == 4 and result.envelope.side == "SELL"
    assert result.envelope.limit_price == D("1.05"), "sells execute at the bid"


def test_the_idempotency_key_is_stable_and_generation_specific():
    assert idempotency_key(effect_for()) == idempotency_key(effect_for())
    newer = Effect(EffectKind.PROPOSE_OPEN, KEY, "e1", 2, "test", quantity_hint=1)
    assert idempotency_key(newer) != idempotency_key(effect_for())


def test_the_envelope_payload_is_flat_and_string_only():
    result = auth()
    assert result.envelope is not None
    payload = result.envelope.to_payload()
    assert all(isinstance(v, str) for v in payload.values())
    assert payload["side"] == "BUY" and payload["contract_key"] == KEY


# ── the reducer honours risk ───────────────────────────────────────────────
def test_the_reducer_refuses_to_propose_while_stood_down():
    state = BookState(risk=RiskState(realized_today=D("-5000"), market_day=DAY, halted_reason="max_drawdown:10000USD"))
    new_state, effects = reduce_book(state, event())
    assert len(effects) == 1 and effects[0].kind is EffectKind.REVIEW
    assert "risk_stand_down" in effects[0].reason
    assert new_state.positions == {}


def test_the_reducer_records_the_risk_day_on_roll():
    state, _ = reduce_book(BookState(), event())
    assert state.risk.market_day == DAY


def test_a_streak_gate_does_not_disturb_a_healthy_book():
    state, effects = reduce_book(BookState(), event())
    assert effects[0].kind is EffectKind.PROPOSE_OPEN and not state.risk.halted_reason
