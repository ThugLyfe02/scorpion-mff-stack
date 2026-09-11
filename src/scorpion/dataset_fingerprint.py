from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .history_archive import HistoryArchive
from .provenance import canonical_json, fingerprint_file


@dataclass(frozen=True, slots=True)
class DatasetFingerprint:
    name: str
    sha256: str
    records: int
    bytes: int | None = None


def fingerprint_history_archive(
    archive: HistoryArchive,
    *,
    channel_ids: Iterable[str],
) -> DatasetFingerprint:
    channels = tuple(sorted(set(channel_ids)))
    digest = hashlib.sha256()
    records = 0
    with sqlite3.connect(archive.path) as db:
        db.row_factory = sqlite3.Row
        for channel_id in channels:
            rows = db.execute(
                """
                SELECT revision_id,message_id,guild_id,channel_id,author_id,source_ts_utc,
                       edited_ts_utc,referenced_message_id,content_sha256
                FROM historical_discord_messages
                WHERE channel_id=?
                ORDER BY source_ts_utc,message_id,revision_id
                """,
                (channel_id,),
            )
            for row in rows:
                digest.update(
                    canonical_json(
                        {
                            "revision_id": str(row["revision_id"]),
                            "message_id": str(row["message_id"]),
                            "guild_id": str(row["guild_id"]),
                            "channel_id": str(row["channel_id"]),
                            "author_id": str(row["author_id"]),
                            "source_ts_utc": str(row["source_ts_utc"]),
                            "edited_ts_utc": (
                                str(row["edited_ts_utc"])
                                if row["edited_ts_utc"] is not None
                                else None
                            ),
                            "referenced_message_id": (
                                str(row["referenced_message_id"])
                                if row["referenced_message_id"] is not None
                                else None
                            ),
                            "content_sha256": str(row["content_sha256"]),
                        }
                    ).encode("utf-8")
                )
                digest.update(b"\n")
                records += 1
    return DatasetFingerprint("discord_history", digest.hexdigest(), records)


def fingerprint_quote_jsonl(path: str | Path) -> DatasetFingerprint:
    file = fingerprint_file(path)
    records = sum(1 for line in Path(path).read_text().splitlines() if line.strip())
    return DatasetFingerprint("option_quote_tape", file.sha256, records, file.bytes)
