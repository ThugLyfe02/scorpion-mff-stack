"""Emit the evidence manifest for one lane's tree: the PR body, measured.

`DIRECTION.md` makes the claim concrete: "reviewers do not merge prose, they merge a
diff they can re-run." So the manifest is not written, it is *emitted* - every row
names the command that produced it and the verdict that command returned. Three laws
shape the output:

1. **A row is a command.** No row exists that cannot be reproduced with one shell
   line. The markdown ends with the exact command list the manifest ran.
2. **Unmeasured is refused, never passed.** A skipped gate, an unresolvable base or a
   tree with no package at all is `refused` (exit 2), not `pass`. This is the same
   discipline the hot path applies to a stale quote: the absence of evidence is not
   evidence of a green tree.
3. **A dirty tree describes no commit.** A manifest generated over uncommitted work
   cannot be checked out by a reviewer, so it fails provenance rather than silently
   describing a tree that exists only on one machine.

Checks, in order:

  identity     branch, head sha, subject, dirty count
  lane         the branch (or detached tip) is the lane's declared branch
  drift        commits behind the base branch, against a review ceiling
  purity       no cross-lane import, in both directions; exactly one package
  gate         ruff -> mypy --strict -> pytest, per-stage verdicts and counts
  claims       `--claim LABEL=COMMAND` rows: the one command per claim, run

Exit codes: 0 every row passed; 1 a row failed; 2 something could not be measured
(or the arguments were wrong). Usage is documented in `scripts/fleet/evidence.sh`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = 1

# A lane owns exactly one package. Anything else in the tree is either the other
# lane's copy (two truths for one invariant) or a research archive that has not been
# harvested yet; both are review findings, so the tree check names them.
LANE_PACKAGES = {
    "main": "scorpion_mff",
    "architect": "src/scorpion",
    "semantic": "src/scorpion",
}
PACKAGE_ROOTS = ("scorpion_mff", "src/scorpion")

# `scorpion_mff` is the live lane, `scorpion` is the paper lane. An import in either
# direction is the seam DIRECTION.md forbids, and it is grep-able, so it is gated.
IMPORT_RULES = (
    ("scorpion_mff", r"^\s*(from|import)\s+scorpion(\.|\s|$)"),
    ("src", r"^\s*(from|import)\s+scorpion_mff(\.|\s|$)"),
)


@dataclass
class Row:
    """One measured fact: what was checked, what the command said, and its verdict."""

    check: str
    verdict: str
    detail: str
    command: str = ""
    data: dict[str, object] = field(default_factory=dict)


class Refused(Exception):
    """A measurement that could not be taken. Never converted into a pass."""


def run(command: list[str], cwd: Path, timeout: int = 1200) -> tuple[int, str]:
    """Run a command, returning its exit status and combined output.

    A missing binary is a refusal (`FileNotFoundError`), not an empty success: the
    manifest must not read "0 hits" for a `git grep` that never ran.
    """
    try:
        done = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise Refused(f"{command[0]} is not installed: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise Refused(f"{shlex.join(command)} exceeded {timeout}s") from exc
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def git(root: Path, *args: str) -> tuple[int, str]:
    # `cwd=root` rather than `git -C`: the manifest prints the command it ran, and a
    # reviewer must be able to paste it at the tree root instead of at one machine's
    # absolute path.
    return run(["git", "-c", "core.quotepath=false", *args], cwd=root)


def git_out(root: Path, *args: str) -> str:
    status, output = git(root, *args)
    if status != 0:
        raise Refused(f"git {shlex.join(args)}: {output.strip() or f'exit {status}'}")
    return output.strip()


def fleet_path(given: str | None) -> Path:
    """The lane registry: `--fleet`, else the orchestration tree that holds this script.

    The lane tree is never the source of `fleet.json` - the registry lives on the
    default branch, so a stale worktree cannot redefine which branch it is supposed
    to be.
    """
    if given:
        return Path(given)
    return Path(__file__).resolve().parents[2] / "fleet.json"


def rerun_command(lane: str | None) -> str:
    # The reproducing command for the checks this script performs in-process: running
    # the manifest again re-emits the same row. No hidden tool, no invented verb.
    return "scripts/fleet/evidence.sh" + (f" --lane {lane}" if lane else "")


def quote(command: list[str]) -> str:
    return shlex.join(command)


# ── identity ─────────────────────────────────────────────────────────────────
def check_identity(root: Path) -> list[Row]:
    branch_cmd = ["git", "rev-parse", "--abbrev-ref", "HEAD"]
    branch = git_out(root, "rev-parse", "--abbrev-ref", "HEAD")
    sha = git_out(root, "rev-parse", "HEAD")
    subject = git_out(root, "log", "-1", "--format=%s")
    short = git_out(root, "rev-parse", "--short", "HEAD")

    status, porcelain = git(root, "status", "--porcelain")
    if status != 0:
        raise Refused(f"git status: {porcelain.strip()}")
    dirty = [line for line in porcelain.splitlines() if line.strip()]

    rows: list[Row] = [
        Row(
            check="identity",
            verdict="pass",
            detail=f"`{short}` on `{branch}`",
            command=quote(branch_cmd),
            data={
                "branch": branch,
                "sha": sha,
                "short": short,
                "subject": subject,
            },
        )
    ]
    # A manifest over uncommitted work cannot be reproduced from a commit, which is
    # the only artifact a reviewer can check out. Name the count, never "clean".
    rows.append(
        Row(
            check="dirty",
            verdict="pass" if not dirty else "fail",
            detail=(
                "0 uncommitted files: the manifest describes a commit"
                if not dirty
                else f"{len(dirty)} uncommitted file(s) - this manifest describes a "
                "working tree, not a commit"
            ),
            command=quote(["git", "status", "--porcelain"]),
            data={"count": len(dirty), "files": [line[3:] for line in dirty[:5]]},
        )
    )
    return rows


def check_lane(root: Path, lane: str | None, fleet_file: Path) -> list[Row]:
    """The manifest must describe the lane whose PR it is pasted onto."""
    if lane is None:
        return []
    command = rerun_command(lane)
    try:
        with open(fleet_file, encoding="utf-8") as handle:
            fleet = json.load(handle)
        declared = next(item for item in fleet["lanes"] if item["id"] == lane)
    except (OSError, KeyError, StopIteration, json.JSONDecodeError) as exc:
        raise Refused(f"cannot read lane {lane!r} from {fleet_file}: {exc}") from exc

    expected_branch = str(declared["branch"])
    branch = git_out(root, "rev-parse", "--abbrev-ref", "HEAD")

    if branch == expected_branch:
        return [
            Row(
                check="lane",
                verdict="pass",
                detail=f"`{branch}` is the `{lane}` lane's declared branch",
                command=command,
            )
        ]

    # A lane worktree is checked out detached at the fetched tip (`run-lane.sh`), so
    # the branch name is `HEAD` there. Compare the tip instead of failing the lane
    # for the checkout strategy.
    if branch == "HEAD":
        head = git_out(root, "rev-parse", "HEAD")
        status, tip = git(root, "rev-parse", f"refs/remotes/origin/{expected_branch}")
        if status == 0 and tip.strip() == head:
            return [
                Row(
                    check="lane",
                    verdict="pass",
                    detail=f"detached HEAD is the tip of `origin/{expected_branch}`",
                    command=command,
                )
            ]
    return [
        Row(
            check="lane",
            verdict="fail",
            detail=(
                f"tree is on `{branch}`, but the `{lane}` lane declares "
                f"`{expected_branch}`: this manifest does not describe that PR"
            ),
            command=command,
        )
    ]


def check_drift(root: Path, base: str | None, max_behind: int) -> list[Row]:
    # Currency is measured against the remote base: a stale local `main` would make a
    # converged lane look current, which is the exact failure DIRECTION.md calls a
    # correctness risk rather than a tidiness issue.
    wanted = base or "main"
    candidates = [wanted] if "/" in wanted else [f"origin/{wanted}", wanted]

    resolved = None
    for candidate in candidates:
        status, _ = git(root, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}")
        if status == 0:
            resolved = candidate
            break

    command = ["git", "rev-list", "--left-right", "--count", f"{resolved or wanted}...HEAD"]
    if resolved is None:
        return [
            Row(
                check="drift",
                verdict="refused",
                detail=(
                    f"base {wanted!r} does not resolve in this clone, so drift cannot be "
                    "measured - fetch the base before claiming currency"
                ),
                command=quote(command),
            )
        ]

    output = git_out(root, "rev-list", "--left-right", "--count", f"{resolved}...HEAD")
    behind, ahead = (int(part) for part in output.split())
    return [
        Row(
            check="drift",
            verdict="pass" if behind <= max_behind else "fail",
            detail=(
                f"{ahead} ahead, {behind} behind `{resolved}` "
                f"(review ceiling: {max_behind} behind)"
                + ("" if behind <= max_behind else " - converge before review")
            ),
            command=quote(command),
            data={"ahead": ahead, "behind": behind, "base": resolved, "ceiling": max_behind},
        )
    ]


# ── purity ───────────────────────────────────────────────────────────────────
def check_imports(root: Path) -> list[Row]:
    rows: list[Row] = []
    for tree, pattern in IMPORT_RULES:
        command = ["git", "grep", "-n", "-E", pattern, "--", tree]
        if not (root / tree).is_dir():
            rows.append(
                Row(
                    check=f"purity.imports:{tree}",
                    verdict="pass",
                    detail=f"No `{tree}` tree in this lane: no import seam to police",
                    command=quote(command),
                )
            )
            continue
        status, output = git(root, "grep", "-n", "-E", pattern, "--", tree)
        if status not in (0, 1):
            raise Refused(f"git grep over {tree}: {output.strip() or f'exit {status}'}")
        hits = [line for line in output.splitlines() if line.strip()] if status == 0 else []
        rows.append(
            Row(
                check=f"purity.imports:{tree}",
                verdict="pass" if not hits else "fail",
                detail=(
                    f"0 cross-lane imports in `{tree}`"
                    if not hits
                    else f"{len(hits)} cross-lane import(s) in `{tree}`: " + "; ".join(hits[:3])
                ),
                command=quote(command),
                data={"hits": len(hits)},
            )
        )
    return rows


def check_tree(root: Path, lane: str | None) -> list[Row]:
    present = [name for name in PACKAGE_ROOTS if (root / name).is_dir()]
    command = rerun_command(lane)
    expected = LANE_PACKAGES.get(lane) if lane else None

    if len(present) == 2:
        return [
            Row(
                check="purity.tree",
                verdict="fail",
                detail=(
                    "both `scorpion_mff` and `src/scorpion` exist in one tree: two "
                    "truths for one invariant - the copies must not travel together"
                ),
                command=command,
                data={"packages": present},
            )
        ]
    if not present:
        return [
            Row(
                check="purity.tree",
                verdict="refused",
                detail=(
                    "neither `scorpion_mff` nor `src/scorpion` exists: the manifest "
                    "cannot name which lane's package this tree is"
                ),
                command=command,
                data={"packages": present},
            )
        ]

    found = present[0]
    if expected is not None and found != expected:
        return [
            Row(
                check="purity.tree",
                verdict="fail",
                detail=(
                    f"the `{lane}` lane owns `{expected}` but this tree carries "
                    f"`{found}` - the other lane's copy"
                ),
                command=command,
                data={"packages": present, "expected": expected},
            )
        ]
    return [
        Row(
            check="purity.tree",
            verdict="pass",
            detail=f"exactly one package, `{found}`"
            + ("" if expected else " (no lane declared; ownership not asserted)"),
            command=command,
            data={"packages": present, "expected": expected},
        )
    ]


# ── the gate ─────────────────────────────────────────────────────────────────
STAGE_MARKER = re.compile(r"^==\s*(.+?)\s*==\s*$", re.MULTILINE)
PYTEST_SUMMARY = re.compile(
    r"(\d+) (passed|failed|skipped|error|errors|xfailed|xpassed|deselected|warning)"
)
MYPY_FILES = re.compile(r"no issues found in (\d+) source files")


def split_stages(log: str) -> dict[str, str]:
    marks = list(STAGE_MARKER.finditer(log))
    stages: dict[str, str] = {}
    for index, match in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(log)
        stages[match.group(1).lower()] = log[match.end() : end]
    return stages


COLLECTED_SUMMARY = re.compile(r"(\d+) tests? collected")
COLLECTED_PER_FILE = re.compile(r"^(\S+?): (\d+)$", re.MULTILINE)


def lane_python(root: Path) -> str:
    """The interpreter the lane's gate would use, as a tree-relative command word.

    `scripts/gate.sh` picks `.venv` when present and falls back to `python` on PATH.
    The manifest names the same one so its count and the gate's verdict describe one
    environment.
    """
    for relative in (".venv/Scripts/python.exe", ".venv/bin/python"):
        if (root / relative).exists():
            return relative
    return "python"


def check_collected(root: Path, gate_rows: list[Row]) -> list[Row]:
    """Count collected tests when the gate log does not carry a pytest summary.

    `scripts/gate.sh` runs `pytest -q` on top of `addopts = -q`. Two `-q` put pytest
    at verbosity -2, where it prints no "N passed in Ts" line at all - so the gate log
    alone cannot be quoted for the headline number. Collect instead of re-running the
    suite: it takes seconds, it cannot flake, and "N collected" is not a weaker claim
    than "N passed" in practice, because the gate row already states the pass.
    """
    pytest_row = next((row for row in gate_rows if row.check == "gate.pytest"), None)
    # Skip only when the log really carried the count. A stage can be `pass` by exit
    # status while printing no summary at all (verbosity -2), and a pass without a
    # number is exactly the row this fallback exists to fill.
    if pytest_row is not None and PYTEST_SUMMARY.search(pytest_row.detail):
        return []

    python = lane_python(root)
    command = f"{python} -m pytest --collect-only -q"
    try:
        status, output = run(
            [python, "-m", "pytest", "--collect-only", "-q"], cwd=root, timeout=600
        )
    except Refused as exc:
        return [Row(check="tests.collected", verdict="refused", detail=str(exc), command=command)]
    if status != 0:
        return [
            Row(
                check="tests.collected",
                verdict="refused",
                detail=f"collection exited {status}: no test count can be asserted",
                command=command,
            )
        ]

    summary = COLLECTED_SUMMARY.search(output)
    per_file = COLLECTED_PER_FILE.findall(output)
    total = int(summary.group(1)) if summary else sum(int(count) for _, count in per_file)
    if not total:
        return [
            Row(
                check="tests.collected",
                verdict="refused",
                detail=(
                    "collection reported no tests: a suite that collects nothing proves nothing"
                ),
                command=command,
            )
        ]
    return [
        Row(
            check="tests.collected",
            verdict="pass",
            detail=f"{total} tests collected across {len(per_file)} files",
            command=command,
            data={"tests": total, "files": len(per_file)},
        )
    ]


def stage_verdict(name: str, block: str) -> Row:
    if name.startswith("ruff"):
        passed = "All checks passed" in block
        found = re.search(r"Found (\d+) error", block)
        verdict = "pass" if passed else ("fail" if found else "not_run")
        detail = "All checks passed" if passed else (found.group(0) if found else "no result")
    elif name.startswith("mypy"):
        files = MYPY_FILES.search(block)
        found = re.search(r"Found (\d+) error", block)
        verdict = "pass" if files else ("fail" if found else "not_run")
        detail = (
            f"no issues found in {files.group(1)} source files"
            if files
            else (found.group(0) if found else "no result")
        )
    elif name.startswith("pytest"):
        counts = {kind: int(n) for n, kind in PYTEST_SUMMARY.findall(block)}
        bad = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
        verdict = "pass" if counts and not bad else ("fail" if counts else "not_run")
        detail = (
            ", ".join(f"{int(n)} {kind}" for n, kind in PYTEST_SUMMARY.findall(block))
            or "no summary line"
        )
    else:
        verdict = "pass" if "clean" in block else "not_run"
        detail = block.strip().splitlines()[-1] if block.strip() else "no result"
    return Row(check=f"gate.{name}", verdict=verdict, detail=detail)


def check_gate(
    root: Path,
    gate: str,
    gate_log: Path | None,
    gate_exit: int | None,
    timeout: int,
    no_gate: bool = False,
) -> list[Row]:
    if no_gate:
        # A skipped gate is refused, never passed: the manifest exists to stop a
        # green story about an unmeasured tree. `--gate-log` is the honest fast path.
        return [
            Row(
                check="gate",
                verdict="refused",
                detail="gate not run (--no-gate): a tree without its gate is not evidence",
                command=f"bash {gate}",
            )
        ]
    reused = False
    if gate_log is not None:
        # The reproducible command is still the gate itself: the log is one run of it,
        # and a reviewer should re-run the gate rather than trust a saved file.
        command = f"bash {gate}"
        reused = True
        try:
            log = gate_log.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise Refused(f"cannot read {gate_log}: {exc}") from exc
        # Without an explicit status, the gate's own last word decides. Anything
        # else (a truncated log, a gate that died) must not read as green.
        if gate_exit is None:
            gate_exit = 0 if "gates: all three clean" in log else 1
    else:
        command = f"bash {gate}"
        status, log = run(["bash", gate], cwd=root, timeout=timeout)
        gate_exit = status

    detail = (
        "exit 0 - ruff, mypy --strict and pytest all clean"
        if gate_exit == 0
        else f"exit {gate_exit}: the lane cannot ship this tree"
    )
    if reused:
        detail = f"gate log reused from this run: {detail}"
    rows = [
        Row(
            check="gate",
            verdict="pass" if gate_exit == 0 else "fail",
            detail=detail,
            command=command,
            data={"exit": gate_exit, "reused_log": reused},
        )
    ]
    stages = split_stages(log)
    if not stages:
        rows.append(
            Row(
                check="gate.output",
                verdict="refused",
                detail="the gate printed no `== stage ==` markers, so no stage can be named",
                command=command,
            )
        )
        return rows
    order = ["ruff", "mypy", "pytest"]
    for name in sorted(
        stages,
        key=lambda item: next(
            (index for index, stem in enumerate(order) if item.startswith(stem)), len(order)
        ),
    ):
        row = stage_verdict(name, stages[name])
        row.command = command
        # The gate runs under `set -e`, so exit 0 means every stage ran, even when a
        # stage printed no machine-readable verdict (pytest at `-q -q` prints no
        # summary line). Stay silent about what was seen, not about what happened:
        # "not run" would be a false refusal, and the count is measured separately.
        if row.verdict == "not_run" and gate_exit == 0:
            row.verdict = "pass"
            row.detail = (
                "clean by exit status; the stage printed no summary line "
                "(count measured by `tests.collected`)"
            )
        rows.append(row)
    return rows


# ── claims ───────────────────────────────────────────────────────────────────
def check_claims(root: Path, claims: list[str]) -> list[Row]:
    rows: list[Row] = []
    for claim in claims:
        label, sep, command = claim.partition("=")
        if not sep or not label.strip() or not command.strip():
            raise Refused(f"--claim wants LABEL=COMMAND, got {claim!r}")
        try:
            status, output = run(["bash", "-c", command], cwd=root)
        except Refused as exc:
            rows.append(
                Row(
                    check=f"claim:{label.strip()}",
                    verdict="refused",
                    detail=str(exc),
                    command=command.strip(),
                )
            )
            continue
        tail = [line for line in output.strip().splitlines() if line.strip()]
        rows.append(
            Row(
                check=f"claim:{label.strip()}",
                verdict="pass" if status == 0 else "fail",
                detail=(tail[-1][:300] if tail else "no output")
                if status
                else (tail[-1][:300] if tail else "exit 0"),
                command=command.strip(),
                data={"exit": status},
            )
        )
    return rows


# ── rendering ────────────────────────────────────────────────────────────────
def exit_code(rows: list[Row]) -> int:
    verdicts = {row.verdict for row in rows}
    if "fail" in verdicts:
        return 1
    if "refused" in verdicts:
        return 2
    return 0


def counts(rows: list[Row]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        if row.check == "gate.pytest":
            for number, kind in PYTEST_SUMMARY.findall(row.detail):
                if kind != "warning":
                    out[f"tests_{kind}"] = out.get(f"tests_{kind}", 0) + int(number)
        if row.check == "tests.collected" and "tests" in row.data:
            out["tests_collected"] = int(row.data["tests"])  # type: ignore[arg-type]
        if row.check == "gate.mypy":
            files = MYPY_FILES.search(row.detail)
            if files:
                out["mypy_files"] = int(files.group(1))
    return out


def as_json(rows: list[Row], root: Path, lane: str | None) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "root": str(root),
        "lane": lane,
        "exit": exit_code(rows),
        "counts": counts(rows),
        "checks": [
            {
                "check": row.check,
                "verdict": row.verdict,
                "detail": row.detail,
                "command": row.command,
                **({"data": row.data} if row.data else {}),
            }
            for row in rows
        ],
    }


def as_markdown(rows: list[Row], root: Path, lane: str | None, manifest: dict[str, object]) -> str:
    mark = {"pass": "PASS", "fail": "FAIL", "refused": "REFUSED", "not_run": "NOT RUN"}
    head = next((row for row in rows if row.check == "identity"), None)
    lines = [
        "## Evidence manifest",
        "",
        f"Generated {manifest['generated_at']} by `scripts/fleet/evidence.sh`"
        + (f" for the `{lane}` lane." if lane else ".")
        + (f" Tree: `{root}`." if not lane else ""),
    ]
    if head is not None:
        short = head.data.get("short", "")
        subject = head.data.get("subject", "")
        lines += ["", f"**Commit:** `{short}` - {subject}"]
    lines.append("")

    lines += ["| check | verdict | detail |", "|---|---|---|"]
    for row in rows:
        detail = row.detail.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| `{row.check}` | {mark.get(row.verdict, row.verdict)} | {detail} |")

    if manifest.get("counts"):
        counts_row = ", ".join(f"{key}={value}" for key, value in manifest["counts"].items())
        lines += ["", f"**Counts:** {counts_row}"]

    commands = []
    for row in rows:
        if row.command and row.command not in commands:
            commands.append(row.command)
    if commands:
        lines += [
            "",
            "### Commands",
            "",
            "Every row above is reproducible; these are the commands behind them:",
            "",
        ]
        lines += [f"    {command}" for command in commands]

    verdict = exit_code(rows)
    note = {0: "all rows measured and green", 1: "a row failed", 2: "a row could not be measured"}
    lines += ["", f"Manifest exit `{verdict}` - {note[verdict]}.", ""]
    return "\n".join(lines)


# ── entry point ──────────────────────────────────────────────────────────────
def build(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    if not (root / ".git").exists():
        raise Refused(f"{root} is not a git tree: there is nothing to describe")

    fleet_file = fleet_path(args.fleet)
    rows: list[Row] = []
    rows += check_identity(root)
    rows += check_lane(root, args.lane, fleet_file)
    rows += check_drift(root, args.base, args.max_behind)
    rows += check_imports(root)
    rows += check_tree(root, args.lane)
    gate_rows = check_gate(
        root, args.gate, args.gate_log, args.gate_exit, args.timeout, args.no_gate
    )
    rows += gate_rows
    rows += check_collected(root, gate_rows)
    rows += check_claims(root, args.claim)

    manifest = as_json(rows, root, args.lane)
    text = (
        json.dumps(manifest, indent=2) + "\n"
        if args.json
        else as_markdown(rows, root, args.lane, manifest)
    )

    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        if not args.quiet:
            sys.stderr.write(f"manifest: {args.out}\n")
    else:
        sys.stdout.write(text)
    return exit_code(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="evidence.sh",
        description="Emit a lane's evidence manifest (PR body) from its own tree.",
    )
    parser.add_argument("--root", default=".", help="the tree to describe (default: cwd)")
    parser.add_argument("--lane", default=None, help="lane id from fleet.json")
    parser.add_argument("--fleet", default=None, help="path to fleet.json")
    parser.add_argument("--base", default=None, help="base ref (default: fleet.json base_ref)")
    parser.add_argument(
        "--max-behind",
        type=int,
        default=int(os.environ.get("FLEET_MAX_BEHIND", "10")),
        help="review ceiling: commits behind the base (default 10)",
    )
    parser.add_argument("--gate", default="scripts/gate.sh", help="the lane's gate script")
    parser.add_argument(
        "--gate-log",
        default=None,
        help="describe a gate run that already happened instead of running it again",
    )
    parser.add_argument(
        "--gate-exit", type=int, default=None, help="exit status of the run in --gate-log"
    )
    parser.add_argument("--no-gate", action="store_true", help="skip the gate (it is then REFUSED)")
    parser.add_argument("--timeout", type=int, default=1800, help="gate timeout in seconds")
    parser.add_argument(
        "--claim",
        action="append",
        default=[],
        metavar="LABEL=COMMAND",
        help="run one claim's command and record its verdict (repeatable)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable manifest")
    parser.add_argument("--out", default=None, help="write to this file instead of stdout")
    parser.add_argument("--quiet", action="store_true", help="do not report the output path")
    args = parser.parse_args(argv)

    if args.base is None:
        try:
            with open(fleet_path(args.fleet), encoding="utf-8") as handle:
                args.base = json.load(handle).get("base_ref")
        except (OSError, json.JSONDecodeError):
            args.base = None

    if args.no_gate:
        # ...and the honest fast path is --gate-log: describe the run that happened.
        args.gate_log = None
        args.gate_exit = None

    try:
        return build(args)
    except Refused as exc:
        sys.stderr.write(f"evidence: refused: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
