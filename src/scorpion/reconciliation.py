from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Sequence

from .domain import BookState, PositionStatus


class ReconciliationSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class ExternalPositionObservation:
    contract_key: str
    quantity: int
    average_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationFinding:
    code: str
    severity: ReconciliationSeverity
    contract_key: str
    detail: str


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    findings: tuple[ReconciliationFinding, ...]

    @property
    def clean(self) -> bool:
        return not self.findings

    @property
    def critical(self) -> bool:
        return any(
            finding.severity is ReconciliationSeverity.CRITICAL for finding in self.findings
        )


def reconcile_positions(
    state: BookState,
    observations: Sequence[ExternalPositionObservation],
) -> ReconciliationReport:
    findings: list[ReconciliationFinding] = []
    external = {observation.contract_key: observation for observation in observations}

    for contract_key, position in state.positions.items():
        observed = external.pop(contract_key, None)
        if position.quantity > 0:
            if observed is None or observed.quantity <= 0:
                findings.append(
                    ReconciliationFinding(
                        "missing_external_position",
                        ReconciliationSeverity.CRITICAL,
                        contract_key,
                        f"source state quantity={position.quantity}, external quantity=0",
                    )
                )
                continue
            if observed.quantity != position.quantity:
                findings.append(
                    ReconciliationFinding(
                        "quantity_mismatch",
                        ReconciliationSeverity.CRITICAL,
                        contract_key,
                        f"source={position.quantity}, external={observed.quantity}",
                    )
                )
            if (
                observed.average_price is not None
                and position.average_price > 0
                and observed.average_price != position.average_price
            ):
                findings.append(
                    ReconciliationFinding(
                        "average_price_mismatch",
                        ReconciliationSeverity.WARNING,
                        contract_key,
                        f"source={position.average_price}, external={observed.average_price}",
                    )
                )
        elif observed is not None and observed.quantity > 0:
            severity = (
                ReconciliationSeverity.WARNING
                if position.status is PositionStatus.PENDING_ENTRY
                else ReconciliationSeverity.CRITICAL
            )
            findings.append(
                ReconciliationFinding(
                    "external_position_not_reflected_in_state",
                    severity,
                    contract_key,
                    f"status={position.status.value}, external quantity={observed.quantity}",
                )
            )

    for contract_key, observed in external.items():
        if observed.quantity <= 0:
            continue
        findings.append(
            ReconciliationFinding(
                "unexpected_external_position",
                ReconciliationSeverity.CRITICAL,
                contract_key,
                f"external quantity={observed.quantity} has no deterministic source state",
            )
        )

    return ReconciliationReport(tuple(findings))
