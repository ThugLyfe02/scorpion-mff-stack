"""The evidence manifest: a PR body that a reviewer can re-run.

These tests drive `scripts/fleet/evidence.sh` as a process, because that is how it is
used - a lane generates its manifest from a worktree. The properties under test are the
ones that make the artifact worth trusting:

- the manifest is machine-readable and names the command behind every row;
- an unrun gate is REFUSED (exit 2), never green: a skipped gate is not a gate that
  passed, and the recorder must not let a lane quote one as if it had;
- a tree that carries both packages is refused as two truths for one invariant;
- a claim that fails is recorded as a failure, not dropped;
- an unknown lane or a malformed claim is a usage error (exit 2), not an empty manifest.

Most tests run against a throwaway one-package git tree rather than this checkout, so
the exit code is decided by the property under test and not by whether the developer's
working tree happens to be dirty.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = REPO_ROOT / "scripts" / "fleet" / "evidence.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or not EVIDENCE.exists(),
    reason="the fleet recorder is a bash entry point",
)


def manifest(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(EVIDENCE), *args],
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )


def checks(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    rows = payload["checks"]
    assert isinstance(rows, list)
    return {row["check"]: row for row in rows}


def committed_tree(tmp_path: Path, package: str = "scorpion_mff") -> Path:
    """A throwaway lane tree with one package and one commit, so `dirty` is measured."""
    tree = tmp_path / "lane"
    (tree / package).mkdir(parents=True)
    (tree / package / "__init__.py").write_text("", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(tree), check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(tree), check=True)
    subprocess.run(
        ["git", "-c", "user.email=fleet@example.invalid", "-c", "user.name=fleet",
         "commit", "-qm", "one package"],
        cwd=str(tree),
        check=True,
    )
    return tree


def test_the_manifest_is_machine_readable_and_names_a_command_per_row():
    done = manifest("--json", "--no-gate")
    payload = json.loads(done.stdout)

    assert payload["schema"] == 1
    assert payload["exit"] == done.returncode
    rows = checks(payload)
    assert {"identity", "dirty", "drift", "purity.imports:scorpion_mff", "purity.tree"} <= set(rows)
    for name, row in rows.items():
        assert row["verdict"] in {"pass", "fail", "refused", "not_run"}
        assert str(row["command"]).strip(), f"{name} has no reproducing command"
        assert str(row["detail"]).strip(), f"{name} has no detail"


def test_an_unrun_gate_is_refused_rather_than_green(tmp_path):
    tree = committed_tree(tmp_path)
    done = manifest("--json", "--no-gate", cwd=tree)
    payload = json.loads(done.stdout)

    gate = checks(payload)["gate"]
    assert gate["verdict"] == "refused"
    assert "--no-gate" in str(gate["detail"])
    # Refusal is exit 2: the recorder declines to claim, it does not fail the code.
    assert done.returncode == 2
    # ...and the facts it could measure are still reported.
    assert checks(payload)["dirty"]["verdict"] == "pass"
    assert checks(payload)["purity.tree"]["verdict"] == "pass"


def test_a_failed_claim_is_recorded_as_a_failure(tmp_path):
    tree = committed_tree(tmp_path)
    done = manifest("--json", "--no-gate", "--claim", "sizing=exit 3", cwd=tree)
    payload = json.loads(done.stdout)

    assert checks(payload)["claim:sizing"]["verdict"] == "fail"
    assert done.returncode == 1


def test_a_claim_without_a_label_is_a_usage_error():
    done = manifest("--json", "--no-gate", "--claim", "no-equals-sign")
    assert done.returncode == 2
    assert "LABEL=COMMAND" in done.stderr


def test_an_unknown_lane_is_a_usage_error_not_an_empty_manifest():
    done = manifest("--json", "--no-gate", "--lane", "does-not-exist")
    assert done.returncode == 2
    assert "does-not-exist" in done.stderr


def test_two_packages_in_one_tree_are_refused_as_two_truths(tmp_path):
    """The merge gate DIRECTION.md names: a lane must not carry the other lane's copy.

    The architect lane reached exactly this state by merging `main` into the paper
    lane, so the check is proven against the smallest tree that reproduces it rather
    than against whichever worktrees happen to exist on the machine.
    """
    tree = committed_tree(tmp_path)
    (tree / "src" / "scorpion").mkdir(parents=True)
    (tree / "src" / "scorpion" / "__init__.py").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(tree), check=True)
    subprocess.run(
        ["git", "-c", "user.email=fleet@example.invalid", "-c", "user.name=fleet",
         "commit", "-qm", "the copy travels along"],
        cwd=str(tree),
        check=True,
    )

    done = manifest("--json", "--no-gate", cwd=tree)
    tree_row = checks(json.loads(done.stdout))["purity.tree"]
    assert tree_row["verdict"] == "fail"
    assert "two truths" in str(tree_row["detail"])
    assert done.returncode == 1
