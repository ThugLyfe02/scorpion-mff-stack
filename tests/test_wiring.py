"""The seam: a posture carries its provenance, and arm refuses anything it cannot trust."""

import json

import pytest

from scorpion.drift import Detection
from scorpion.wiring import (
    SCHEMA_VERSION,
    WiringError,
    build,
    compare,
    load_for_arm,
    read,
    render,
    sizes_for_arm,
    write,
)


def rows(prefix, values):
    return [{"identity": f"{prefix}-{i}", "executed_r": v} for i, v in enumerate(values)]


def variants():
    return {
        "baseline": rows("t", [1.0] * 30),
        "no_markup": rows("t", [1.2] * 30),
    }


CLEAN = Detection(distance=0.001, threshold=0.01, size=30, reference_size=300)
DIRTY = Detection(distance=0.5, threshold=0.01, size=30, reference_size=300)


def posture_for(detection=CLEAN, fingerprint="policy-abc", budget=1000.0):
    return build(
        variants(), budget=budget, policy_fingerprint=fingerprint,
        as_of="2026-10-21T14:00:00Z", detection=detection,
    )


def test_a_posture_carries_its_provenance():
    payload = posture_for()
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["identity_count"] == 30
    assert payload["variant_count"] == 2
    assert payload["policy_fingerprint"] == "policy-abc"
    assert sum(payload["weights"].values()) == pytest.approx(1.0)


def test_a_missing_fingerprint_is_refused():
    with pytest.raises(WiringError):
        build(variants(), budget=100.0, policy_fingerprint="", as_of="now", detection=CLEAN)


def test_a_posture_without_a_drift_verdict_is_refused():
    with pytest.raises(WiringError):
        build(variants(), budget=100.0, policy_fingerprint="p", as_of="now", detection=None)


def test_a_drifted_window_withdraws_the_allocation():
    payload = posture_for(detection=DIRTY)
    assert payload["allocated"] is False
    assert all(size == 0.0 for size in payload["sizes"].values())


def test_the_file_round_trips(tmp_path):
    payload = posture_for()
    path = tmp_path / "weights.json"
    write(path, payload)
    assert read(path)["policy_fingerprint"] == "policy-abc"


def test_a_missing_file_refuses_with_its_own_code(tmp_path):
    with pytest.raises(WiringError) as raised:
        read(tmp_path / "nope.json")
    assert raised.value.code == "missing"


def test_a_foreign_schema_refuses(tmp_path):
    path = tmp_path / "weights.json"
    path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    with pytest.raises(WiringError) as raised:
        read(path)
    assert raised.value.code == "schema"


def test_arm_refuses_a_foreign_policy(tmp_path):
    path = tmp_path / "weights.json"
    write(path, posture_for(fingerprint="old-policy"))
    with pytest.raises(WiringError) as raised:
        load_for_arm(path, policy_fingerprint="new-policy", age_seconds=10.0,
                     max_age_seconds=600.0)
    assert raised.value.code == "fingerprint"


def test_arm_refuses_a_stale_posture(tmp_path):
    path = tmp_path / "weights.json"
    write(path, posture_for())
    with pytest.raises(WiringError) as raised:
        load_for_arm(path, policy_fingerprint="policy-abc", age_seconds=9999.0,
                     max_age_seconds=600.0)
    assert raised.value.code == "stale"


def test_arm_refuses_a_withdrawn_allocation(tmp_path):
    path = tmp_path / "weights.json"
    write(path, posture_for(detection=DIRTY))
    with pytest.raises(WiringError) as raised:
        load_for_arm(path, policy_fingerprint="policy-abc", age_seconds=1.0,
                     max_age_seconds=600.0)
    assert raised.value.code == "drifted"


def test_arm_returns_sizes_when_everything_lines_up(tmp_path):
    path = tmp_path / "weights.json"
    write(path, posture_for())
    sizes = sizes_for_arm(path, policy_fingerprint="policy-abc", age_seconds=5.0,
                          max_age_seconds=600.0)
    assert sizes and all(isinstance(v, float) for v in sizes.values())


def test_compare_measures_how_far_the_posture_moved():
    first = posture_for()
    second = build({"baseline": rows("t", [1.0] * 30), "no_markup": rows("t", [-2.0] * 30)},
                   budget=1000.0, policy_fingerprint="policy-abc",
                   as_of="2026-10-22T14:00:00Z", detection=CLEAN)
    moved = compare(first, second)
    assert moved["posture_changed"] is True
    assert moved["total_variation"] > 0


def test_render_names_the_policy_and_the_sizes():
    text = render(posture_for())
    assert "posture as of" in text and "baseline" in text
