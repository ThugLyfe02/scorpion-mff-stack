"""M1: live option-contract identity, OSI parsing, and fail-closed resolution.

Every test here pins either a *behaviour* (a symbol a venue can execute, a cache that
is per-session, a broker payload that carries the identity) or a *refusal* (a named
reason, and - where it matters - no HTTP send after the refusal). The refusal tests
assert the reason string, not just "it raised", because the reason is what the fill
log records.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from scorpion_mff.broker import BrokerError, RobinhoodAgenticBroker
from scorpion_mff.instruments import (
    BAD_CONTRACT_KEY,
    BAD_EXPIRY,
    BAD_OSI_SYMBOL,
    BAD_SIDE,
    BAD_STRIKE,
    BAD_TICKER,
    CONTRACT_AMBIGUOUS,
    CONTRACT_CHAIN_MALFORMED,
    CONTRACT_EXPIRED_SAME_DAY,
    CONTRACT_INSTRUMENT_MISSING,
    CONTRACT_MISSING,
    CONTRACT_MULTIPLIER_INVALID,
    CONTRACT_STRIKE_MISMATCH,
    ChainContractResolver,
    InstrumentError,
    InstrumentRef,
    OptionContract,
    StaticContractResolver,
    parse_cli_contract_key,
)
from scorpion_mff.types import OrderIntent, Side

SESSION = date(2026, 9, 11)
T0 = datetime(2026, 9, 11, 14, 0, tzinfo=UTC).timestamp()

QQQ = OptionContract(ticker="QQQ", side=Side.CALL, strike_cents=71900, expiry=SESSION)


def refuse(reason: str, call, *args, **kwargs) -> InstrumentError:
    """Assert one named refusal and return it for detail assertions."""
    with pytest.raises(InstrumentError) as caught:
        call(*args, **kwargs)
    assert caught.value.reason == reason
    return caught.value


def row_for(contract: OptionContract, *, instrument_id: str = "inst-1",
            multiplier: object = 100, **overrides: object) -> dict:
    row: dict = {
        "osi_symbol": contract.osi_symbol,
        "instrument_id": instrument_id,
        "multiplier": multiplier,
        "venue": "robinhood",
    }
    row.update(overrides)
    return row


def resolver_for(rows, *, clock=None, multipliers=frozenset({100})
                 ) -> tuple[ChainContractResolver, list]:
    calls: list = []

    def lookup(ticker, side, strike, expiry):
        calls.append((ticker, side, strike, expiry))
        return rows

    return (ChainContractResolver(lookup=lookup, multipliers=multipliers, venue="robinhood",
                                  clock=clock or (lambda: T0)), calls)


# ── identity + OSI roundtrip ─────────────────────────────────────────────────
def test_osi_symbol_is_the_21_character_venue_identity():
    assert QQQ.osi_symbol == "QQQ   260911C00719000"
    assert len(QQQ.osi_symbol) == 21


def test_osi_roundtrip_returns_the_same_contract():
    parsed = OptionContract.parse_osi_symbol(QQQ.osi_symbol)
    assert parsed == QQQ


def test_osi_roundtrip_pads_a_four_letter_root():
    contract = OptionContract(ticker="SPXW", side=Side.PUT, strike_cents=250, expiry=SESSION)
    assert contract.osi_symbol == "SPXW  260911P00002500"
    assert OptionContract.parse_osi_symbol(contract.osi_symbol) == contract


def test_osi_roundtrip_keeps_a_dotted_root():
    contract = OptionContract(ticker="BRK.B", side=Side.CALL, strike_cents=45000,
                              expiry=date(2026, 12, 18))
    assert contract.osi_symbol[:6] == "BRK.B "
    assert OptionContract.parse_osi_symbol(contract.osi_symbol) == contract


def test_strike_cents_are_the_identity_and_two_point_five_is_250():
    contract = OptionContract.from_contract_key("QQQ|CALL|2.5|2026-09-11")
    assert contract.strike_cents == 250
    assert contract.strike == 2.5
    assert contract.osi_symbol.endswith("C00002500")


def test_osi_symbol_refuses_a_wrong_length():
    refuse(BAD_OSI_SYMBOL, OptionContract.parse_osi_symbol, "QQQ260911C00719000")


def test_osi_symbol_refuses_an_unknown_cp_character():
    malformed = QQQ.osi_symbol[:12] + "X" + QQQ.osi_symbol[13:]
    refuse(BAD_SIDE, OptionContract.parse_osi_symbol, malformed)


def test_osi_symbol_refuses_an_impossible_date():
    malformed = QQQ.osi_symbol[:6] + "261399" + QQQ.osi_symbol[12:]
    refuse(BAD_EXPIRY, OptionContract.parse_osi_symbol, malformed)


def test_osi_symbol_refuses_a_sub_cent_strike():
    malformed = QQQ.osi_symbol[:13] + "00719005"
    refused = refuse(BAD_STRIKE, OptionContract.parse_osi_symbol, malformed)
    assert "cents" in refused.detail


# ── construction validation ──────────────────────────────────────────────────
@pytest.mark.parametrize("ticker", ["", "TOOLONGROOT", "123", "QQQ QQQ"])
def test_construction_refuses_a_bad_root(ticker):
    refuse(BAD_TICKER, OptionContract, ticker=ticker, side=Side.CALL,
           strike_cents=100, expiry=SESSION)


def test_construction_refuses_a_bad_side():
    refuse(BAD_SIDE, OptionContract, ticker="QQQ", side="STRADDLE",
           strike_cents=100, expiry=SESSION)


@pytest.mark.parametrize("strike", [0, -100, 719.0, True, 10_000_000])
def test_construction_refuses_a_bad_strike(strike):
    refuse(BAD_STRIKE, OptionContract, ticker="QQQ", side=Side.CALL,
           strike_cents=strike, expiry=SESSION)


@pytest.mark.parametrize("expiry", ["2026-09-11", datetime(2026, 9, 11), None])
def test_construction_refuses_a_bad_expiry(expiry):
    refuse(BAD_EXPIRY, OptionContract, ticker="QQQ", side=Side.CALL,
           strike_cents=71900, expiry=expiry)


def test_ticker_and_side_are_normalised_but_not_guessed():
    contract = OptionContract(ticker=" qqq ", side="call", strike_cents=71900, expiry=SESSION)
    assert contract.ticker == "QQQ" and contract.side is Side.CALL


# ── contract-key parsing ─────────────────────────────────────────────────────
def test_contract_key_parses_a_fractional_strike():
    contract = OptionContract.from_contract_key("NVDA|PUT|227.5|2026-09-11")
    assert (contract.ticker, contract.side, contract.strike_cents) == ("NVDA", Side.PUT, 22750)
    assert contract.expiry == date(2026, 9, 11)


def test_zero_dte_resolves_from_the_session_date():
    contract = OptionContract.from_contract_key("QQQ|CALL|719|0DTE", SESSION)
    assert contract.expiry == SESSION
    assert contract.contract_key == "QQQ|CALL|719|2026-09-11"


def test_zero_dte_without_a_session_date_is_refused():
    refused = refuse(BAD_EXPIRY, OptionContract.from_contract_key, "QQQ|CALL|719|0DTE")
    assert "session date" in refused.detail


@pytest.mark.parametrize("key", ["QQQ|CALL|719", "QQQ|CALL|719|0DTE|extra", "QQQ"])
def test_contract_key_refuses_the_wrong_number_of_fields(key):
    refuse(BAD_CONTRACT_KEY, OptionContract.from_contract_key, key, SESSION)


def test_contract_key_refuses_an_unknown_side():
    refuse(BAD_SIDE, OptionContract.from_contract_key, "QQQ|STRADDLE|719|2026-09-11")


@pytest.mark.parametrize("strike", ["0", "-5", "2.505", "abc", ""])
def test_contract_key_refuses_a_bad_strike(strike):
    refuse(BAD_STRIKE, OptionContract.from_contract_key,
           f"QQQ|CALL|{strike}|2026-09-11")


@pytest.mark.parametrize("expiry", ["Dec 5", "December 5", "?", "2026-02-30", "soon"])
def test_contract_key_refuses_an_expiry_it_cannot_date(expiry):
    refuse(BAD_EXPIRY, OptionContract.from_contract_key, f"QQQ|CALL|719|{expiry}")


def test_month_day_refusal_names_the_parser_roll():
    refused = refuse(BAD_EXPIRY, OptionContract.from_contract_key, "QQQ|CALL|719|Dec 5")
    assert "month/day" in refused.detail


# ── InstrumentRef ────────────────────────────────────────────────────────────
def test_instrument_ref_validates_its_identity():
    ref = InstrumentRef(instrument_id="rh-42", osi_symbol=QQQ.osi_symbol,
                        multiplier=100, venue="robinhood")
    assert ref.to_dict()["instrument_id"] == "rh-42"

    refuse(CONTRACT_INSTRUMENT_MISSING, InstrumentRef, instrument_id="",
           osi_symbol=QQQ.osi_symbol, multiplier=100)
    refuse(BAD_OSI_SYMBOL, InstrumentRef, instrument_id="x", osi_symbol="short",
           multiplier=100)
    refuse(CONTRACT_MULTIPLIER_INVALID, InstrumentRef, instrument_id="x",
           osi_symbol=QQQ.osi_symbol, multiplier=0)
    refuse(CONTRACT_MULTIPLIER_INVALID, InstrumentRef, instrument_id="x",
           osi_symbol=QQQ.osi_symbol, multiplier=100.0)


# ── static resolver ──────────────────────────────────────────────────────────
def test_static_resolver_returns_the_declared_instrument():
    ref = InstrumentRef("rh-42", QQQ.osi_symbol, 100, "robinhood")
    resolver = StaticContractResolver({QQQ.contract_key: ref})
    assert resolver.resolve(QQQ) is ref


def test_static_resolver_refuses_an_undeclared_contract():
    refuse(CONTRACT_MISSING, StaticContractResolver().resolve, QQQ)


def test_static_resolver_from_payload_derives_the_symbol_and_multiplier():
    resolver = StaticContractResolver.from_payload({
        "QQQ|CALL|719|0DTE": {"instrument_id": "rh-42"},
    }, SESSION)
    ref = resolver.resolve(QQQ)
    assert ref.instrument_id == "rh-42"
    assert ref.osi_symbol == QQQ.osi_symbol
    assert ref.multiplier == 100


def test_static_resolver_payload_refuses_a_row_without_an_instrument():
    with pytest.raises(InstrumentError) as caught:
        StaticContractResolver.from_payload({"QQQ|CALL|719|0DTE": {}}, SESSION)
    assert caught.value.reason == CONTRACT_INSTRUMENT_MISSING


# ── chain resolver: the happy path ──────────────────────────────────────────
def test_chain_resolver_resolves_one_matching_row():
    resolver, calls = resolver_for([row_for(QQQ, instrument_id="rh-42")])
    ref = resolver.resolve(QQQ)
    assert ref.instrument_id == "rh-42"
    assert ref.osi_symbol == QQQ.osi_symbol
    assert ref.multiplier == 100
    assert calls == [("QQQ", Side.CALL, 719.0, SESSION)]


def test_chain_resolver_reads_explicit_fields_when_the_row_has_no_symbol():
    rows = [{"ticker": "QQQ", "side": "CALL", "strike": 719.0, "expiry": "2026-09-11",
             "instrument_id": "rh-42", "multiplier": 100}]
    resolver, _ = resolver_for(rows)
    assert resolver.resolve(QQQ).instrument_id == "rh-42"


def test_chain_resolver_ignores_rows_for_another_expiry_or_side():
    other = OptionContract(ticker="QQQ", side=Side.CALL, strike_cents=71900,
                           expiry=date(2026, 10, 16))
    rows = [row_for(other, instrument_id="wrong-date"), row_for(QQQ)]
    resolver, _ = resolver_for(rows)
    assert resolver.resolve(QQQ).instrument_id == "inst-1"


# ── chain resolver: the cache is per session ────────────────────────────────
def test_chain_resolver_caches_within_a_session():
    contract = OptionContract("QQQ", Side.CALL, 71900, date(2026, 10, 16))
    resolver, calls = resolver_for([row_for(contract)])
    resolver.resolve(contract)
    resolver.resolve(contract)
    assert resolver.lookups == 1
    assert len(calls) == 1


def test_chain_resolver_re_reads_in_a_new_session():
    contract = OptionContract("QQQ", Side.CALL, 71900, date(2026, 10, 16))
    clock = [T0]
    resolver, calls = resolver_for([row_for(contract)], clock=lambda: clock[0])
    resolver.resolve(contract)
    clock[0] += 86_400
    resolver.resolve(contract)
    assert resolver.lookups == 2
    assert len(calls) == 2


def test_chain_resolver_cache_is_keyed_by_contract_too():
    contract = OptionContract("QQQ", Side.CALL, 71900, date(2026, 10, 16))
    other = OptionContract("QQQ", Side.CALL, 72000, date(2026, 10, 16))
    rows = [row_for(contract), row_for(other, instrument_id="inst-2")]
    resolver, calls = resolver_for(rows)
    resolver.resolve(contract)
    resolver.resolve(other)
    assert resolver.lookups == 2
    assert resolver.resolve(other).instrument_id == "inst-2"


# ── chain resolver: every named refusal ──────────────────────────────────────
def test_chain_resolver_refuses_when_the_chain_has_no_row():
    later = OptionContract("QQQ", Side.CALL, 71900, date(2026, 10, 16))
    resolver, _ = resolver_for([])
    refuse(CONTRACT_MISSING, resolver.resolve, later)


def test_chain_resolver_refuses_duplicate_matches_as_ambiguous():
    rows = [row_for(QQQ, instrument_id="a"), row_for(QQQ, instrument_id="b")]
    resolver, _ = resolver_for(rows)
    refused = refuse(CONTRACT_AMBIGUOUS, resolver.resolve, QQQ)
    assert "2" in refused.detail


def test_chain_resolver_refuses_a_strike_the_chain_does_not_list():
    other_strike = OptionContract("QQQ", Side.CALL, 72000, SESSION)
    resolver, _ = resolver_for([row_for(other_strike)])
    refused = refuse(CONTRACT_STRIKE_MISMATCH, resolver.resolve, QQQ)
    assert "720" in refused.detail


def test_chain_resolver_refuses_a_multiplier_outside_the_declared_set():
    resolver, _ = resolver_for([row_for(QQQ, multiplier=10)])
    refused = refuse(CONTRACT_MULTIPLIER_INVALID, resolver.resolve, QQQ)
    assert "declared" in refused.detail


def test_chain_resolver_refuses_a_missing_multiplier():
    row = row_for(QQQ)
    del row["multiplier"]
    resolver, _ = resolver_for([row])
    refuse(CONTRACT_MULTIPLIER_INVALID, resolver.resolve, QQQ)


def test_chain_resolver_refuses_a_row_without_an_instrument_id():
    row = row_for(QQQ)
    del row["instrument_id"]
    resolver, _ = resolver_for([row])
    refuse(CONTRACT_INSTRUMENT_MISSING, resolver.resolve, QQQ)


def test_chain_resolver_refuses_a_contract_that_expired_before_the_session():
    stale = OptionContract("QQQ", Side.CALL, 71900, date(2026, 9, 10))
    resolver, calls = resolver_for([row_for(stale)])
    refuse(CONTRACT_EXPIRED_SAME_DAY, resolver.resolve, stale)
    assert calls == []


def test_chain_resolver_refuses_a_zero_dte_contract_that_left_the_chain():
    resolver, _ = resolver_for([])
    refuse(CONTRACT_EXPIRED_SAME_DAY, resolver.resolve, QQQ)


def test_chain_resolver_refuses_a_malformed_row_rather_than_skipping_it():
    resolver, _ = resolver_for([{"instrument_id": "x", "multiplier": 100}])
    refuse(CONTRACT_CHAIN_MALFORMED, resolver.resolve, QQQ)


def test_chain_resolver_refuses_rows_that_are_not_rows():
    rows = ["not-a-row"]
    resolver, _ = resolver_for(rows)
    refuse(CONTRACT_CHAIN_MALFORMED, resolver.resolve, QQQ)


def test_chain_resolver_refuses_a_declared_multiplier_set_it_cannot_use():
    refuse(CONTRACT_MULTIPLIER_INVALID, ChainContractResolver,
           lookup=lambda *args: [], multipliers=frozenset({0}))


# ── broker integration: identity in the payload, no send on refusal ─────────
def _intent(contract_key: str = "QQQ|CALL|719|0DTE") -> OrderIntent:
    return OrderIntent(action="buy", qty=1, limit=1.06, contract_key=contract_key,
                       reason="entry:standard", idempotency_key=f"{contract_key}:buy:1")


def _broker(captured: list, resolver: StaticContractResolver | None) -> RobinhoodAgenticBroker:
    def transport(request, timeout):
        captured.append(request)
        return 200, {"id": "rh-1", "average_price": 1.07}

    return RobinhoodAgenticBroker(base_url="http://localhost:1", token="tok",
                                  dry_run=False, transport=transport, retries=1,
                                  sleep=lambda _s: None, resolver=resolver,
                                  session_date=SESSION)


def test_broker_payload_carries_the_resolved_identity():
    ref = InstrumentRef("rh-42", QQQ.osi_symbol, 100, "robinhood")
    captured: list = []
    broker = _broker(captured, StaticContractResolver({QQQ.contract_key: ref}))
    ack = broker.place(_intent())
    assert ack.ok
    body = json.loads(captured[0].data or b"{}")
    assert body["osi_symbol"] == QQQ.osi_symbol
    assert body["instrument_id"] == "rh-42"
    assert body["multiplier"] == 100
    assert body["time_in_force"] == "gtc"


def test_broker_refuses_an_unresolved_contract_without_sending():
    captured: list = []
    broker = _broker(captured, StaticContractResolver({}))
    with pytest.raises(BrokerError) as caught:
        broker.place(_intent())
    assert CONTRACT_MISSING in str(caught.value)
    assert captured == []


def test_broker_without_a_resolver_sends_the_unchanged_payload():
    captured: list = []
    broker = _broker(captured, None)
    broker.place(_intent("NVDA|PUT|227.5|2026-09-11"))
    body = json.loads(captured[0].data or b"{}")
    assert "osi_symbol" not in body
    assert body["contract_key"] == "NVDA|PUT|227.5|2026-09-11"


def test_broker_dry_run_never_reads_a_chain():
    def boom(*args, **kwargs):        # pragma: no cover - must never be called
        raise AssertionError("dry-run must not resolve")

    captured: list = []
    broker = RobinhoodAgenticBroker(token="", dry_run=True, transport=None,
                                    resolver=StaticContractResolver({}), clock=boom)
    ack = broker.place(_intent())
    assert ack.ok and ack.dry_run
    assert captured == []


# ── CLI helper ───────────────────────────────────────────────────────────────
def test_cli_helper_returns_the_contract_and_the_osi_symbol():
    answer = parse_cli_contract_key("QQQ|CALL|719|0DTE", SESSION)
    assert answer.ok
    assert answer.osi_symbol == QQQ.osi_symbol
    assert answer.to_dict()["contract_key"] == "QQQ|CALL|719|2026-09-11"


def test_cli_helper_returns_a_named_refusal_instead_of_raising():
    answer = parse_cli_contract_key("QQQ|CALL|719|Dec 5")
    assert not answer.ok
    assert answer.reason == BAD_EXPIRY
    assert "month/day" in answer.detail
