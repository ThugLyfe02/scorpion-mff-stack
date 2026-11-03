"""The buying-power precheck: exact cents, stamped evidence, and a named refusal.

Three claims are defended here.

1. **Money is integer cents and every debit is arithmetic, not estimation.** Odd limits
   (1.17), odd quantities and a declared fee floor are priced exactly; a sub-cent limit
   is refused rather than rounded into a different order.

2. **The feed refuses what is not evidence.** A missing file, a 500, a transport error,
   a missing path, a negative balance, a boolean, a sub-cent number, a missing timestamp
   when an age is armed, or a snapshot older than the limit produces a named
   `AccountError` - never a zero balance. "No account" and "an empty account" are
   opposite facts and only one of them is about money.

3. **The guard fails closed and never blocks an exit.** Every refusal is one of the
   closed `BpReason` strings the listener records as `bp_refused:<reason>`; exactly
   enough money is an admission; a `sell` is admitted before any evidence is consulted.

The HTTP adapter is exercised against a real local server, so the request shape, the
bearer header and the error taxonomy are real rather than mocked. The token must never
appear in an error string.
"""

from __future__ import annotations

import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from scorpion_mff.account import (
    DEFAULT_MAX_AGE_SECONDS,
    AccountError,
    AccountReason,
    AccountSnapshot,
    BpDecision,
    BpReason,
    BuyingPowerGuard,
    FeeModel,
    FileAccountFeed,
    HttpAccountFeed,
    PremiumCost,
    StaticAccountFeed,
    account_feed_from_env,
    cents_to_usd,
    fee_model_from_env,
    render_decision,
    render_fee_model,
    render_snapshot,
    usd_to_cents,
)
from scorpion_mff.types import OrderIntent

T0 = 1_757_340_000.0
QQQ = "QQQ|CALL|720|0DTE"
TOKEN = "account-secret-token-1234567890"
ACCOUNT = "acct-1"


def snap(
    *,
    cash: int = 1_000_00,
    buying_power: int = 1_000_00,
    equity: int = 1_200_00,
    taken_at: float | None = T0,
    source: str = "test",
) -> AccountSnapshot:
    return AccountSnapshot(
        cash=cash, buying_power=buying_power, equity=equity, taken_at=taken_at, source=source
    )


def fees(*, venue: int = 5, regulatory: int = 2, minimum: int = 10) -> FeeModel:
    return FeeModel(
        per_contract_venue_cents=venue,
        per_contract_regulatory_cents=regulatory,
        minimum_fee_cents=minimum,
    )


def guard(
    *, feed=None, fee: FeeModel | None = None, max_age_seconds: float | None = 60.0
) -> BuyingPowerGuard:
    return BuyingPowerGuard(
        feed=feed,
        fee_model=fee if fee is not None else fees(),
        max_age_seconds=max_age_seconds,
        clock=lambda: T0,
    )


def write_json(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


_SCRATCH = Path(__file__).resolve().parent / "_scratch"


@pytest.fixture()
def tmp_path(request):
    """A workspace-local scratch directory, removed afterwards.

    The DSH Windows sandbox cannot read directories made by `tempfile.mkdtemp`
    (which is what pytest's stock `tmp_path` uses), so this module supplies the
    same fixture over a normal directory under `tests/`. Intent and cleanup are
    identical; only the location differs.
    """
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in request.node.name)
    path = _SCRATCH / safe
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


# ── money: exact cents, no float arithmetic ─────────────────────────────────
def test_a_dollar_amount_becomes_exact_cents():
    assert usd_to_cents(1.15) == 115
    assert usd_to_cents("250.10") == 25010
    assert usd_to_cents(0) == 0


def test_a_sub_cent_amount_is_refused_not_rounded():
    with pytest.raises(AccountError, match="whole cent") as excinfo:
        usd_to_cents(1.155)
    assert excinfo.value.reason is AccountReason.MALFORMED_ACCOUNT


def test_a_boolean_or_nan_is_not_money():
    with pytest.raises(AccountError, match="boolean"):
        usd_to_cents(True)
    with pytest.raises(AccountError, match="finite"):
        usd_to_cents(float("nan"))


def test_a_negative_amount_is_named_negative():
    with pytest.raises(AccountError) as excinfo:
        usd_to_cents(-0.01)
    assert excinfo.value.reason is AccountReason.NEGATIVE_ACCOUNT


def test_cents_render_as_dollars_for_humans():
    assert cents_to_usd(25010) == "$250.10"
    assert cents_to_usd(35_121) == "$351.21"


# ── PremiumCost: the debit of one order, exactly ────────────────────────────
def test_premium_is_limit_times_multiplier_times_quantity():
    cost = PremiumCost(qty=3, limit_cents=117, multiplier=100, fee_model=fees())
    assert cost.per_contract_cents == 11_700
    assert cost.premium_cents == 35_100
    assert cost.fees_cents == 21  # 3 * (5 + 2) beats the 10c floor
    assert cost.total_cents == 35_121


def test_the_declared_minimum_fee_floors_a_single_contract():
    cost = PremiumCost(qty=1, limit_cents=100, multiplier=100, fee_model=fees())
    assert cost.fees_cents == 10  # 7c formula < 10c minimum
    assert cost.total_cents == 10_010


def test_a_custom_multiplier_is_honoured_in_cents():
    cost = PremiumCost(qty=2, limit_cents=50, multiplier=10, fee_model=FeeModel())
    assert cost.premium_cents == 1_000  # 50c * 10 shares * 2


def test_premium_cost_serialises_every_component_in_cents():
    payload = PremiumCost(qty=2, limit_cents=100, fee_model=fees()).to_dict()
    assert payload["premium_cents"] == 20_000
    assert payload["fees_cents"] == 14
    assert payload["total_cents"] == 20_014


def test_premium_cost_refuses_a_bad_quantity_or_limit():
    with pytest.raises(AccountError, match="qty"):
        PremiumCost(qty=0, limit_cents=100)
    with pytest.raises(AccountError, match="limit_cents"):
        PremiumCost(qty=1, limit_cents=0)


def test_premium_cost_from_a_dollar_limit_is_exact():
    cost = PremiumCost.from_limit_usd(1.17, qty=3, fee_model=fees())
    assert cost.limit_cents == 117 and cost.premium_cents == 35_100
    with pytest.raises(AccountError, match="whole cent"):
        PremiumCost.from_limit_usd(1.001, qty=1)


# ── FeeModel: declared, validated, loadable ────────────────────────────────
def test_a_fee_model_parses_and_names_its_parts():
    model = FeeModel.from_dict(
        {"per_contract_venue_cents": 5, "per_contract_regulatory_cents": 2, "minimum_fee_cents": 10}
    )
    assert model.per_contract_cents == 7
    assert model.fees_for(3) == 21


def test_an_unknown_fee_key_is_refused():
    with pytest.raises(AccountError, match="unknown key"):
        FeeModel.from_dict({"venue_fee": 1})


def test_a_negative_or_boolean_fee_is_refused():
    with pytest.raises(AccountError):
        FeeModel(per_contract_venue_cents=-1)
    with pytest.raises(AccountError):
        FeeModel(minimum_fee_cents=True)


def test_a_wrong_fee_version_is_refused():
    with pytest.raises(AccountError, match="version"):
        FeeModel.from_dict({"version": 2})


def test_a_missing_fee_file_is_none_but_a_missing_declared_path_raises(tmp_path):
    assert FeeModel.load(tmp_path / "fees.json") is None
    with pytest.raises(AccountError, match="not found"):
        FeeModel.load(tmp_path / "fees.json", missing_ok=False)


def test_a_fee_file_roundtrips(tmp_path):
    path = write_json(
        tmp_path / "fees.json",
        {"per_contract_venue_cents": 3, "per_contract_regulatory_cents": 2, "minimum_fee_cents": 1},
    )
    model = FeeModel.load(path)
    assert model is not None
    assert model.fees_for(2) == 10
    assert model.to_dict()["schema"].startswith("mff.fee-model.v")


def test_a_malformed_fee_file_is_refused(tmp_path):
    path = tmp_path / "fees.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(AccountError, match="not JSON"):
        FeeModel.load(path)


def test_fee_model_from_env_is_none_when_nothing_is_declared(monkeypatch):
    monkeypatch.delenv("MFF_ACCOUNT_FEES", raising=False)
    for name in ("MFF_FEE_VENUE_CENTS", "MFF_FEE_REGULATORY_CENTS", "MFF_FEE_MINIMUM_CENTS"):
        monkeypatch.delenv(name, raising=False)
    assert fee_model_from_env() is None


def test_a_partial_fee_env_is_refused_not_zero_filled(monkeypatch):
    monkeypatch.delenv("MFF_ACCOUNT_FEES", raising=False)
    monkeypatch.setenv("MFF_FEE_VENUE_CENTS", "5")
    monkeypatch.delenv("MFF_FEE_REGULATORY_CENTS", raising=False)
    monkeypatch.delenv("MFF_FEE_MINIMUM_CENTS", raising=False)
    with pytest.raises(AccountError, match="partially declared"):
        fee_model_from_env()


def test_a_complete_fee_env_is_a_declared_model(monkeypatch):
    monkeypatch.delenv("MFF_ACCOUNT_FEES", raising=False)
    monkeypatch.setenv("MFF_FEE_VENUE_CENTS", "5")
    monkeypatch.setenv("MFF_FEE_REGULATORY_CENTS", "2")
    monkeypatch.setenv("MFF_FEE_MINIMUM_CENTS", "10")
    model = fee_model_from_env()
    assert model is not None and model.minimum_fee_cents == 10


# ── the guard: the closed decision set ─────────────────────────────────────
def test_exactly_enough_money_is_an_admission():
    decision = guard(feed=StaticAccountFeed(snap(cash=35_121, buying_power=35_121))).precheck(
        QQQ, "buy", 3, 1.17, T0
    )
    assert decision.ok and decision.reason == ""
    assert decision.required_cents == 35_121
    assert decision.snapshot_age_seconds == 0
    assert decision.log_reason == "bp_ok"


def test_one_cent_short_is_insufficient_cash():
    decision = guard(feed=StaticAccountFeed(snap(cash=35_120, buying_power=10**9))).precheck(
        QQQ, "buy", 3, 1.17, T0
    )
    assert not decision.ok
    assert decision.reason == BpReason.INSUFFICIENT_CASH.value
    assert decision.required_cents == 35_121
    assert decision.log_reason == "bp_refused:insufficient_cash"


def test_buying_power_is_checked_even_with_plenty_of_cash():
    decision = guard(feed=StaticAccountFeed(snap(cash=10**9, buying_power=35_120))).precheck(
        QQQ, "buy", 3, 1.17, T0
    )
    assert not decision.ok
    assert decision.reason == BpReason.INSUFFICIENT_BUYING_POWER.value


def test_an_exit_is_never_blocked_by_missing_evidence():
    """A sell must not wait for the account feed: reducing risk is always legal."""
    decision = guard(feed=StaticAccountFeed(None), fee=None).precheck(QQQ, "sell", 9, 0.01, T0)
    assert decision.ok and decision.required_cents == 0
    assert decision.log_reason == "bp_ok"
    assert "never a reason" in decision.detail


def test_an_exit_passes_even_when_the_fee_model_is_undeclared():
    decision = guard(feed=None).check(QQQ, "sell", 1, 1.0, None, T0)
    assert decision.ok


def test_no_feed_is_a_no_account_refusal():
    decision = guard(feed=None).precheck(QQQ, "buy", 1, 1.0, T0)
    assert not decision.ok and decision.reason == BpReason.NO_ACCOUNT.value


def test_a_missing_static_snapshot_is_no_account():
    decision = guard(feed=StaticAccountFeed(None)).precheck(QQQ, "buy", 1, 1.0, T0)
    assert not decision.ok and decision.reason == BpReason.NO_ACCOUNT.value


def test_a_stale_snapshot_is_refused_with_its_age():
    decision = guard(feed=StaticAccountFeed(snap())).precheck(QQQ, "buy", 1, 1.0, T0 + 61)
    assert not decision.ok
    assert decision.reason == BpReason.STALE_ACCOUNT.value
    assert decision.snapshot_age_seconds == pytest.approx(61)
    assert decision.log_reason == "bp_refused:stale_account"


def test_exactly_the_age_limit_is_still_evidence():
    decision = guard(feed=StaticAccountFeed(snap())).precheck(QQQ, "buy", 1, 1.0, T0 + 60)
    assert decision.ok


def test_an_undatable_snapshot_is_malformed_not_fresh():
    decision = guard().check(QQQ, "buy", 1, 1.0, snap(taken_at=None), T0)
    assert not decision.ok and decision.reason == BpReason.MALFORMED_ACCOUNT.value
    assert "undatable" in decision.detail


def test_an_undeclared_fee_model_refuses_an_entry():
    direct = BuyingPowerGuard(
        feed=StaticAccountFeed(snap()), fee_model=None, max_age_seconds=60.0, clock=lambda: T0
    )
    refused = direct.precheck(QQQ, "buy", 1, 1.0, T0)
    assert not refused.ok and refused.reason == BpReason.FEE_MODEL_MISSING.value
    assert refused.log_reason == "bp_refused:fee_model_missing"


def test_a_missing_snapshot_passed_directly_is_no_account():
    decision = guard().check(QQQ, "buy", 1, 1.0, None, T0)
    assert decision.reason == BpReason.NO_ACCOUNT.value


def test_a_non_snapshot_is_malformed():
    direct = BuyingPowerGuard(fee_model=fees(), clock=lambda: T0)
    decision = direct.check(QQQ, "buy", 1, 1.0, object(), T0)  # type: ignore[arg-type]
    assert decision.reason == BpReason.MALFORMED_ACCOUNT.value


def test_a_sub_cent_limit_is_refused_before_arithmetic():
    with pytest.raises(AccountError, match="whole cent"):
        guard(feed=StaticAccountFeed(snap())).precheck(QQQ, "buy", 1, 1.001, T0)


def test_a_bad_side_is_named_not_guessed():
    with pytest.raises(ValueError, match="buy.*sell"):
        guard().precheck(QQQ, "hold", 1, 1.0, T0)


def test_the_guard_is_deterministic():
    first = guard(feed=StaticAccountFeed(snap(cash=20_000))).precheck(QQQ, "buy", 2, 1.0, T0)
    second = guard(feed=StaticAccountFeed(snap(cash=20_000))).precheck(QQQ, "buy", 2, 1.0, T0)
    assert first == second


def test_precheck_intent_maps_the_order_objects():
    guard_ = guard(feed=StaticAccountFeed(snap(cash=0, buying_power=0)))
    buy = OrderIntent(action="buy", qty=1, limit=1.0, contract_key=QQQ, reason="entry")
    sell = OrderIntent(action="sell", qty=1, limit=1.0, contract_key=QQQ, reason="close")
    assert not guard_.precheck_intent(buy, T0).ok
    assert guard_.precheck_intent(sell, T0).ok


class FailingFeed:
    """A feed whose refusal must be translated, never swallowed."""

    def __init__(self, error: AccountError) -> None:
        self.error = error

    @property
    def configured(self) -> bool:
        return True

    @property
    def source(self) -> str:
        return "failing"

    def snapshot(self, now=None):  # noqa: ANN001, ANN201 - protocol-shaped test doubles
        raise self.error


def test_a_stale_feed_refusal_becomes_a_stale_decision():
    feed = FailingFeed(AccountError("old", reason=AccountReason.STALE_ACCOUNT))
    decision = guard(feed=feed).precheck(QQQ, "buy", 1, 1.0, T0)
    assert decision.reason == BpReason.STALE_ACCOUNT.value
    assert "stale_account" in decision.detail


def test_a_negative_feed_refusal_becomes_malformed_with_the_detail():
    feed = FailingFeed(AccountError("negative", reason=AccountReason.NEGATIVE_ACCOUNT))
    decision = guard(feed=feed).precheck(QQQ, "buy", 1, 1.0, T0)
    assert decision.reason == BpReason.MALFORMED_ACCOUNT.value
    assert "negative_account" in decision.detail


def test_a_decision_refusal_must_name_a_reason():
    with pytest.raises(AccountError, match="name its reason"):
        BpDecision(ok=False)


# ── the file feed: shapes, stamps, refusals ────────────────────────────────
def test_a_dollar_payload_is_converted_to_exact_cents(tmp_path):
    path = write_json(
        tmp_path / "account.json",
        {
            "as_of": T0,
            "cash": 250.10,
            "buying_power": 1000,
            "equity": 1200.5,
        },
    )
    snapshot = FileAccountFeed(path=path).snapshot(T0 + 5)
    assert (snapshot.cash, snapshot.buying_power, snapshot.equity) == (25010, 100_000, 120_050)
    assert snapshot.source.startswith("file:")


def test_explicit_cent_fields_are_read_as_cents(tmp_path):
    path = write_json(
        tmp_path / "account.json",
        {
            "taken_at": T0,
            "cash_cents": 25010,
            "buying_power_cents": 100_000,
            "equity_cents": 120_050,
        },
    )
    snapshot = FileAccountFeed(path=path).snapshot(T0 + 1)
    assert snapshot.cash == 25010


def test_a_cents_feed_can_read_plain_fields_as_cents(tmp_path):
    path = write_json(
        tmp_path / "account.json",
        {
            "as_of": T0,
            "cash": 25010,
            "buying_power": 100_000,
            "equity": 120_050,
        },
    )
    snapshot = FileAccountFeed(path=path, units="cents").snapshot(T0)
    assert snapshot.cash == 25010


def test_a_missing_file_is_not_an_empty_account(tmp_path):
    with pytest.raises(AccountError, match="not found") as excinfo:
        FileAccountFeed(path=tmp_path / "nope.json").snapshot(T0)
    assert excinfo.value.reason is AccountReason.NO_ACCOUNT


def test_an_unconfigured_file_feed_refuses():
    feed = FileAccountFeed()
    assert feed.configured is False
    with pytest.raises(AccountError, match="no account snapshot file"):
        feed.snapshot(T0)


def test_a_non_json_file_is_malformed(tmp_path):
    path = tmp_path / "account.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(AccountError, match="not JSON") as excinfo:
        FileAccountFeed(path=path).snapshot(T0)
    assert excinfo.value.reason is AccountReason.MALFORMED_ACCOUNT


def test_a_missing_money_field_is_named(tmp_path):
    path = write_json(tmp_path / "account.json", {"as_of": T0, "cash": 1.0, "buying_power": 1.0})
    with pytest.raises(AccountError, match="equity") as excinfo:
        FileAccountFeed(path=path).snapshot(T0)
    assert excinfo.value.reason is AccountReason.MALFORMED_ACCOUNT


def test_a_negative_balance_is_refused(tmp_path):
    path = write_json(
        tmp_path / "account.json", {"as_of": T0, "cash": -1.0, "buying_power": 1.0, "equity": 1.0}
    )
    with pytest.raises(AccountError) as excinfo:
        FileAccountFeed(path=path).snapshot(T0)
    assert excinfo.value.reason is AccountReason.NEGATIVE_ACCOUNT


def test_a_sub_cent_balance_is_refused(tmp_path):
    path = write_json(
        tmp_path / "account.json",
        {"as_of": T0, "cash": 250.105, "buying_power": 1.0, "equity": 1.0},
    )
    with pytest.raises(AccountError, match="whole cent"):
        FileAccountFeed(path=path).snapshot(T0)


def test_a_stale_file_snapshot_is_refused_with_its_age(tmp_path):
    path = write_json(
        tmp_path / "account.json", {"as_of": T0, "cash": 1.0, "buying_power": 1.0, "equity": 1.0}
    )
    feed = FileAccountFeed(path=path, max_age_seconds=60.0)
    with pytest.raises(AccountError, match="old") as excinfo:
        feed.snapshot(T0 + 61)
    assert excinfo.value.reason is AccountReason.STALE_ACCOUNT
    assert feed.snapshot(T0 + 59).cash == 100


def test_the_file_mtime_is_the_stamp_when_the_payload_has_none(tmp_path):
    path = write_json(tmp_path / "account.json", {"cash": 1.0, "buying_power": 1.0, "equity": 1.0})
    snapshot = FileAccountFeed(path=path, max_age_seconds=None).snapshot(T0)
    assert snapshot.taken_at == pytest.approx(path.stat().st_mtime, abs=2.0)


def test_the_age_limit_can_be_off_for_a_recorded_fixture(tmp_path):
    path = write_json(
        tmp_path / "account.json", {"as_of": T0, "cash": 1.0, "buying_power": 1.0, "equity": 1.0}
    )
    old = FileAccountFeed(path=path, max_age_seconds=None).snapshot(T0 + 10_000_000)
    assert old.stale(T0 + 10_000_000, None) is False


def test_an_undatable_snapshot_is_refused_when_an_age_is_required():
    snapshot = AccountSnapshot(cash=1, buying_power=1, equity=1, taken_at=None, source="t")
    with pytest.raises(AccountError, match="no timestamp") as excinfo:
        snapshot.check_age(T0, 60.0)
    assert excinfo.value.reason is AccountReason.UNDATABLE_ACCOUNT


# ── the HTTP feed: a real local server, and a token that never leaks ───────
class AccountServer(BaseHTTPRequestHandler):
    script: list[tuple[int, object]] = []
    requests: list[dict[str, str]] = []

    def do_GET(self):  # noqa: N802 - http.server naming
        AccountServer.requests.append({"path": self.path, "headers": dict(self.headers)})
        status, payload = AccountServer.script[
            min(len(AccountServer.requests) - 1, len(AccountServer.script) - 1)
        ]
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        return


@pytest.fixture()
def account_server():
    AccountServer.requests = []
    AccountServer.script = [
        (
            200,
            {
                "as_of": T0,
                "results": {"cash": 250.10, "buying_power": 2000, "equity": 2200.5},
            },
        )
    ]
    server = HTTPServer(("127.0.0.1", 0), AccountServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/account?account={{account}}"
    finally:
        server.shutdown()


def http_feed(url: str, **kw) -> HttpAccountFeed:
    base = dict(
        template=url,
        cash_path="results.cash",
        buying_power_path="results.buying_power",
        equity_path="results.equity",
        token=TOKEN,
        account=ACCOUNT,
        clock=lambda: T0 + 5,
    )
    base.update(kw)
    return HttpAccountFeed(**base)


def test_the_http_feed_reads_each_dotted_path(account_server):
    snapshot = http_feed(account_server).snapshot(T0 + 5)
    assert (snapshot.cash, snapshot.buying_power, snapshot.equity) == (25010, 200_000, 220_050)
    assert snapshot.taken_at == T0
    assert snapshot.source.startswith("http:127.0.0.1")
    request = AccountServer.requests[0]
    assert "account=acct-1" in request["path"]
    assert request["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_a_server_error_is_unavailable_not_zero(account_server):
    AccountServer.script = [(500, {"detail": "boom"})]
    with pytest.raises(AccountError, match="HTTP 500") as excinfo:
        http_feed(account_server).snapshot(T0)
    assert excinfo.value.reason is AccountReason.NO_ACCOUNT


def test_a_transport_error_is_unavailable_and_token_free():
    def dead(request, timeout):  # noqa: ARG001 - transport signature
        return 0, {}

    feed = http_feed("http://account.invalid/account", transport=dead)
    with pytest.raises(AccountError, match="unreachable") as excinfo:
        feed.snapshot(T0)
    assert TOKEN not in str(excinfo.value)
    assert excinfo.value.reason is AccountReason.NO_ACCOUNT


def test_a_missing_path_names_the_path():
    def payload(request, timeout):  # noqa: ARG001
        return 200, {"results": {"cash": 1.0}}

    feed = http_feed("http://account.invalid/account", transport=payload)
    with pytest.raises(AccountError, match="buying_power") as excinfo:
        feed.snapshot(T0)
    assert excinfo.value.reason is AccountReason.MALFORMED_ACCOUNT


def test_an_unconfigured_http_feed_refuses():
    feed = HttpAccountFeed(template="")
    assert feed.configured is False
    with pytest.raises(AccountError, match="MFF_ACCOUNT_URL"):
        feed.snapshot(T0)


def test_an_http_snapshot_older_than_the_limit_is_refused(account_server):
    AccountServer.script = [
        (200, {"as_of": T0, "results": {"cash": 1.0, "buying_power": 1.0, "equity": 1.0}})
    ]
    feed = http_feed(account_server, max_age_seconds=60.0)
    with pytest.raises(AccountError, match="old") as excinfo:
        feed.snapshot(T0 + 61)
    assert excinfo.value.reason is AccountReason.STALE_ACCOUNT


def test_an_http_body_with_no_timestamp_is_stamped_at_the_read_time(account_server):
    AccountServer.script = [(200, {"results": {"cash": 1.0, "buying_power": 1.0, "equity": 1.0}})]
    feed = http_feed(account_server, max_age_seconds=60.0)
    snapshot = feed.snapshot(T0 + 5)
    assert snapshot.taken_at == pytest.approx(T0 + 5)


def test_the_http_source_never_carries_a_token_from_the_url():
    feed = HttpAccountFeed(template=f"http://account.local/x?token={TOKEN}")
    assert TOKEN not in feed.source


# ── environment wiring and rendering ───────────────────────────────────────
def test_no_environment_means_no_account_feed(monkeypatch):
    monkeypatch.delenv("MFF_ACCOUNT_SNAPSHOT", raising=False)
    monkeypatch.delenv("MFF_ACCOUNT_URL", raising=False)
    assert account_feed_from_env() is None


def test_a_snapshot_path_in_the_environment_builds_a_file_feed(monkeypatch, tmp_path):
    monkeypatch.setenv("MFF_ACCOUNT_SNAPSHOT", str(tmp_path / "a.json"))
    monkeypatch.delenv("MFF_ACCOUNT_URL", raising=False)
    feed = account_feed_from_env()
    assert isinstance(feed, FileAccountFeed)
    assert feed.configured is True
    assert feed.max_age_seconds == DEFAULT_MAX_AGE_SECONDS


def test_a_url_in_the_environment_builds_an_http_feed(monkeypatch):
    monkeypatch.delenv("MFF_ACCOUNT_SNAPSHOT", raising=False)
    monkeypatch.setenv("MFF_ACCOUNT_URL", "http://account.local/account")
    feed = account_feed_from_env(max_age_seconds=30.0)
    assert isinstance(feed, HttpAccountFeed)
    assert feed.max_age_seconds == 30.0


def test_the_rendered_snapshot_is_honest_about_money_and_age():
    text = render_snapshot(snap(cash=25010), T0 + 12)
    assert "$250.10" in text and "12s old" in text


def test_the_rendered_decision_names_the_refusal_for_the_fill_log():
    decision = guard(feed=StaticAccountFeed(snap(cash=0, buying_power=0))).precheck(
        QQQ, "buy", 1, 1.0, T0
    )
    text = render_decision(decision, contract=QQQ)
    assert "bp_refused:insufficient_cash" in text and QQQ in text


def test_the_fee_model_renders_in_cents():
    text = render_fee_model(fees())
    assert "cents/contract" in text and "minimum" in text
