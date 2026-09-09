from __future__ import annotations

import argparse
import json

from .evolution_ledger import list_evolution_candidates


def evolution_main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect immutable Scorpion self-evolution research challengers. "
            "This command cannot activate or deploy a candidate."
        )
    )
    parser.add_argument("--db", default="scorpion-evolution.db")
    args = parser.parse_args()
    print(json.dumps(list_evolution_candidates(args.db), indent=2, sort_keys=True))
