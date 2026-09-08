from __future__ import annotations

from dataclasses import dataclass

from .domain import BookState, PositionStatus


@dataclass(frozen=True, slots=True)
class InvariantViolation:
    code: str
    contract_key: str | None
    detail: str


def validate_book_state(
    state: BookState,
    *,
    max_open_positions: int = 2,
) -> tuple[InvariantViolation, ...]:
    violations: list[InvariantViolation] = []
    open_count = 0
    for key, position in state.positions.items():
        if key != position.contract_key:
            violations.append(InvariantViolation("key_mismatch", key, position.contract_key))
        if position.generation < 0:
            violations.append(
                InvariantViolation("negative_generation", key, str(position.generation))
            )
        if position.quantity < 0:
            violations.append(InvariantViolation("negative_quantity", key, str(position.quantity)))
        if position.average_price < 0:
            violations.append(
                InvariantViolation("negative_average_price", key, str(position.average_price))
            )
        if (
            position.status in {PositionStatus.FLAT, PositionStatus.CLOSED}
            and position.quantity != 0
        ):
            violations.append(
                InvariantViolation("closed_with_quantity", key, str(position.quantity))
            )
        if position.status in {
            PositionStatus.PENDING_ENTRY,
            PositionStatus.OPEN,
            PositionStatus.CLOSING,
        }:
            open_count += 1
            if not position.source_entry_message_id:
                violations.append(InvariantViolation("missing_source_message", key, ""))
            if not position.source_channel_id:
                violations.append(InvariantViolation("missing_source_channel", key, ""))
            if not position.source_author_id:
                violations.append(InvariantViolation("missing_source_author", key, ""))
    if open_count > max_open_positions:
        violations.append(
            InvariantViolation("max_open_exceeded", None, f"{open_count}>{max_open_positions}")
        )
    return tuple(violations)


def assert_valid_book(state: BookState, *, max_open_positions: int = 2) -> None:
    violations = validate_book_state(state, max_open_positions=max_open_positions)
    if violations:
        summary = "; ".join(
            f"{item.code}:{item.contract_key or '-'}:{item.detail}" for item in violations
        )
        raise RuntimeError(f"book invariant violation: {summary}")
