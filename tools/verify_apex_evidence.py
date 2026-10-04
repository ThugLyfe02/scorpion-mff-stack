"""Verify the bytes of a sealed Program A evidence pack; this grants no authority.

Successful verification means the retained artifacts match their manifest. It
does not rerun historical CI, certify a deployment, or turn incomplete evidence
into a passing baseline. Those distinctions remain explicit in summary.json.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path


def verify(repo: Path, manifest: Path) -> dict[str, object]:
    repo = repo.resolve()
    document = json.loads(manifest.read_text(encoding="utf-8"))
    if document.get("schema_version") != 1:
        raise ValueError("unsupported evidence manifest")
    entries = document.get("artifacts")
    if not isinstance(entries, list) or not entries:
        raise ValueError("evidence manifest has no artifacts")
    seen: set[str] = set()
    byte_count = 0
    for item in entries:
        relative = item["path"]
        if not isinstance(relative, str) or relative in seen:
            raise ValueError("invalid or duplicate artifact path")
        seen.add(relative)
        path = (repo / relative).resolve()
        if Path(relative).is_absolute() or repo not in path.parents:
            raise ValueError(f"artifact path escapes repository: {relative}")
        data = path.read_bytes()
        if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError(f"artifact integrity mismatch: {relative}")
        byte_count += len(data)
        if item.get("compression") == "gzip":
            data = gzip.decompress(data)
            if (
                len(data) != item["uncompressed_bytes"]
                or hashlib.sha256(data).hexdigest() != item["uncompressed_sha256"]
            ):
                raise ValueError(f"uncompressed artifact mismatch: {relative}")
        elif item.get("compression") is not None:
            raise ValueError(f"unsupported compression: {relative}")
        if item.get("format") == "json":
            json.loads(data)
    return {
        "artifact_integrity": "VERIFIED",
        "artifacts": len(entries),
        "bytes": byte_count,
        "baseline_gate": document["baseline_gate"],
        "limits": "Byte verification does not grant research promotion or execution authority.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest", type=Path, default=Path("docs/apex/program-a/manifest.json"))
    args = parser.parse_args()
    manifest = args.manifest if args.manifest.is_absolute() else args.repo / args.manifest
    try:
        result = verify(args.repo, manifest)
    except (OSError, ValueError, KeyError, TypeError, EOFError) as error:
        print(json.dumps({"artifact_integrity": "INVALID_EVIDENCE", "reason": str(error)}))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
