from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from .evolution_ledger import list_evolution_candidates
from .learning_journal import verify_learning_journal


def evolution_main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect immutable Scorpion self-evolution research challengers and verify "
            "adaptive-learning lineage. This command cannot activate or deploy a candidate."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    candidates = subparsers.add_parser("candidates")
    candidates.add_argument("--db", default="scorpion-evolution.db")

    verify = subparsers.add_parser("verify-learning")
    verify.add_argument("--db", default="scorpion-learning.db")
    verify.add_argument("--ensemble-id", required=True)

    args = parser.parse_args()
    if args.command == "candidates":
        print(json.dumps(list_evolution_candidates(args.db), indent=2, sort_keys=True))
        return

    report = verify_learning_journal(args.db, args.ensemble_id)
    print(json.dumps(asdict(report), indent=2, sort_keys=True))
    if not report.valid:
        raise SystemExit(2)
