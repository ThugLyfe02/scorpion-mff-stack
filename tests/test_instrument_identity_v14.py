from scorpion.instrument_identity import InstrumentIdentity, InstrumentIdentityRegistry
from scorpion.opra_bridge import normalize_databento_opra_row

RAW = "AAPL1 260918C00200000"
CANONICAL = "AAPL|CALL|200|2026-09-18"
TS = 1_789_742_400_000_000_000


def _row() -> dict[str, str]:
    return {
        "symbol": RAW,
        "ts_recv": str(TS + 1_000),
        "ts_event": str(TS),
        "publisher_id": "30",
        "sequence": "1",
        "action": "A",
        "bid_px_00": "1000000000",
        "ask_px_00": "1050000000",
        "bid_sz_00": "10",
        "ask_sz_00": "12",
        "bid_pb_00": "2",
        "ask_pb_00": "3",
    }


def test_point_in_time_identity_overrides_vendor_root_assumption():
    identity = InstrumentIdentity(
        raw_symbol=RAW,
        contract_key=CANONICAL,
        valid_from_ns=TS - 10_000,
        valid_to_ns=TS + 10_000,
        source="vendor-definition",
        instrument_id=123,
        adjusted=True,
    )
    registry = InstrumentIdentityRegistry((identity,))
    events = normalize_databento_opra_row(_row(), identity_registry=registry)
    assert len(events) == 1
    assert events[0].contract_key == CANONICAL
    assert registry.resolve(RAW, TS) == identity
    coverage = registry.coverage(((RAW, TS),))
    assert coverage.complete is True
    assert coverage.adjusted == (RAW,)


def test_identity_registry_fails_closed_on_ambiguous_vendor_symbol():
    registry = InstrumentIdentityRegistry(
        (
            InstrumentIdentity(
                raw_symbol=RAW,
                contract_key=CANONICAL,
                valid_from_ns=TS - 10_000,
                valid_to_ns=None,
                source="definition-a",
            ),
            InstrumentIdentity(
                raw_symbol=RAW,
                contract_key="AAPL|CALL|205|2026-09-18",
                valid_from_ns=TS - 5_000,
                valid_to_ns=None,
                source="definition-b",
            ),
        )
    )
    assert registry.resolve(RAW, TS) is None
    coverage = registry.coverage(((RAW, TS),))
    assert coverage.complete is False
    assert coverage.ambiguous == (RAW,)
    try:
        normalize_databento_opra_row(_row(), identity_registry=registry)
    except ValueError as exc:
        assert "did not resolve to exactly one canonical contract" in str(exc)
    else:
        raise AssertionError("ambiguous vendor symbol must fail closed")
