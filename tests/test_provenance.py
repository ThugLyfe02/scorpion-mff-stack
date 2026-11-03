"""Input provenance: authority, fingerprints, and the edit/delete race.

The claims proven here are the ones that can prevent a wrong order:

1. **Authority is declared and fails closed.** An allowlisted TRUSTED/KNOWN author is
   admitted; an absent author is `author_unknown`; an explicitly UNKNOWN author is
   `author_not_allowed`; a channel outside the declared set is refused; and a missing or
   malformed `authority.json` trusts nobody. The one bypass is the declared
   `trusted_bypass_unknown`, and it bypasses only the unknown-author check.

2. **The fingerprint is deterministic and detects an edit.** Whitespace, CRLF and
   full-width digits do not change it; a changed strike, message id or reply context
   does.

3. **An edit or delete can abort a not-yet-sent order.** `verify_before_send` recomputes
   the hash and refuses `content_changed`; `filter_update`/`filter_delete` return
   `REVOKE` for an already-actioned message so the caller can page and refuse the send.

4. **The journal is replayable and refuses mid-file corruption.** A torn final line is
   tolerated and named; a gap or unreadable middle row is a refusal; the replayed guard
   durably remembers decisions and revocations across a restart.
"""

from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import pytest

from scorpion_mff.provenance import (
    DEFAULT_AUTHORITY_PATH,
    Admission,
    AuthorityError,
    AuthorPolicy,
    DecisionRecord,
    ErrorReason,
    FilterAction,
    FilterVerdict,
    JournalEntry,
    MessageEnvelope,
    MessageJournal,
    ProvenanceError,
    ProvenanceFilter,
    ProvenanceGuard,
    Refusal,
    TrustTier,
    authority_path_from_env,
    content_hash_for,
    normalize_content,
    render_admission,
    render_journal,
    render_policy,
    render_verdict,
)

T0 = 1_757_340_000.0
CHANNEL = "1231301953972207667"
AUTHOR = "trusted-author-1"
OTHER = "known-author-2"
DENIED = "denied-author-3"
CONTENT = "QQQ 720 calls at 1.15, adding 2"


def write_json(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


_SCRATCH = Path(__file__).resolve().parent / "_scratch"


@pytest.fixture()
def tmp_path(request):
    """A workspace-local scratch directory, removed afterwards.

    The DSH Windows sandbox cannot read directories made by `tempfile.mkdtemp`
    (which is what pytest's stock `tmp_path` uses), so this module supplies the
    same fixture over a normal directory under `tests/`.
    """
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in request.node.name)
    path = _SCRATCH / safe
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def base_policy(**overrides) -> AuthorPolicy:
    values: dict = {
        "authors": {AUTHOR: TrustTier.TRUSTED, OTHER: TrustTier.KNOWN, DENIED: TrustTier.UNKNOWN},
        "channels": frozenset({CHANNEL}),
    }
    values.update(overrides)
    return AuthorPolicy(**values)


def envelope(
    message_id: str = "msg-1",
    author_id: str = AUTHOR,
    channel_id: str = CHANNEL,
    content: str = CONTENT,
    edited_at: float | None = None,
    deleted: bool = False,
    reference_message_id: str = "",
    event_type: str = "MESSAGE_CREATE",
    content_hash: str = "",
) -> MessageEnvelope:
    return MessageEnvelope(
        message_id=message_id,
        channel_id=channel_id,
        author_id=author_id,
        author_name="the hone",
        content=content,
        edited_at=edited_at,
        deleted=deleted,
        reference_message_id=reference_message_id,
        event_type=event_type,
        content_hash=content_hash,
    )


def edited(
    source: MessageEnvelope, *, content: str | None = None, edited_at: float | None = T0 + 1
) -> MessageEnvelope:
    """A MESSAGE_UPDATE for the same id, with a freshly computed fingerprint."""
    return envelope(
        message_id=source.message_id,
        author_id=source.author_id,
        channel_id=source.channel_id,
        content=source.content if content is None else content,
        edited_at=edited_at,
        reference_message_id=source.reference_message_id,
        event_type="MESSAGE_UPDATE",
    )


def guard(**overrides) -> ProvenanceGuard:
    values: dict = {"policy": base_policy(), "journal": None, "clock": lambda: T0}
    values.update(overrides)
    return ProvenanceGuard(**values)


# ── normalisation and the fingerprint ──────────────────────────────────────
def test_whitespace_and_line_endings_do_not_change_the_fingerprint():
    first = content_hash_for("QQQ 720 calls\r\nat 1.15", message_id="m1")
    second = content_hash_for("  QQQ   720 calls\n\nat 1.15  ", message_id="m1")
    assert first == second
    assert normalize_content(" a\r\n  b ") == "a b"


def test_nfkc_folds_full_width_digits_into_ascii():
    assert normalize_content("ＱＱＱ ７２０") == "QQQ 720"
    assert content_hash_for("ＱＱＱ ７２０", message_id="m") == content_hash_for(
        "QQQ 720", message_id="m"
    )


def test_zero_width_characters_are_removed():
    assert normalize_content("QQ\u200bQ 720") == "QQQ 720"


def test_an_edited_strike_changes_the_fingerprint():
    before = content_hash_for("QQQ 720 calls", message_id="m1")
    after = content_hash_for("QQQ 725 calls", message_id="m1")
    assert before != after
    assert len(before) == 64


def test_the_fingerprint_includes_message_and_reply_identity():
    base = content_hash_for("QQQ 720 calls", message_id="m1")
    assert base != content_hash_for("QQQ 720 calls", message_id="m2")
    assert base != content_hash_for("QQQ 720 calls", message_id="m1", reference_message_id="m0")


def test_the_envelope_computes_its_own_fingerprint():
    env = envelope()
    assert env.content_hash == content_hash_for(CONTENT, message_id="msg-1")
    assert env.is_edit is False
    assert edited(env).is_edit is True


def test_normalisation_is_deterministic_and_validates_the_type():
    assert normalize_content(CONTENT) == normalize_content(CONTENT)
    with pytest.raises(ProvenanceError):
        normalize_content(720)  # type: ignore[arg-type]


# ── the declared authority ─────────────────────────────────────────────────
def test_policy_parses_tiers_case_insensitively():
    policy = AuthorPolicy.from_dict(
        {
            "authors": {"a": "trusted", "b": "Known", "c": "UNKNOWN"},
            "channels": [CHANNEL],
        }
    )
    assert policy.tier_for("a") is TrustTier.TRUSTED
    assert policy.tier_for("b") is TrustTier.KNOWN
    assert policy.tier_for("c") is TrustTier.UNKNOWN
    assert policy.tier_for("d") is TrustTier.UNKNOWN
    assert policy.allows_channel(CHANNEL)
    assert policy.is_declared("a") and not policy.is_declared("d")


def test_policy_refuses_an_unknown_key_instead_of_ignoring_it():
    with pytest.raises(AuthorityError, match="unknown key") as excinfo:
        AuthorPolicy.from_dict({"author": {"a": "TRUSTED"}})
    assert excinfo.value.reason is ErrorReason.AUTHORITY_MALFORMED


def test_policy_refuses_a_bad_tier():
    with pytest.raises(AuthorityError, match="tier"):
        AuthorPolicy.from_dict({"authors": {"a": "FRIEND"}})


def test_policy_refuses_a_bad_version_and_bad_channels():
    with pytest.raises(AuthorityError, match="version"):
        AuthorPolicy.from_dict({"version": 2})
    with pytest.raises(AuthorityError, match="channel"):
        AuthorPolicy.from_dict({"channels": [7]})


def test_an_empty_policy_is_not_configured():
    assert AuthorPolicy().configured is False
    assert base_policy().configured is True
    assert AuthorPolicy(trusted_bypass_unknown=True).configured is True


def test_policy_roundtrips_through_its_wire_shape():
    payload = base_policy().to_dict()
    again = AuthorPolicy.from_dict(payload)
    assert again.authors == base_policy().authors
    assert again.channels == base_policy().channels


def test_a_missing_authority_file_is_none_or_a_named_refusal(tmp_path):
    path = tmp_path / "authority.json"
    assert AuthorPolicy.load(path) is None
    with pytest.raises(AuthorityError) as excinfo:
        AuthorPolicy.load(path, missing_ok=False)
    assert excinfo.value.reason is ErrorReason.AUTHORITY_MISSING


def test_a_malformed_authority_file_is_named(tmp_path):
    path = tmp_path / "authority.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(AuthorityError) as excinfo:
        AuthorPolicy.load(path)
    assert excinfo.value.reason is ErrorReason.AUTHORITY_MALFORMED


def test_authority_path_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv("MFF_AUTHORITY_PATH", raising=False)
    assert authority_path_from_env() == DEFAULT_AUTHORITY_PATH
    monkeypatch.setenv("MFF_AUTHORITY_PATH", "config/authority.json")
    assert authority_path_from_env() == Path("config/authority.json")


# ── admission: tiers, channels, malformed evidence ─────────────────────────
def test_a_trusted_author_is_admitted_with_its_tier():
    admission = guard().admit(envelope(), T0)
    assert admission.ok
    assert admission.tier is TrustTier.TRUSTED
    assert admission.content_hash == envelope().content_hash
    assert admission.log_reason == "provenance_admitted"


def test_a_known_author_is_admitted_too():
    admission = guard().admit(envelope(author_id=OTHER), T0)
    assert admission.ok and admission.tier is TrustTier.KNOWN


def test_an_absent_author_is_refused_as_unknown():
    admission = guard().admit(envelope(author_id="stranger"), T0)
    assert not admission.ok
    assert admission.reason == Refusal.AUTHOR_UNKNOWN.value
    assert admission.log_reason == "provenance_refused:author_unknown"


def test_an_explicitly_unknown_author_is_not_allowed():
    admission = guard().admit(envelope(author_id=DENIED), T0)
    assert not admission.ok
    assert admission.reason == Refusal.AUTHOR_NOT_ALLOWED.value


def test_no_authority_configured_trusts_nobody():
    admission = guard(policy=None).admit(envelope(), T0)
    assert not admission.ok
    assert admission.reason == Refusal.AUTHOR_UNKNOWN.value
    assert "no authority configured" in admission.detail


def test_the_declared_bypass_admits_an_unknown_author_only_for_that_check():
    bypass = base_policy(trusted_bypass_unknown=True)
    admission = guard(policy=bypass).admit(envelope(author_id="stranger"), T0)
    assert admission.ok and admission.tier is TrustTier.TRUSTED
    assert "trusted_bypass_unknown" in admission.detail
    # An explicit UNKNOWN author is still refused: the bypass is for absent ids only.
    denied = guard(policy=bypass).admit(envelope(author_id=DENIED), T0)
    assert not denied.ok and denied.reason == Refusal.AUTHOR_NOT_ALLOWED.value
    # And every other refusal still applies to the bypassed author.
    off_channel = guard(policy=bypass).admit(
        envelope(author_id="stranger", channel_id="elsewhere"), T0
    )
    assert off_channel.reason == Refusal.CHANNEL_NOT_ALLOWED.value


def test_a_channel_outside_the_declared_set_is_refused():
    admission = guard().admit(envelope(channel_id="unknown-channel"), T0)
    assert not admission.ok
    assert admission.reason == Refusal.CHANNEL_NOT_ALLOWED.value


def test_an_empty_channel_set_allows_any_channel():
    admission = guard(policy=base_policy(channels=frozenset())).admit(
        envelope(channel_id="anything"), T0
    )
    assert admission.ok


def test_a_deleted_envelope_is_refused_not_admitted():
    admission = guard().admit(envelope(deleted=True), T0)
    assert not admission.ok and admission.reason == Refusal.DELETED.value


def test_malformed_envelopes_are_named_not_crashing():
    for bad, needle in [
        (envelope(message_id=""), "message_id"),
        (envelope(channel_id=""), "channel_id"),
        (envelope(author_id=""), "author_id"),
        (envelope(content="   "), "content"),
        (envelope(edited_at=math.nan), "edited_at"),
    ]:
        admission = guard().admit(bad, T0)
        assert not admission.ok
        assert admission.reason == Refusal.MALFORMED_ENVELOPE.value
        assert needle in admission.detail
    not_an_envelope = guard().admit("QQQ 720 calls", T0)  # type: ignore[arg-type]
    assert not_an_envelope.ok is False
    assert not_an_envelope.reason == Refusal.MALFORMED_ENVELOPE.value


def test_a_tampered_content_hash_is_content_changed():
    tampered = envelope(content_hash="0" * 64)
    admission = guard().admit(tampered, T0)
    assert not admission.ok
    assert admission.reason == Refusal.CONTENT_CHANGED.value


def test_the_same_message_twice_is_a_duplicate():
    guard_ = guard()
    first = guard_.admit(envelope(), T0)
    assert first.ok
    second = guard_.admit(envelope(), T0)
    assert second.ok is False
    assert second.reason == Refusal.DUPLICATE.value


def test_a_redelivery_with_different_text_is_content_changed():
    guard_ = guard()
    assert guard_.admit(envelope(), T0).ok
    changed = guard_.admit(envelope(content="QQQ 725 calls"), T0)
    assert not changed.ok
    assert changed.reason == Refusal.CONTENT_CHANGED.value


def test_a_decision_is_recorded_once_and_survives_redelivery():
    guard_ = guard()
    env = envelope()
    assert guard_.admit(env, T0).ok
    assert guard_.note_decision(env, T0 + 1).ok
    assert guard_.is_actioned(env.message_id)
    decision = guard_.decision_for(env.message_id)
    assert isinstance(decision, DecisionRecord)
    assert decision.decided_at == T0 + 1
    redelivered = guard_.admit(env, T0 + 2)
    assert redelivered.reason == Refusal.DUPLICATE.value


def test_a_decision_for_an_unadmitted_message_is_refused():
    admission = guard().note_decision(envelope(), T0)
    assert not admission.ok
    assert admission.reason == Refusal.MALFORMED_ENVELOPE.value


def test_an_edited_message_cannot_be_actioned():
    guard_ = guard()
    env = envelope()
    assert guard_.admit(env, T0).ok
    late = edited(env, content="QQQ 725 calls", edited_at=T0 + 0.5)
    admission = guard_.note_decision(late, T0 + 1)
    assert not admission.ok
    assert admission.reason == Refusal.CONTENT_CHANGED.value
    # A same-text edit stamped after admission is the edit-after-decision case.
    same_text = guard_.note_decision(edited(env, edited_at=T0 + 0.5), T0 + 1)
    assert same_text.reason == Refusal.EDITED_AFTER_DECISION.value


# ── verify_before_send: the abort seam ─────────────────────────────────────
def test_verify_before_send_passes_an_unchanged_message():
    guard_ = guard()
    env = envelope()
    admitted = guard_.admit(env, T0)
    assert guard_.note_decision(env, T0).ok
    verified = guard_.verify_before_send(env, admitted, T0 + 2)
    assert verified.ok and verified.detail == "verified_before_send"


def test_verify_before_send_aborts_on_an_edit_that_changed_the_text():
    guard_ = guard()
    original = envelope()
    admitted = guard_.admit(original, T0)
    assert guard_.note_decision(original, T0).ok
    changed = edited(original, content="QQQ 725 calls", edited_at=T0 + 1)
    verified = guard_.verify_before_send(changed, admitted, T0 + 1)
    assert not verified.ok
    assert verified.reason == Refusal.CONTENT_CHANGED.value
    assert "aborting" in verified.detail


def test_verify_before_send_aborts_on_an_edit_after_the_decision():
    guard_ = guard()
    env = envelope()
    admitted = guard_.admit(env, T0)
    assert guard_.note_decision(env, T0).ok
    same_text_later_edit = edited(env, edited_at=T0 + 5)
    verified = guard_.verify_before_send(same_text_later_edit, admitted, T0 + 6)
    assert not verified.ok
    assert verified.reason == Refusal.EDITED_AFTER_DECISION.value


def test_verify_before_send_aborts_on_a_delete():
    guard_ = guard()
    env = envelope()
    admitted = guard_.admit(env, T0)
    assert guard_.note_decision(env, T0).ok
    assert guard_.on_delete(env.message_id, T0 + 1).reason == Refusal.DELETED.value
    verified = guard_.verify_before_send(env, admitted, T0 + 2)
    assert not verified.ok
    assert verified.reason == Refusal.DELETED.value


def test_verify_before_send_refuses_without_an_admission():
    verified = guard().verify_before_send(envelope(), Admission(False, "deleted"), T0)
    assert not verified.ok
    assert verified.reason == Refusal.CONTENT_CHANGED.value


# ── the filter: drop before parsing, revoke after action ───────────────────
def test_filter_create_admits_an_allowed_message():
    verdict = ProvenanceFilter(guard()).filter_create(envelope(), T0)
    assert verdict.action is FilterAction.ADMIT and verdict.ok
    assert verdict.to_dict()["action"] == "ADMIT"


def test_filter_create_drops_a_denied_message_before_parsing():
    verdict = ProvenanceFilter(guard()).filter_create(envelope(author_id="stranger"), T0)
    assert verdict.action is FilterAction.DROP
    assert verdict.admission.reason == Refusal.AUTHOR_UNKNOWN.value


def test_filter_update_revokes_an_actioned_message_on_a_real_edit():
    guard_ = guard()
    filter_ = ProvenanceFilter(guard_)
    env = envelope()
    assert filter_.filter_create(env, T0).ok
    assert filter_.record_decision(env, T0).ok
    verdict = filter_.filter_update(edited(env, content="QQQ 725 calls", edited_at=T0 + 1), T0 + 1)
    assert verdict.action is FilterAction.REVOKE and verdict.must_abort
    assert verdict.admission.reason == Refusal.CONTENT_CHANGED.value
    assert guard_.is_revoked(env.message_id)
    # A later create of the same id cannot be admitted again.
    assert filter_.filter_create(env, T0 + 2).action is FilterAction.DROP


def test_filter_update_revokes_on_an_edit_after_the_decision():
    guard_ = guard()
    filter_ = ProvenanceFilter(guard_)
    env = envelope()
    filter_.filter_create(env, T0)
    filter_.record_decision(env, T0)
    verdict = filter_.filter_update(edited(env, edited_at=T0 + 3), T0 + 3)
    assert verdict.action is FilterAction.REVOKE
    assert verdict.admission.reason == Refusal.EDITED_AFTER_DECISION.value


def test_filter_update_drops_an_update_for_an_unknown_message():
    verdict = ProvenanceFilter(guard()).filter_update(edited(envelope()), T0)
    assert verdict.action is FilterAction.DROP


def test_filter_update_drops_a_no_op_update():
    guard_ = guard()
    filter_ = ProvenanceFilter(guard_)
    env = envelope()
    filter_.filter_create(env, T0)
    verdict = filter_.filter_update(edited(env, edited_at=None), T0 + 1)
    assert verdict.action is FilterAction.DROP
    assert verdict.admission.reason == Refusal.DUPLICATE.value


def test_filter_delete_revokes_an_actioned_message():
    guard_ = guard()
    filter_ = ProvenanceFilter(guard_)
    env = envelope()
    filter_.filter_create(env, T0)
    filter_.record_decision(env, T0)
    verdict = filter_.filter_delete(env.message_id, T0 + 1)
    assert verdict.action is FilterAction.REVOKE
    assert verdict.admission.reason == Refusal.DELETED.value


def test_filter_delete_drops_an_unknown_message():
    verdict = ProvenanceFilter(guard()).filter_delete("never-seen", T0)
    assert verdict.action is FilterAction.DROP
    assert verdict.admission.reason == Refusal.DELETED.value


def test_observe_dispatches_the_three_event_types_and_drops_the_rest():
    filter_ = ProvenanceFilter(guard())
    env = envelope()
    assert filter_.observe("MESSAGE_CREATE", env, T0).action is FilterAction.ADMIT
    verdict = filter_.observe("MESSAGE_REACTION_ADD", env, T0)
    assert verdict.action is FilterAction.DROP
    assert verdict.admission.reason == Refusal.MALFORMED_ENVELOPE.value


def test_verify_before_send_through_the_filter_is_the_same_guard():
    guard_ = guard()
    filter_ = ProvenanceFilter(guard_)
    env = envelope()
    admitted = filter_.filter_create(env, T0).admission
    assert filter_.verify_before_send(env, admitted, T0).ok


def test_from_discord_payload_extracts_identity_and_reference():
    payload = {
        "id": "msg-9",
        "channel_id": CHANNEL,
        "content": CONTENT,
        "author": {"id": AUTHOR, "username": "hone"},
        "edited_timestamp": "2026-09-08T14:00:00Z",
        "message_reference": {"message_id": "msg-8"},
    }
    env = MessageEnvelope.from_discord_payload(payload)
    assert env.message_id == "msg-9" and env.author_id == AUTHOR
    assert env.author_name == "hone"
    assert env.reference_message_id == "msg-8"
    assert env.edited_at is not None
    assert env.content_hash == content_hash_for(
        CONTENT, message_id="msg-9", reference_message_id="msg-8"
    )


def test_from_discord_payload_marks_a_delete_and_refuses_garbage():
    env = MessageEnvelope.from_discord_payload(
        {"id": "msg-3", "channel_id": CHANNEL}, event_type="MESSAGE_DELETE"
    )
    assert env.deleted is True
    with pytest.raises(ProvenanceError):
        MessageEnvelope.from_discord_payload("not a payload")


# ── the journal: append, replay, refuse corruption ─────────────────────────
def journal_with_rows(tmp_path: Path) -> MessageJournal:
    journal = MessageJournal(path=tmp_path / "provenance.jsonl")
    journal.record(kind="admit", message_id="m1", action="accept", content_hash="a" * 64, ts=T0)
    journal.record(
        kind="decision", message_id="m1", action="accept", content_hash="a" * 64, ts=T0 + 1
    )
    journal.record(
        kind="delete", message_id="m1", action="revoke", reason=Refusal.DELETED.value, ts=T0 + 2
    )
    return journal


def test_the_journal_roundtrips_through_its_file(tmp_path):
    journal = journal_with_rows(tmp_path)
    path = journal.path
    assert path is not None
    replayed = MessageJournal.load(path)
    assert [entry.seq for entry in replayed.entries] == [1, 2, 3]
    assert [entry.kind for entry in replayed.entries] == ["admit", "decision", "delete"]
    assert replayed.state()["m1"].revoked is True
    assert replayed.verify_file() == []


def test_a_torn_final_journal_line_is_tolerated_and_named(tmp_path):
    journal = journal_with_rows(tmp_path)
    path = journal.path
    assert path is not None
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"v": 1, "seq": 4, "ts":')
    replayed = MessageJournal.load(path)
    assert replayed.torn_lines == 1
    assert len(replayed.entries) == 3
    problems = replayed.verify_file()
    assert any("torn final append" in problem for problem in problems)


def test_mid_file_journal_corruption_is_refused(tmp_path):
    journal = journal_with_rows(tmp_path)
    path = journal.path
    assert path is not None
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(lines[0] + "\nnot json\n" + lines[2] + "\n", encoding="utf-8")
    with pytest.raises(ProvenanceError) as excinfo:
        MessageJournal.load(path)
    assert excinfo.value.reason is ErrorReason.JOURNAL_CORRUPT


def test_a_wrong_journal_schema_version_is_refused(tmp_path):
    path = tmp_path / "provenance.jsonl"
    row = {"v": 2, "seq": 1, "ts": T0, "kind": "admit", "message_id": "m1", "action": "accept"}
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ProvenanceError) as excinfo:
        MessageJournal.load(path)
    assert excinfo.value.reason is ErrorReason.JOURNAL_CORRUPT


def test_a_missing_journal_is_empty_not_an_error(tmp_path):
    journal = MessageJournal.load(tmp_path / "none.jsonl")
    assert journal.entries == [] and journal.torn_lines == 0
    assert journal.verify_file() == [f"provenance journal not found: {tmp_path / 'none.jsonl'}"]


def test_a_journal_seq_gap_is_a_verify_problem(tmp_path):
    journal = journal_with_rows(tmp_path)
    path = journal.path
    assert path is not None
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(lines[0] + "\n" + lines[2] + "\n", encoding="utf-8")
    problems = MessageJournal(path=path).verify_file(path)
    assert any("seq" in problem for problem in problems)
    # Loading refuses the gap: a lost admission row is not repaired silently.
    with pytest.raises(ProvenanceError) as excinfo:
        MessageJournal.load(path)
    assert excinfo.value.reason is ErrorReason.JOURNAL_CORRUPT


def test_journal_entries_are_validated_on_read():
    with pytest.raises(ProvenanceError, match="kind"):
        JournalEntry.from_dict({"seq": 1, "ts": T0, "kind": "teleport", "message_id": "m1"})
    with pytest.raises(ProvenanceError, match="seq"):
        JournalEntry.from_dict({"seq": True, "ts": T0, "kind": "admit", "message_id": "m1"})


def test_the_filter_writes_admit_decision_edit_and_delete_rows(tmp_path):
    journal = MessageJournal(path=tmp_path / "provenance.jsonl")
    filter_ = ProvenanceFilter(guard(journal=journal))
    env = envelope()
    filter_.filter_create(env, T0)
    filter_.record_decision(env, T0 + 1)
    filter_.filter_update(edited(env, content="QQQ 725 calls", edited_at=T0 + 2), T0 + 2)
    kinds = [entry.kind for entry in journal.entries]
    assert "admit" in kinds and "decision" in kinds
    assert "edit" in kinds and "revoke" in kinds


def test_a_restarted_guard_replays_decisions_and_revocations(tmp_path):
    journal = MessageJournal(path=tmp_path / "provenance.jsonl")
    first = ProvenanceGuard(policy=base_policy(), journal=journal, clock=lambda: T0)
    env = envelope()
    admitted = first.admit(env, T0)
    assert admitted.ok
    assert first.note_decision(env, T0).ok
    assert first.on_delete(env.message_id, T0 + 1).reason == Refusal.DELETED.value

    replayed = ProvenanceGuard.from_journal(journal, policy=base_policy(), clock=lambda: T0 + 5)
    assert replayed.is_actioned(env.message_id)
    assert replayed.verify_before_send(env, admitted, T0 + 5).ok is False
    reattempt = replayed.admit(env, T0 + 5)
    assert reattempt.reason == Refusal.DELETED.value


def test_a_restart_does_not_re_admit_an_actioned_message(tmp_path):
    journal = MessageJournal(path=tmp_path / "provenance.jsonl")
    first = ProvenanceGuard(policy=base_policy(), journal=journal, clock=lambda: T0)
    env = envelope()
    first.admit(env, T0)
    first.note_decision(env, T0)
    replayed = ProvenanceGuard.from_journal(journal, policy=base_policy())
    assert replayed.admit(env, T0 + 5).reason == Refusal.DUPLICATE.value


def test_a_delete_before_any_decision_survives_a_restart(tmp_path):
    """A deleted message must not be re-admitted just because no order existed yet."""
    journal = MessageJournal(path=tmp_path / "provenance.jsonl")
    first = ProvenanceGuard(policy=base_policy(), journal=journal, clock=lambda: T0)
    assert first.on_delete("msg-x", T0).reason == Refusal.DELETED.value
    replayed = ProvenanceGuard.from_journal(journal, policy=base_policy())
    assert replayed.is_revoked("msg-x")
    assert replayed.admit(envelope(message_id="msg-x"), T0 + 1).reason == Refusal.DELETED.value


# ── the closed admission type and rendering ────────────────────────────────
def test_a_refusal_must_carry_a_named_reason():
    with pytest.raises(ProvenanceError, match="refusal reason"):
        Admission(False, "because-i-said-so")
    with pytest.raises(ProvenanceError, match="must not carry"):
        Admission(True, Refusal.DUPLICATE.value)


def test_render_helpers_name_the_state():
    policy = base_policy()
    assert "authority" in render_policy(policy)
    assert "no author is trusted" in render_policy(None)
    admission = guard().admit(envelope(author_id="stranger"), T0)
    assert "author_unknown" in render_admission(admission)
    verdict = FilterVerdict(FilterAction.DROP, admission)
    assert "DROP" in render_verdict(verdict)
    journal = MessageJournal()
    journal.record(kind="admit", message_id="m1", action="accept", ts=T0)
    assert "1 row" in render_journal(journal)


def test_policy_loads_from_a_real_file(tmp_path):
    path = write_json(
        tmp_path / "authority.json",
        {
            "version": 1,
            "authors": {AUTHOR: "TRUSTED"},
            "channels": [CHANNEL],
            "trusted_bypass_unknown": False,
        },
    )
    policy = AuthorPolicy.load(path)
    assert policy is not None and policy.tier_for(AUTHOR) is TrustTier.TRUSTED
    admission = ProvenanceGuard(policy=policy, clock=lambda: T0).admit(envelope(), T0)
    assert admission.ok


def test_guard_from_policy_file_fails_closed_on_a_missing_file(tmp_path):
    guard_ = ProvenanceGuard.from_policy_file(tmp_path / "missing.json", clock=lambda: T0)
    admission = guard_.admit(envelope(), T0)
    assert not admission.ok
    assert admission.reason == Refusal.AUTHOR_UNKNOWN.value
