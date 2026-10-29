from datetime import UTC, datetime, timedelta

import pytest

from scorpion.authorization import ApprovalEnvelope, validate_approval
from scorpion.domain import Effect, EffectKind, PositionState


def test_rejects_stale_quote():
    now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    effect = Effect(EffectKind.PROPOSE_OPEN, "K", "e", 1, "r")
    env = ApprovalEnvelope(effect, "operator", now, now - timedelta(seconds=4))
    position = PositionState("K", generation=1)
    with pytest.raises(ValueError, match="quote is stale"):
        validate_approval(env, position, now=now)
