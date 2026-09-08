from datetime import UTC, datetime, timedelta
from decimal import Decimal

from scorpion.parser import parse_message
from scorpion.semantic import (
    ContractDescriptor,
    ContractValidationStatus,
    InMemoryContractCatalog,
    QuoteQualityStatus,
    QuoteSnapshot,
    validate_contract,
    validate_quote,
)


def test_exact_contract_is_semantically_valid(raw_factory):
    event = parse_message(raw_factory("QQQ 719C TODAY @ 1.01"))
    catalog = InMemoryContractCatalog(
        [ContractDescriptor("QQQ", "CALL", Decimal("719"), event.expiry)]  # type: ignore[arg-type]
    )
    assert validate_contract(event, catalog).status is ContractValidationStatus.EXACT


def test_missing_contract_routes_to_review(raw_factory):
    event = parse_message(raw_factory("QQQ 719C TODAY @ 1.01"))
    catalog = InMemoryContractCatalog([])
    assessment = validate_contract(event, catalog)
    assert assessment.status is ContractValidationStatus.REVIEW
    assert assessment.reason == "contract_not_in_catalog"


def test_quote_validation_detects_stale_and_dislocated_quote():
    now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    quote = QuoteSnapshot(Decimal("1.20"), Decimal("1.40"), now - timedelta(seconds=10))
    assessment = validate_quote(Decimal("1.00"), quote, now=now)
    assert assessment.status is QuoteQualityStatus.REVIEW
    assert "stale_quote" in assessment.flags
    assert "ask_above_reference_threshold" in assessment.flags


def test_crossed_quote_is_invalid():
    now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    quote = QuoteSnapshot(Decimal("1.20"), Decimal("1.10"), now)
    assert validate_quote(Decimal("1.00"), quote, now=now).status is QuoteQualityStatus.INVALID
