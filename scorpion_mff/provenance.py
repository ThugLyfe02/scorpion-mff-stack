"""Input authenticity: who may speak, and whether the words are still the words.

`RawMessage` carries a channel name, text and a message id - and nothing about the
author, no edit, no delete, no reply context. The Gateway only ever yielded
`MESSAGE_CREATE`. So a webhook, a renamed user, or an edit that changes a strike between
the decision and the send had no way to be refused before it became an order. This
module is the evidence layer in front of the parser:

* **Authority is declared, never inferred.** `AuthorPolicy` reads `live/authority.json`:
  an allowlist of author ids with a trust tier, an optional required channel set, and a
  deliberate flag for trusting authors the allowlist has never seen. A missing or
  malformed authority file fails closed: no author is trusted and every message is
  refused with a named reason.
* **The content is fingerprinted.** `MessageEnvelope.content_hash` is a SHA-256 over the
  NFC-normalised text plus the message's identity fields. An edit that changes `720` to
  `725` changes the hash; a bare CR/LF or double space does not.
* **Edit and delete can abort a decision.** `ProvenanceFilter` drops non-allowed
  messages before parsing, returns `REVOKE` for an edit/delete of an already-actioned
  message, and `verify_before_send` recomputes the hash immediately before the broker
  call so a not-yet-sent order can be withdrawn.
* **The journal is the audit trail.** `MessageJournal` is append-only JSONL of
  admissions, refusals, decisions, edits, deletes and revocations. A torn final line is
  tolerated (a crash mid-append); corruption anywhere earlier is refused, because a
  journal that silently loses an admission is worse than one that refuses to start.

Ordering inside `ProvenanceGuard.admit` is deliberate: malformed evidence, deleted,
authority, channel, then edit/duplicate. A message that is not allowed is never recorded
as admitted, so a policy fix cannot resurrect an order that was already dropped; and a
deletion is checked before the allowlist so a revoked message is named as revoked even
if the author was also removed.

Normalisation (documented because it defines the fingerprint):
  * Unicode NFKC, so full-width digits and ligatures compare equal to their ASCII forms;
  * zero-width characters removed (they are invisible edit markers, not content);
  * CRLF/CR collapsed to LF;
  * every run of whitespace collapsed to one space and the ends trimmed.
Case is preserved: a change of case is a change to the fingerprint and therefore a
refusal, which is the safe direction (the parser normalises tickers separately).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

V = 1
AUTHORITY_SCHEMA = "mff.authority.v1"
JOURNAL_SCHEMA = "mff.provenance-journal.v1"
DEFAULT_AUTHORITY_PATH = Path("live/authority.json")

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff"), None)


class TrustTier(StrEnum):
    """What an author id is allowed to do. UNKNOWN is a refusal, never a default allow."""

    TRUSTED = "TRUSTED"
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"


class Refusal(StrEnum):
    """Every named reason a message may be dropped or a decision revoked."""

    AUTHOR_NOT_ALLOWED = "author_not_allowed"
    AUTHOR_UNKNOWN = "author_unknown"
    CHANNEL_NOT_ALLOWED = "channel_not_allowed"
    EDITED_AFTER_DECISION = "edited_after_decision"
    DELETED = "deleted"
    CONTENT_CHANGED = "content_changed"
    DUPLICATE = "duplicate"
    MALFORMED_ENVELOPE = "malformed_envelope"


class ErrorReason(StrEnum):
    """Named failures of the *configuration* and *journal*, not of a message."""

    AUTHORITY_MISSING = "authority_missing"
    AUTHORITY_MALFORMED = "authority_malformed"
    JOURNAL_CORRUPT = "journal_corrupt"
    JOURNAL_UNREADABLE = "journal_unreadable"
    VALUE_INVALID = "value_invalid"


_REFUSAL_VALUES = frozenset(reason.value for reason in Refusal)
_REVOKE_VALUES = frozenset(
    {
        Refusal.CONTENT_CHANGED.value,
        Refusal.EDITED_AFTER_DECISION.value,
        Refusal.DELETED.value,
    }
)

KIND_ADMIT = "admit"
KIND_DECISION = "decision"
KIND_EDIT = "edit"
KIND_DELETE = "delete"
KIND_REVOKE = "revoke"
KIND_VERIFY = "verify"
_KINDS = frozenset({KIND_ADMIT, KIND_DECISION, KIND_EDIT, KIND_DELETE, KIND_REVOKE, KIND_VERIFY})
_ACTIONS = frozenset({"", "accept", "drop", "revoke"})


class ProvenanceError(RuntimeError):
    """A configuration or journal that cannot be trusted. Fail closed."""

    def __init__(self, message: str, *, reason: ErrorReason) -> None:
        super().__init__(message)
        self.reason = reason


class AuthorityError(ProvenanceError):
    """`authority.json` is missing, unreadable or not a policy."""


# ── normalisation and the fingerprint ───────────────────────────────────────
def normalize_content(text: str) -> str:
    """Normalise message text deterministically. The definition of "same post".

    NFKC folds compatibility forms; zero-width characters are deleted rather than
    collapsed; line endings become LF; all whitespace runs collapse to one space.
    The result is stable across platforms and across Discord's HTML-entity round trips
    for the characters that matter here. Case is deliberately preserved.
    """
    if not isinstance(text, str):
        raise ProvenanceError(
            f"content must be a string, got {type(text).__name__}", reason=ErrorReason.VALUE_INVALID
        )
    normal = unicodedata.normalize("NFKC", text)
    normal = normal.translate(_ZERO_WIDTH)
    normal = normal.replace("\r\n", "\n").replace("\r", "\n")
    return " ".join(normal.split())


def content_hash_for(content: str, *, message_id: str = "", reference_message_id: str = "") -> str:
    """SHA-256 over the normalised text and the identity fields that shape the order.

    `message_id` makes two identical posts distinct; `reference_message_id` is included
    because a reply's context is part of what the message means. Author and channel are
    not in the digest: a display-name change must not read as an edited order, and the
    channel is already checked by the authority policy.
    """
    canonical = json.dumps(
        {
            "content": normalize_content(content),
            "message_id": str(message_id),
            "reference_message_id": str(reference_message_id),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _timestamp(value: object) -> float | None:
    """Epoch seconds from an epoch number or an ISO-8601 string. None when unreadable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            try:
                return float(text)
            except ValueError:
                return None
    return None


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


# ── the envelope ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class MessageEnvelope:
    """One Discord message event, with the evidence needed to trust it.

    The hash is computed at construction when the caller does not supply one, so the
    only way to present a different `content_hash` is to tamper with the envelope -
    which `ProvenanceGuard` refuses. A malformed envelope (empty ids, empty content,
    wrong types) is left constructible on purpose: the guard names the defect in a
    refusal instead of a constructor exception that the listener might not expect.
    """

    message_id: str
    channel_id: str
    author_id: str
    author_name: str = ""
    content: str = ""
    edited_at: float | None = None
    deleted: bool = False
    reference_message_id: str = ""
    content_hash: str = ""
    event_type: str = "MESSAGE_CREATE"

    def __post_init__(self) -> None:
        if not self.content_hash and isinstance(self.content, str):
            object.__setattr__(
                self,
                "content_hash",
                content_hash_for(
                    self.content,
                    message_id=self.message_id,
                    reference_message_id=self.reference_message_id,
                ),
            )

    @property
    def is_edit(self) -> bool:
        return self.event_type == "MESSAGE_UPDATE" or self.edited_at is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "channel_id": self.channel_id,
            "author_id": self.author_id,
            "author_name": self.author_name,
            "content": self.content,
            "edited_at": self.edited_at,
            "deleted": self.deleted,
            "reference_message_id": self.reference_message_id,
            "content_hash": self.content_hash,
            "event_type": self.event_type,
        }

    @classmethod
    def from_discord_payload(
        cls, payload: object, *, event_type: str = "MESSAGE_CREATE"
    ) -> MessageEnvelope:
        """Build an envelope from a raw Discord payload dict.

        The Gateway must pass the raw author/reference objects through for this to be
        meaningful; a payload without an author id builds an envelope the guard refuses
        as `malformed_envelope`, which is the correct answer for an unauthenticated
        message. A MESSAGE_DELETE payload has no content: the envelope is marked
        deleted instead of inventing text.
        """
        if not isinstance(payload, Mapping):
            raise ProvenanceError(
                f"discord payload must be an object, got {type(payload).__name__}",
                reason=ErrorReason.AUTHORITY_MALFORMED,
            )
        author = payload.get("author")
        author_map = author if isinstance(author, Mapping) else {}
        reference = payload.get("message_reference")
        reference_map = reference if isinstance(reference, Mapping) else {}
        referenced = payload.get("referenced_message")
        referenced_map = referenced if isinstance(referenced, Mapping) else {}
        reference_id = str(
            reference_map.get("message_id")
            or reference_map.get("id")
            or referenced_map.get("id")
            or ""
        )
        kind = str(event_type or "MESSAGE_CREATE").strip().upper()
        return cls(
            message_id=str(payload.get("id") or ""),
            channel_id=str(payload.get("channel_id") or ""),
            author_id=str(author_map.get("id") or ""),
            author_name=str(author_map.get("global_name") or author_map.get("username") or ""),
            content=str(payload.get("content") or ""),
            edited_at=_timestamp(payload.get("edited_timestamp")),
            deleted=kind == "MESSAGE_DELETE",
            reference_message_id=reference_id,
            event_type=kind,
        )


# ── authority ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class AuthorPolicy:
    """The declared answer to "who is allowed to place orders in this channel?".

    `authors` maps an author id to its tier. Two refusals are distinct on purpose:
    an author *absent* from the mapping is `author_unknown` (the policy has never heard
    of them), while an author present with tier `UNKNOWN` is `author_not_allowed` (the
    policy knows exactly who they are and says no). `trusted_bypass_unknown` is the one
    deliberate escape hatch: when set, an author the mapping has never seen is treated as
    TRUSTED for the author check *only* - every other refusal (deleted, edited, channel,
    duplicate) still applies. It is off by default and must be an explicit operator act.

    `channels` is the optional required channel set; empty means any channel.
    """

    authors: Mapping[str, TrustTier] = field(default_factory=dict)
    channels: frozenset[str] = frozenset()
    trusted_bypass_unknown: bool = False
    version: int = 1
    schema: str = AUTHORITY_SCHEMA

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version != 1:
            raise AuthorityError(
                f"authority version must be 1, got {self.version!r}",
                reason=ErrorReason.AUTHORITY_MALFORMED,
            )
        if not isinstance(self.authors, Mapping):
            raise AuthorityError(
                "authority authors must be an object", reason=ErrorReason.AUTHORITY_MALFORMED
            )
        normalised: dict[str, TrustTier] = {}
        for author_id, tier in self.authors.items():
            if not isinstance(author_id, str) or not author_id.strip():
                raise AuthorityError(
                    f"authority author id {author_id!r} is not a non-empty string",
                    reason=ErrorReason.AUTHORITY_MALFORMED,
                )
            normalised[author_id] = self._tier(tier, author_id)
        object.__setattr__(self, "authors", normalised)
        if not isinstance(self.channels, (frozenset, set, list, tuple)):
            raise AuthorityError(
                "authority channels must be an array of channel ids",
                reason=ErrorReason.AUTHORITY_MALFORMED,
            )
        channels = frozenset(self.channels)
        for channel_id in channels:
            if not isinstance(channel_id, str) or not channel_id.strip():
                raise AuthorityError(
                    f"authority channel id {channel_id!r} is not a non-empty string",
                    reason=ErrorReason.AUTHORITY_MALFORMED,
                )
        object.__setattr__(self, "channels", channels)
        if not isinstance(self.trusted_bypass_unknown, bool):
            raise AuthorityError(
                "trusted_bypass_unknown must be a boolean", reason=ErrorReason.AUTHORITY_MALFORMED
            )
        if not isinstance(self.schema, str):
            raise AuthorityError(
                "authority schema must be a string", reason=ErrorReason.AUTHORITY_MALFORMED
            )

    @staticmethod
    def _tier(value: object, author_id: str) -> TrustTier:
        if isinstance(value, TrustTier):
            return value
        if isinstance(value, str):
            try:
                return TrustTier(value.strip().upper())
            except ValueError:
                pass
        raise AuthorityError(
            f"authority tier for {author_id!r} must be TRUSTED, KNOWN or UNKNOWN, got {value!r}",
            reason=ErrorReason.AUTHORITY_MALFORMED,
        )

    @property
    def configured(self) -> bool:
        """A policy with no authors trusts nobody unless the bypass is declared."""
        return bool(self.authors) or self.trusted_bypass_unknown

    def tier_for(self, author_id: str) -> TrustTier:
        return self.authors.get(author_id, TrustTier.UNKNOWN)

    def is_declared(self, author_id: str) -> bool:
        return author_id in self.authors

    def allows_channel(self, channel_id: str) -> bool:
        return not self.channels or channel_id in self.channels

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "authors": {author_id: tier.value for author_id, tier in sorted(self.authors.items())},
            "channels": sorted(self.channels),
            "trusted_bypass_unknown": self.trusted_bypass_unknown,
        }

    @classmethod
    def from_dict(cls, payload: object) -> AuthorPolicy:
        """Parse `authority.json` strictly; unknown keys are refused, not ignored."""
        if not isinstance(payload, Mapping):
            raise AuthorityError(
                "authority must be an object", reason=ErrorReason.AUTHORITY_MALFORMED
            )
        known = {"schema", "version", "authors", "channels", "trusted_bypass_unknown"}
        unknown = sorted(str(key) for key in payload if key not in known)
        if unknown:
            raise AuthorityError(
                f"authority has unknown key(s): {', '.join(unknown)}",
                reason=ErrorReason.AUTHORITY_MALFORMED,
            )
        version = payload.get("version", 1)
        if isinstance(version, bool) or not isinstance(version, int) or version != 1:
            raise AuthorityError(
                f"authority version must be 1, got {version!r}",
                reason=ErrorReason.AUTHORITY_MALFORMED,
            )
        authors_raw = payload.get("authors", {})
        if not isinstance(authors_raw, Mapping):
            raise AuthorityError(
                "authority authors must be an object", reason=ErrorReason.AUTHORITY_MALFORMED
            )
        authors = {
            str(author_id): cls._tier(tier, str(author_id))
            for author_id, tier in authors_raw.items()
        }
        channels_raw = payload.get("channels", ())
        if not isinstance(channels_raw, (list, tuple)):
            raise AuthorityError(
                "authority channels must be an array", reason=ErrorReason.AUTHORITY_MALFORMED
            )
        channels: list[str] = []
        for channel_id in channels_raw:
            if not isinstance(channel_id, str) or not channel_id.strip():
                raise AuthorityError(
                    f"authority channel id {channel_id!r} must be a non-empty string",
                    reason=ErrorReason.AUTHORITY_MALFORMED,
                )
            channels.append(channel_id)
        bypass = payload.get("trusted_bypass_unknown", False)
        if not isinstance(bypass, bool):
            raise AuthorityError(
                "trusted_bypass_unknown must be a boolean", reason=ErrorReason.AUTHORITY_MALFORMED
            )
        return cls(authors=authors, channels=frozenset(channels), trusted_bypass_unknown=bypass)

    @classmethod
    def load(
        cls, path: str | Path = DEFAULT_AUTHORITY_PATH, *, missing_ok: bool = True
    ) -> AuthorPolicy | None:
        """Read the declared authority. Missing is None; malformed raises.

        `None` is the fail-closed state: the guard refuses every message with
        `author_unknown` ("no authority configured: no author is trusted"), so a
        forgotten file cannot become an open door.
        """
        target = Path(path)
        try:
            raw = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            if missing_ok:
                return None
            raise AuthorityError(
                f"authority not found: {target}", reason=ErrorReason.AUTHORITY_MISSING
            ) from None
        except OSError as exc:
            raise AuthorityError(
                f"cannot read authority {target}: {exc}", reason=ErrorReason.AUTHORITY_MISSING
            ) from None
        try:
            payload: object = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AuthorityError(
                f"authority {target} is not JSON: {exc}", reason=ErrorReason.AUTHORITY_MALFORMED
            ) from None
        return cls.from_dict(payload)


def authority_path_from_env(env: Mapping[str, str] | None = None) -> Path:
    """`MFF_AUTHORITY_PATH`, else `live/authority.json`."""
    source = os.environ if env is None else env
    configured = source.get("MFF_AUTHORITY_PATH", "").strip()
    return Path(configured) if configured else DEFAULT_AUTHORITY_PATH


# ── the admission ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Admission:
    """The answer for one message: admitted, or refused with exactly one named reason."""

    ok: bool
    reason: str = ""
    tier: TrustTier | None = None
    message_id: str = ""
    content_hash: str = ""
    detail: str = ""
    at: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.ok, bool):
            raise ProvenanceError(
                "admission ok must be a boolean", reason=ErrorReason.AUTHORITY_MALFORMED
            )
        if self.ok:
            if self.reason:
                raise ProvenanceError(
                    "an admitted message must not carry a refusal reason",
                    reason=ErrorReason.AUTHORITY_MALFORMED,
                )
        elif self.reason not in _REFUSAL_VALUES:
            raise ProvenanceError(
                f"refusal reason {self.reason!r} is not one of the named refusals",
                reason=ErrorReason.AUTHORITY_MALFORMED,
            )
        if self.tier is not None and not isinstance(self.tier, TrustTier):
            raise ProvenanceError(
                "admission tier must be a TrustTier or None", reason=ErrorReason.AUTHORITY_MALFORMED
            )

    @property
    def log_reason(self) -> str:
        """The exact reason string a listener/CLI writes for this answer."""
        return "provenance_admitted" if self.ok else f"provenance_refused:{self.reason}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "log_reason": self.log_reason,
            "tier": self.tier.value if self.tier is not None else None,
            "message_id": self.message_id,
            "content_hash": self.content_hash,
            "detail": self.detail,
            "at": self.at,
        }


@dataclass(frozen=True)
class SeenRecord:
    """What this process admitted, for duplicate and edit detection."""

    content_hash: str
    at: float
    tier: TrustTier | None = None


@dataclass(frozen=True)
class DecisionRecord:
    """The instant a message was actioned; later edits are compared against it."""

    message_id: str
    content_hash: str
    decided_at: float
    tier: TrustTier | None = None


def _structural_problem(envelope: object) -> str | None:
    """Name the defect that makes an envelope unusable. None means structurally sound."""
    if not isinstance(envelope, MessageEnvelope):
        return f"not a MessageEnvelope: {type(envelope).__name__}"
    if not isinstance(envelope.message_id, str) or not envelope.message_id.strip():
        return "message_id is empty"
    if not isinstance(envelope.channel_id, str) or not envelope.channel_id.strip():
        return "channel_id is empty"
    if not isinstance(envelope.author_id, str) or not envelope.author_id.strip():
        return "author_id is empty"
    if not isinstance(envelope.deleted, bool):
        return "deleted must be a boolean"
    if not isinstance(envelope.content, str):
        return "content must be a string"
    if not isinstance(envelope.event_type, str):
        return "event_type must be a string"
    if envelope.edited_at is not None and _finite(envelope.edited_at) is None:
        return f"edited_at must be a finite epoch second or None, got {envelope.edited_at!r}"
    if envelope.deleted:
        return None
    if not envelope.content.strip():
        return "content is empty"
    if not isinstance(envelope.content_hash, str) or not envelope.content_hash:
        return "content_hash is empty"
    return None


def _recompute(envelope: MessageEnvelope) -> str:
    return content_hash_for(
        envelope.content,
        message_id=envelope.message_id,
        reference_message_id=envelope.reference_message_id,
    )


# ── the journal ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class JournalEntry:
    """One append-only row: what happened to one message, with its fingerprint."""

    seq: int
    ts: float
    kind: str
    message_id: str
    channel_id: str = ""
    author_id: str = ""
    content_hash: str = ""
    action: str = ""
    reason: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.seq, bool) or not isinstance(self.seq, int) or self.seq <= 0:
            raise ProvenanceError(
                f"journal seq must be a positive integer, got {self.seq!r}",
                reason=ErrorReason.JOURNAL_CORRUPT,
            )
        if _finite(self.ts) is None:
            raise ProvenanceError(
                f"journal ts must be a finite epoch second, got {self.ts!r}",
                reason=ErrorReason.JOURNAL_CORRUPT,
            )
        if self.kind not in _KINDS:
            raise ProvenanceError(
                f"journal kind {self.kind!r} is not one of {', '.join(sorted(_KINDS))}",
                reason=ErrorReason.JOURNAL_CORRUPT,
            )
        if self.action not in _ACTIONS:
            raise ProvenanceError(
                f"journal action {self.action!r} is not one of {', '.join(sorted(_ACTIONS))}",
                reason=ErrorReason.JOURNAL_CORRUPT,
            )
        for name in ("message_id", "channel_id", "author_id", "content_hash", "reason", "note"):
            if not isinstance(getattr(self, name), str):
                raise ProvenanceError(
                    f"journal {name} must be a string", reason=ErrorReason.JOURNAL_CORRUPT
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "kind": self.kind,
            "message_id": self.message_id,
            "channel_id": self.channel_id,
            "author_id": self.author_id,
            "content_hash": self.content_hash,
            "action": self.action,
            "reason": self.reason,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, payload: object) -> JournalEntry:
        if not isinstance(payload, Mapping):
            raise ProvenanceError(
                "journal row must be an object", reason=ErrorReason.JOURNAL_CORRUPT
            )

        def integer(name: str) -> int:
            value = payload.get(name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ProvenanceError(
                    f"journal {name} must be an integer, got {value!r}",
                    reason=ErrorReason.JOURNAL_CORRUPT,
                )
            return value

        def number(name: str) -> float:
            value = _finite(payload.get(name))
            if value is None:
                raise ProvenanceError(
                    f"journal {name} must be a finite number", reason=ErrorReason.JOURNAL_CORRUPT
                )
            return value

        def text(name: str) -> str:
            value = payload.get(name, "")
            if not isinstance(value, str):
                raise ProvenanceError(
                    f"journal {name} must be a string", reason=ErrorReason.JOURNAL_CORRUPT
                )
            return value

        return cls(
            seq=integer("seq"),
            ts=number("ts"),
            kind=text("kind"),
            message_id=text("message_id"),
            channel_id=text("channel_id"),
            author_id=text("author_id"),
            content_hash=text("content_hash"),
            action=text("action"),
            reason=text("reason"),
            note=text("note"),
        )


@dataclass(frozen=True)
class JournalMessageState:
    """The journal's last known answer for one message id."""

    message_id: str
    content_hash: str = ""
    admitted_at: float | None = None
    decided_at: float | None = None
    revoked_reason: str = ""

    @property
    def actioned(self) -> bool:
        return self.decided_at is not None

    @property
    def revoked(self) -> bool:
        return bool(self.revoked_reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "content_hash": self.content_hash,
            "admitted_at": self.admitted_at,
            "decided_at": self.decided_at,
            "revoked_reason": self.revoked_reason,
            "actioned": self.actioned,
        }


@dataclass
class MessageJournal:
    """Append-only JSONL of provenance events, replayable into guard state."""

    path: Path | None = None
    fsync: bool = False
    max_memory: int = 10_000
    entries: list[JournalEntry] = field(default_factory=list)
    torn_lines: int = 0
    _seq: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_memory, bool)
            or not isinstance(self.max_memory, int)
            or self.max_memory <= 0
        ):
            raise ProvenanceError(
                "journal max_memory must be a positive integer", reason=ErrorReason.JOURNAL_CORRUPT
            )
        if self.path is not None:
            self.path = Path(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        kind: str,
        message_id: str,
        action: str = "",
        reason: str = "",
        channel_id: str = "",
        author_id: str = "",
        content_hash: str = "",
        note: str = "",
        ts: float | None = None,
    ) -> JournalEntry:
        """Append one event and return it. One complete write per row."""
        self._seq += 1
        entry = JournalEntry(
            seq=self._seq,
            ts=time.time() if ts is None else float(ts),
            kind=kind,
            message_id=message_id,
            channel_id=channel_id,
            author_id=author_id,
            content_hash=content_hash,
            action=action,
            reason=reason,
            note=note,
        )
        self._remember(entry)
        self._append(entry)
        return entry

    def _remember(self, entry: JournalEntry) -> None:
        self.entries.append(entry)
        if len(self.entries) > self.max_memory:
            del self.entries[: len(self.entries) - self.max_memory]

    def _append(self, entry: JournalEntry) -> None:
        if self.path is None:
            return
        payload = entry.to_dict()
        payload["v"] = V
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            if self.fsync:
                os.fsync(handle.fileno())

    @classmethod
    def load(
        cls, path: str | Path, *, fsync: bool = False, max_memory: int = 10_000
    ) -> MessageJournal:
        """Replay a journal. A missing file is empty; torn final line tolerated.

        Corruption earlier than the last line is refused: the journal exists to say
        which messages were admitted and which decisions were taken, and silently
        dropping a row in the middle would make an admitted order look unknown.
        """
        target = Path(path)
        journal = cls(path=target, fsync=fsync, max_memory=max_memory)
        if not target.exists():
            return journal
        try:
            text = target.read_text(encoding="utf-8")
        except OSError as exc:
            raise ProvenanceError(
                f"cannot read provenance journal {target}: {exc}",
                reason=ErrorReason.JOURNAL_UNREADABLE,
            ) from None
        rows = [
            (number, line.strip())
            for number, line in enumerate(text.splitlines(), start=1)
            if line.strip()
        ]
        for index, (number, line) in enumerate(rows):
            try:
                payload: object = json.loads(line)
            except json.JSONDecodeError as exc:
                if index == len(rows) - 1:
                    journal.torn_lines += 1  # crash mid-append; named, not hidden
                    break
                raise ProvenanceError(
                    f"provenance journal {target}:{number} is corrupt: {exc}",
                    reason=ErrorReason.JOURNAL_CORRUPT,
                ) from None
            if not isinstance(payload, Mapping):
                raise ProvenanceError(
                    f"provenance journal {target}:{number} is not an object",
                    reason=ErrorReason.JOURNAL_CORRUPT,
                )
            version = payload.get("v")
            if version != V:
                raise ProvenanceError(
                    f"provenance journal {target}:{number} has schema version "
                    f"{version!r}; this build reads v{V}",
                    reason=ErrorReason.JOURNAL_CORRUPT,
                )
            entry = JournalEntry.from_dict(payload)
            if entry.seq != journal._seq + 1:
                raise ProvenanceError(
                    f"provenance journal {target}:{number} has seq {entry.seq} after "
                    f"{journal._seq} (expected {journal._seq + 1})",
                    reason=ErrorReason.JOURNAL_CORRUPT,
                )
            journal._seq = entry.seq
            journal._remember(entry)
        return journal

    def verify_file(self, path: str | Path | None = None) -> list[str]:
        """Problems that make the journal untrustworthy; empty means it is intact."""
        target = Path(path) if path is not None else self.path
        if target is None:
            return ["no provenance journal configured"]
        if not target.exists():
            return [f"provenance journal not found: {target}"]
        problems: list[str] = []
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            return [f"cannot read provenance journal {target}: {exc}"]
        rows = [
            (number, line.strip()) for number, line in enumerate(lines, start=1) if line.strip()
        ]
        previous = 0
        for index, (number, line) in enumerate(rows):
            try:
                payload: object = json.loads(line)
            except json.JSONDecodeError as exc:
                if index == len(rows) - 1:
                    problems.append(
                        f"line {number}: torn final append ({exc}); the rest is readable"
                    )
                else:
                    problems.append(f"line {number}: unreadable row ({exc})")
                continue
            if not isinstance(payload, Mapping):
                problems.append(f"line {number}: row is not an object")
                continue
            if payload.get("v") != V:
                problems.append(
                    f"line {number}: schema version {payload.get('v')!r} (expected {V})"
                )
                continue
            try:
                entry = JournalEntry.from_dict(payload)
            except ProvenanceError as exc:
                problems.append(f"line {number}: {exc}")
                continue
            if entry.seq != previous + 1:
                problems.append(
                    f"line {number}: seq {entry.seq} after {previous} (expected {previous + 1})"
                )
            previous = entry.seq
        return problems

    # ── replay views ─────────────────────────────────────────────────────────
    def admitted(self) -> dict[str, JournalEntry]:
        return {
            entry.message_id: entry
            for entry in self.entries
            if entry.kind == KIND_ADMIT and entry.action == "accept"
        }

    def decisions(self) -> dict[str, JournalEntry]:
        return {entry.message_id: entry for entry in self.entries if entry.kind == KIND_DECISION}

    def revoked(self) -> dict[str, JournalEntry]:
        return {
            entry.message_id: entry
            for entry in self.entries
            if entry.kind in {KIND_REVOKE, KIND_DELETE}
        }

    def state(self) -> dict[str, JournalMessageState]:
        """Fold the journal into one last-known state per message id."""
        states: dict[str, JournalMessageState] = {}
        for entry in self.entries:
            current = states.get(entry.message_id) or JournalMessageState(
                message_id=entry.message_id
            )
            content_hash = entry.content_hash or current.content_hash
            if entry.kind == KIND_ADMIT and entry.action == "accept":
                states[entry.message_id] = JournalMessageState(
                    message_id=entry.message_id,
                    content_hash=content_hash,
                    admitted_at=entry.ts,
                    decided_at=current.decided_at,
                    revoked_reason=current.revoked_reason,
                )
            elif entry.kind == KIND_DECISION:
                states[entry.message_id] = JournalMessageState(
                    message_id=entry.message_id,
                    content_hash=content_hash,
                    admitted_at=current.admitted_at,
                    decided_at=entry.ts,
                    revoked_reason=current.revoked_reason,
                )
            elif entry.kind in {KIND_REVOKE, KIND_DELETE}:
                states[entry.message_id] = JournalMessageState(
                    message_id=entry.message_id,
                    content_hash=content_hash,
                    admitted_at=current.admitted_at,
                    decided_at=current.decided_at,
                    revoked_reason=entry.reason or Refusal.DELETED.value,
                )
        return states


# ── the guard ───────────────────────────────────────────────────────────────
@dataclass
class ProvenanceGuard:
    """Admit, refuse, and detect the edit/delete races that can invalidate an order.

    `policy=None` is the fail-closed state: `authority.json` was missing or unreadable,
    so no author is trusted and every message is refused with `author_unknown`. The
    journal, when attached, is the audit sink and the replay source for a restart.
    """

    policy: AuthorPolicy | None = None
    journal: MessageJournal | None = None
    max_seen: int = 10_000
    clock: Callable[[], float] = time.time
    _seen: dict[str, SeenRecord] = field(default_factory=dict, init=False, repr=False)
    _decisions: dict[str, DecisionRecord] = field(default_factory=dict, init=False, repr=False)
    _revoked: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_seen, bool)
            or not isinstance(self.max_seen, int)
            or self.max_seen <= 0
        ):
            raise ProvenanceError(
                "max_seen must be a positive integer", reason=ErrorReason.JOURNAL_CORRUPT
            )

    @classmethod
    def from_policy_file(
        cls,
        path: str | Path = DEFAULT_AUTHORITY_PATH,
        *,
        journal: MessageJournal | None = None,
        max_seen: int = 10_000,
        clock: Callable[[], float] = time.time,
    ) -> ProvenanceGuard:
        """The wiring seam: load `authority.json`, fail closed when it is absent."""
        policy = AuthorPolicy.load(path)
        return cls(policy=policy, journal=journal, max_seen=max_seen, clock=clock)

    @classmethod
    def from_journal(
        cls,
        journal: MessageJournal,
        *,
        policy: AuthorPolicy | None = None,
        max_seen: int = 10_000,
        clock: Callable[[], float] = time.time,
    ) -> ProvenanceGuard:
        """Rebuild admitted/decided/revoked state from a replayed journal.

        A restart must not re-admit a message it already actioned, and it must not
        forget that a message was deleted mid-flight.
        """
        guard = cls(policy=policy, journal=journal, max_seen=max_seen, clock=clock)
        for entry in journal.entries:
            guard._absorb(entry)
        return guard

    def _absorb(self, entry: JournalEntry) -> None:
        if entry.kind == KIND_ADMIT and entry.action == "accept":
            self._seen[entry.message_id] = SeenRecord(content_hash=entry.content_hash, at=entry.ts)
        elif entry.kind == KIND_DECISION:
            self._decisions[entry.message_id] = DecisionRecord(
                message_id=entry.message_id, content_hash=entry.content_hash, decided_at=entry.ts
            )
        elif entry.kind in {KIND_REVOKE, KIND_DELETE} and entry.action == "revoke":
            self._revoked[entry.message_id] = entry.reason or Refusal.DELETED.value
        elif entry.kind == KIND_DELETE:
            # A delete recorded before any decision is still a durable revocation: a
            # restart must not re-admit a message that was already deleted, even though
            # the original row's action was "drop" (nothing to revoke yet).
            self._revoked[entry.message_id] = Refusal.DELETED.value
        self._bound_seen()

    def _bound_seen(self) -> None:
        while len(self._seen) > self.max_seen:
            self._seen.pop(next(iter(self._seen)))

    def _now(self, now: float | None) -> float:
        return self.clock() if now is None else float(now)

    # ── recording ─────────────────────────────────────────────────────────────
    def _record(
        self,
        *,
        kind: str,
        admission: Admission,
        envelope: MessageEnvelope | None,
        action: str,
        note: str = "",
    ) -> None:
        journal = self.journal
        if journal is None:
            return
        journal.record(
            kind=kind,
            message_id=admission.message_id,
            action=action,
            reason=admission.reason,
            channel_id=envelope.channel_id if envelope is not None else "",
            author_id=envelope.author_id if envelope is not None else "",
            content_hash=admission.content_hash,
            note=note,
            ts=admission.at,
        )

    def _refuse(
        self,
        reason: Refusal,
        detail: str,
        at: float,
        envelope: MessageEnvelope | None,
        kind: str,
        *,
        tier: TrustTier | None = None,
        action: str = "drop",
    ) -> Admission:
        admission = Admission(
            ok=False,
            reason=reason.value,
            tier=tier,
            message_id=envelope.message_id if envelope is not None else "",
            content_hash=envelope.content_hash if envelope is not None else "",
            detail=detail,
            at=at,
        )
        self._record(kind=kind, admission=admission, envelope=envelope, action=action)
        return admission

    # ── the decisions ─────────────────────────────────────────────────────────
    def admit(self, envelope: MessageEnvelope, now: float | None = None) -> Admission:
        """Admit a new message, or name the reason it is not an order.

        This is called by the filter BEFORE parsing. A refusal here means the parser
        never sees the post, which is the point: an edit that changes a strike cannot
        become a `Signal` if the edited message never earns an admission.
        """
        at = self._now(now)
        problem = _structural_problem(envelope)
        if problem is not None:
            return Admission(False, Refusal.MALFORMED_ENVELOPE.value, detail=problem, at=at)
        assert isinstance(envelope, MessageEnvelope)  # narrowed by the check
        if envelope.deleted:
            return self._refuse(Refusal.DELETED, "the message is deleted", at, envelope, KIND_ADMIT)
        policy = self.policy
        if policy is None or not policy.configured:
            return self._refuse(
                Refusal.AUTHOR_UNKNOWN,
                "no authority configured: no author is trusted",
                at,
                envelope,
                KIND_ADMIT,
            )
        tier = policy.tier_for(envelope.author_id)
        bypass = ""
        if tier is TrustTier.UNKNOWN:
            if policy.is_declared(envelope.author_id):
                return self._refuse(
                    Refusal.AUTHOR_NOT_ALLOWED,
                    f"author {envelope.author_id} is declared UNKNOWN, not allowed",
                    at,
                    envelope,
                    KIND_ADMIT,
                )
            if not policy.trusted_bypass_unknown:
                return self._refuse(
                    Refusal.AUTHOR_UNKNOWN,
                    f"author {envelope.author_id} is not in the allowlist",
                    at,
                    envelope,
                    KIND_ADMIT,
                )
            tier = TrustTier.TRUSTED
            bypass = (
                "trusted_bypass_unknown: the policy trusts authors it has never "
                "seen (this one check only)"
            )
        if not policy.allows_channel(envelope.channel_id):
            return self._refuse(
                Refusal.CHANNEL_NOT_ALLOWED,
                f"channel {envelope.channel_id} is not in the declared channel set",
                at,
                envelope,
                KIND_ADMIT,
                tier=tier,
            )
        recomputed = _recompute(envelope)
        if envelope.content_hash != recomputed:
            return self._refuse(
                Refusal.CONTENT_CHANGED,
                "content_hash does not match the content: the envelope was tampered "
                "with or built from different text",
                at,
                envelope,
                KIND_ADMIT,
                tier=tier,
            )
        revoked = self._revoked.get(envelope.message_id)
        if revoked is not None:
            reason = _as_refusal(revoked)
            return self._refuse(
                reason,
                f"message {envelope.message_id} was revoked earlier: {revoked}",
                at,
                envelope,
                KIND_ADMIT,
                tier=tier,
            )
        decision = self._decisions.get(envelope.message_id)
        if decision is not None:
            if recomputed != decision.content_hash:
                return self._refuse(
                    Refusal.CONTENT_CHANGED,
                    "the text differs from the actioned decision",
                    at,
                    envelope,
                    KIND_ADMIT,
                    tier=tier,
                )
            if envelope.edited_at is not None and envelope.edited_at > decision.decided_at:
                return self._refuse(
                    Refusal.EDITED_AFTER_DECISION,
                    f"edited at {envelope.edited_at:.0f} after the decision at "
                    f"{decision.decided_at:.0f}",
                    at,
                    envelope,
                    KIND_ADMIT,
                    tier=tier,
                )
            return self._refuse(
                Refusal.DUPLICATE,
                "the message was already actioned",
                at,
                envelope,
                KIND_ADMIT,
                tier=tier,
            )
        seen = self._seen.get(envelope.message_id)
        if seen is not None:
            if recomputed != seen.content_hash:
                return self._refuse(
                    Refusal.CONTENT_CHANGED,
                    "the text changed after it was admitted",
                    at,
                    envelope,
                    KIND_ADMIT,
                    tier=tier,
                )
            return self._refuse(
                Refusal.DUPLICATE,
                "the message was already admitted",
                at,
                envelope,
                KIND_ADMIT,
                tier=tier,
            )
        self._seen[envelope.message_id] = SeenRecord(content_hash=recomputed, at=at, tier=tier)
        self._bound_seen()
        admission = Admission(
            ok=True,
            tier=tier,
            message_id=envelope.message_id,
            content_hash=recomputed,
            detail=bypass,
            at=at,
        )
        self._record(kind=KIND_ADMIT, admission=admission, envelope=envelope, action="accept")
        return admission

    def note_decision(self, envelope: MessageEnvelope, now: float | None = None) -> Admission:
        """Record that this message produced a decision (an order is being prepared).

        Called by the listener after the engine returns intents and before the send.
        From this instant an edit is `edited_after_decision` and the order must be
        withdrawn; from this instant a redelivery is `duplicate`.
        """
        at = self._now(now)
        problem = _structural_problem(envelope)
        if problem is not None:
            return Admission(False, Refusal.MALFORMED_ENVELOPE.value, detail=problem, at=at)
        assert isinstance(envelope, MessageEnvelope)
        if envelope.deleted:
            return self._refuse(
                Refusal.DELETED, "the message is deleted", at, envelope, KIND_DECISION
            )
        recomputed = _recompute(envelope)
        if envelope.content_hash != recomputed:
            return self._refuse(
                Refusal.CONTENT_CHANGED,
                "content_hash does not match the content of the decision envelope",
                at,
                envelope,
                KIND_DECISION,
            )
        seen = self._seen.get(envelope.message_id)
        if seen is None:
            return self._refuse(
                Refusal.MALFORMED_ENVELOPE,
                "a decision was recorded for a message this guard never admitted",
                at,
                envelope,
                KIND_DECISION,
            )
        if recomputed != seen.content_hash:
            return self._refuse(
                Refusal.CONTENT_CHANGED,
                "the text differs from the admitted message; the decision is stale",
                at,
                envelope,
                KIND_DECISION,
            )
        if envelope.edited_at is not None and envelope.edited_at > seen.at:
            return self._refuse(
                Refusal.EDITED_AFTER_DECISION,
                f"edited at {envelope.edited_at:.0f} after admission at {seen.at:.0f}",
                at,
                envelope,
                KIND_DECISION,
            )
        self._decisions[envelope.message_id] = DecisionRecord(
            message_id=envelope.message_id, content_hash=recomputed, decided_at=at, tier=seen.tier
        )
        admission = Admission(
            ok=True,
            tier=seen.tier,
            message_id=envelope.message_id,
            content_hash=recomputed,
            detail="decision_recorded",
            at=at,
        )
        self._record(kind=KIND_DECISION, admission=admission, envelope=envelope, action="accept")
        return admission

    def verify_before_send(
        self, envelope: MessageEnvelope, decision: Admission, now: float | None = None
    ) -> Admission:
        """Recompute the fingerprint immediately before the broker call.

        The one function that can abort a not-yet-sent order: if the envelope's text no
        longer matches the admission (an edit raced the decision), or the message was
        deleted/revoked, the caller must not send. It has no side effect on admitted
        state; refusals are journalled under `verify`.
        """
        at = self._now(now)
        if not isinstance(decision, Admission) or not decision.ok:
            return Admission(
                False,
                Refusal.CONTENT_CHANGED.value,
                at=at,
                detail="no admitted decision to verify against",
            )
        problem = _structural_problem(envelope)
        if problem is not None:
            return Admission(False, Refusal.MALFORMED_ENVELOPE.value, detail=problem, at=at)
        assert isinstance(envelope, MessageEnvelope)
        if envelope.deleted:
            return self._refuse(
                Refusal.DELETED, "the message is deleted", at, envelope, KIND_VERIFY
            )
        recomputed = _recompute(envelope)
        if envelope.content_hash != recomputed:
            return self._refuse(
                Refusal.CONTENT_CHANGED,
                "content_hash does not match the content before send",
                at,
                envelope,
                KIND_VERIFY,
            )
        message_id = envelope.message_id
        revoked = self._revoked.get(message_id)
        if revoked is not None:
            reason = _as_refusal(revoked)
            return self._refuse(
                reason, f"message {message_id} was revoked: {revoked}", at, envelope, KIND_VERIFY
            )
        if recomputed != decision.content_hash:
            return self._refuse(
                Refusal.CONTENT_CHANGED,
                "the text differs from the admitted decision; aborting before send",
                at,
                envelope,
                KIND_VERIFY,
            )
        decided = self._decisions.get(message_id)
        if decided is not None:
            if decided.content_hash != recomputed:
                return self._refuse(
                    Refusal.CONTENT_CHANGED,
                    "the text differs from the actioned message",
                    at,
                    envelope,
                    KIND_VERIFY,
                )
            if envelope.edited_at is not None and envelope.edited_at > decided.decided_at:
                return self._refuse(
                    Refusal.EDITED_AFTER_DECISION,
                    f"edited at {envelope.edited_at:.0f} after the decision at "
                    f"{decided.decided_at:.0f}",
                    at,
                    envelope,
                    KIND_VERIFY,
                )
        return Admission(
            ok=True,
            tier=decision.tier,
            message_id=message_id,
            content_hash=recomputed,
            detail="verified_before_send",
            at=at,
        )

    def on_update(self, envelope: MessageEnvelope, now: float | None = None) -> Admission:
        """Classify a MESSAGE_UPDATE: nothing to do, or a reason to revoke.

        An edit of an already-actioned message is the dangerous race; an edit of a
        message that was admitted but not actioned is still a reason to drop, because
        the parser may be about to see text the guard never fingerprinted.
        """
        at = self._now(now)
        problem = _structural_problem(envelope)
        if problem is not None:
            return Admission(False, Refusal.MALFORMED_ENVELOPE.value, detail=problem, at=at)
        assert isinstance(envelope, MessageEnvelope)
        if envelope.deleted:
            return self._refuse(Refusal.DELETED, "the message is deleted", at, envelope, KIND_EDIT)
        recomputed = _recompute(envelope)
        if envelope.content_hash != recomputed:
            return self._refuse(
                Refusal.CONTENT_CHANGED,
                "content_hash does not match the edited content",
                at,
                envelope,
                KIND_EDIT,
            )
        message_id = envelope.message_id
        decision = self._decisions.get(message_id)
        seen = self._seen.get(message_id)
        if decision is None and seen is None:
            return self._refuse(
                Refusal.DUPLICATE,
                "update for a message this guard never admitted; nothing to revoke",
                at,
                envelope,
                KIND_EDIT,
            )
        if decision is None:
            assert seen is not None
            if recomputed != seen.content_hash:
                return self._refuse(
                    Refusal.CONTENT_CHANGED,
                    "content changed after admission, before a decision",
                    at,
                    envelope,
                    KIND_EDIT,
                    tier=seen.tier,
                )
            return self._refuse(
                Refusal.DUPLICATE,
                "update with unchanged content",
                at,
                envelope,
                KIND_EDIT,
                tier=seen.tier,
            )
        if recomputed != decision.content_hash:
            return self._refuse(
                Refusal.CONTENT_CHANGED,
                "the edited text differs from the actioned message",
                at,
                envelope,
                KIND_EDIT,
                tier=decision.tier,
            )
        if envelope.edited_at is not None and envelope.edited_at > decision.decided_at:
            return self._refuse(
                Refusal.EDITED_AFTER_DECISION,
                f"edited at {envelope.edited_at:.0f} after the decision at "
                f"{decision.decided_at:.0f}",
                at,
                envelope,
                KIND_EDIT,
                tier=decision.tier,
            )
        return self._refuse(
            Refusal.DUPLICATE,
            "update with unchanged content",
            at,
            envelope,
            KIND_EDIT,
            tier=decision.tier,
        )

    def on_delete(self, message_id: str, now: float | None = None) -> Admission:
        """A deletion is always a revocation signal for a not-yet-sent order."""
        at = self._now(now)
        if not isinstance(message_id, str) or not message_id.strip():
            return Admission(
                False, Refusal.MALFORMED_ENVELOPE.value, detail="message_id is empty", at=at
            )
        actioned = message_id in self._decisions
        self._revoked[message_id] = Refusal.DELETED.value
        admission = Admission(
            ok=False,
            reason=Refusal.DELETED.value,
            message_id=message_id,
            detail=("deleted after a decision" if actioned else "deleted before any decision"),
            at=at,
        )
        self._record(
            kind=KIND_DELETE,
            admission=admission,
            envelope=None,
            action="revoke" if actioned else "drop",
        )
        return admission

    def revoke(self, message_id: str, reason: str, now: float | None = None) -> Admission:
        """Persist a revocation so a later create/update cannot re-admit the message."""
        if reason not in _REVOKE_VALUES:
            raise ValueError(
                f"revocation reason must be one of {sorted(_REVOKE_VALUES)}, got {reason!r}"
            )
        at = self._now(now)
        self._revoked[message_id] = reason
        decision = self._decisions.get(message_id)
        admission = Admission(
            ok=False,
            reason=reason,
            message_id=message_id,
            content_hash=decision.content_hash if decision is not None else "",
            tier=decision.tier if decision is not None else None,
            detail="revoked before send",
            at=at,
        )
        self._record(kind=KIND_REVOKE, admission=admission, envelope=None, action="revoke")
        return admission

    # ── read views ────────────────────────────────────────────────────────────
    def is_actioned(self, message_id: str) -> bool:
        return message_id in self._decisions

    def decision_for(self, message_id: str) -> DecisionRecord | None:
        return self._decisions.get(message_id)

    def seen_for(self, message_id: str) -> SeenRecord | None:
        return self._seen.get(message_id)

    def is_revoked(self, message_id: str) -> bool:
        return message_id in self._revoked

    def revoked_reason(self, message_id: str) -> str:
        return self._revoked.get(message_id, "")


def _as_refusal(reason: str) -> Refusal:
    """A stored revocation reason as the enum, defaulting to the safe refusal."""
    try:
        return Refusal(reason)
    except ValueError:
        return Refusal.CONTENT_CHANGED


# ── the filter ──────────────────────────────────────────────────────────────
class FilterAction(StrEnum):
    """What the caller should do with one Gateway event."""

    ADMIT = "ADMIT"
    DROP = "DROP"
    REVOKE = "REVOKE"


@dataclass(frozen=True)
class FilterVerdict:
    """One event's disposition, with the admission that produced it."""

    action: FilterAction
    admission: Admission

    def __post_init__(self) -> None:
        if not isinstance(self.action, FilterAction):
            raise ProvenanceError(
                "filter action must be a FilterAction", reason=ErrorReason.AUTHORITY_MALFORMED
            )
        if not isinstance(self.admission, Admission):
            raise ProvenanceError(
                "filter verdict must carry an Admission", reason=ErrorReason.AUTHORITY_MALFORMED
            )

    @property
    def ok(self) -> bool:
        return self.action is FilterAction.ADMIT

    @property
    def must_abort(self) -> bool:
        return self.action is FilterAction.REVOKE

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action.value, "admission": self.admission.to_dict()}


@dataclass
class ProvenanceFilter:
    """The Gateway/listener seam: admit, drop, and revoke.

    The Lead wires it in front of the parser:

    * `filter_create(envelope)` -> `ADMIT` only for an allowed, unedited, fresh message;
      anything else is `DROP` and the parser never runs;
    * `filter_update(envelope)` / `filter_delete(message_id)` -> `REVOKE` when the
      affected message was already actioned, so the caller can page and refuse a send
      that has not happened yet;
    * `verify_before_send(envelope, decision)` re-checks the fingerprint; and
    * `record_decision(envelope)` marks the message actioned.
    """

    guard: ProvenanceGuard

    def filter_create(self, envelope: MessageEnvelope, now: float | None = None) -> FilterVerdict:
        admission = self.guard.admit(envelope, now)
        action = FilterAction.ADMIT if admission.ok else FilterAction.DROP
        return FilterVerdict(action, admission)

    def filter_update(self, envelope: MessageEnvelope, now: float | None = None) -> FilterVerdict:
        admission = self.guard.on_update(envelope, now)
        message_id = envelope.message_id if isinstance(envelope, MessageEnvelope) else ""
        if message_id and self.guard.is_actioned(message_id) and admission.reason in _REVOKE_VALUES:
            return FilterVerdict(
                FilterAction.REVOKE, self.guard.revoke(message_id, admission.reason, now)
            )
        return FilterVerdict(FilterAction.DROP, admission)

    def filter_delete(self, message_id: str, now: float | None = None) -> FilterVerdict:
        actioned = self.guard.is_actioned(message_id)
        admission = self.guard.on_delete(message_id, now)
        if actioned:
            return FilterVerdict(
                FilterAction.REVOKE, self.guard.revoke(message_id, Refusal.DELETED.value, now)
            )
        return FilterVerdict(FilterAction.DROP, admission)

    def verify_before_send(
        self, envelope: MessageEnvelope, decision: Admission, now: float | None = None
    ) -> Admission:
        return self.guard.verify_before_send(envelope, decision, now)

    def record_decision(self, envelope: MessageEnvelope, now: float | None = None) -> Admission:
        return self.guard.note_decision(envelope, now)

    def observe(
        self, event_type: str, envelope: MessageEnvelope, now: float | None = None
    ) -> FilterVerdict:
        """Dispatch one Gateway dispatch by event type. Unknown events are dropped."""
        kind = str(event_type or "").strip().upper()
        if kind == "MESSAGE_CREATE":
            return self.filter_create(envelope, now)
        if kind == "MESSAGE_UPDATE":
            return self.filter_update(envelope, now)
        if kind == "MESSAGE_DELETE":
            message_id = envelope.message_id if isinstance(envelope, MessageEnvelope) else ""
            return self.filter_delete(message_id, now)
        return FilterVerdict(
            FilterAction.DROP,
            Admission(
                False,
                Refusal.MALFORMED_ENVELOPE.value,
                detail=f"unsupported event type {event_type!r}",
            ),
        )


# ── rendering (the CLI helpers the Lead wires) ──────────────────────────────
def render_policy(policy: AuthorPolicy | None) -> str:
    """A short human view of the declared authority."""
    if policy is None:
        return "authority  (none configured: no author is trusted)"
    lines = [
        f"authority  {len(policy.authors)} author(s); "
        f"bypass_unknown={'on' if policy.trusted_bypass_unknown else 'off'}",
        f"channels   {', '.join(sorted(policy.channels)) or '(any)'}",
    ]
    for author_id, tier in sorted(policy.authors.items()):
        lines.append(f"  {author_id:<24} {tier.value}")
    return "\n".join(lines)


def render_admission(admission: Admission) -> str:
    """One line per message for the listener log and `authority check`."""
    tier = admission.tier.value if admission.tier is not None else "-"
    line = f"{admission.log_reason} msg={admission.message_id or '-'} tier={tier}"
    if admission.detail:
        line += f" - {admission.detail}"
    return line


def render_verdict(verdict: FilterVerdict) -> str:
    """One line per Gateway event: ADMIT, DROP or REVOKE, with the reason."""
    detail = verdict.admission.detail or "no detail"
    return f"{verdict.action.value} {verdict.admission.log_reason} - {detail}"


def render_journal(journal: MessageJournal) -> str:
    """Counts by kind, plus the replay state the guard would rebuild."""
    counts: dict[str, int] = {}
    for entry in journal.entries:
        counts[entry.kind] = counts.get(entry.kind, 0) + 1
    lines = [
        f"journal    {len(journal.entries)} row(s)"
        + (f"; {journal.torn_lines} torn final line(s)" if journal.torn_lines else "")
    ]
    for kind in sorted(counts):
        lines.append(f"  {kind:<10} {counts[kind]}")
    states = journal.state()
    lines.append(
        f"messages   {len(states)} tracked; "
        f"{sum(1 for state in states.values() if state.actioned)} actioned; "
        f"{sum(1 for state in states.values() if state.revoked)} revoked"
    )
    return "\n".join(lines)
