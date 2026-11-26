from __future__ import annotations

import pytest

from scorpion.paper_worker_calibration import _coverage
from scorpion.provenance import stable_hash


def test_coverage_retains_not_dispatched_and_rejects_duplicates():
    source = {"kind": "intent", "seq": 2, "call_id": "a", "chase": False, "quantity": 4}
    audit = {
        "kind": "worker_disposition", "intent_seq": 2, "call_id": "a", "role": "INITIAL",
        "disposition": "NOT_DISPATCHED", "source_record_sha256": stable_hash(source),
    }
    candidates, dispositions = _coverage((source,), (audit,))
    assert candidates[2]["quantity"] == 4
    assert dispositions[2]["disposition"] == "NOT_DISPATCHED"
    with pytest.raises(ValueError, match="roster"):
        _coverage((source,), (audit, audit))
    with pytest.raises(ValueError, match="coverage"):
        _coverage((source,), ())
    with pytest.raises(ValueError, match="identity"):
        _coverage(({**source, "quantity": 5},), (audit,))
