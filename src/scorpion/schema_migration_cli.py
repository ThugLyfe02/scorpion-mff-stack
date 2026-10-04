from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .schema_migrations import apply_schema_migrations, verify_schema_migration_ledger


def migration_main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect or apply Scorpion's checksummed production schema migrations."
    )
    parser.add_argument("--db", required=True, help="Path to the production SQLite database")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply pending supported migrations transactionally before printing status",
    )
    parser.add_argument(
        "--application-id",
        default="scorpion-schema-migrate/0.27.0",
        help="Auditable identity recorded in the migration ledger",
    )
    args = parser.parse_args()
    path = Path(args.db)
    if args.apply:
        report = apply_schema_migrations(path, application_id=args.application_id)
    else:
        report = verify_schema_migration_ledger(path)
    print(json.dumps(asdict(report), sort_keys=True, default=str))
    return 0 if report.valid else 2


if __name__ == "__main__":
    raise SystemExit(migration_main())
