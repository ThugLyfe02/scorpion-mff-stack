"""Discord Gateway session lifecycle: identify, resume, heartbeat, and the gap alarm.

The hot path no longer goes silent when the websocket drops. A Gateway connection is a
*session*, not a single ``async with``: Discord expects the client to IDENTIFY once, then
RESUME with the prior ``session_id`` and the last dispatched sequence number after a close.
Without that bookkeeping a routine reconnect starts a fresh session, and every alert posted
during the reconnect is lost - which is the 9/8 failure in transport form.

The rules enforced here:

  * every DISPATCH has a sequence number, and the heartbeat carries the latest one;
  * a heartbeat that is not ACKed is a zombie connection, not a healthy one;
  * op 7 and a resumable op 9 reconnect by RESUME; a non-resumable session re-IDENTIFYs;
  * a **sequence gap** is named and paged, never smoothed over;
  * a fatal close code stops the loop and pages, rather than reconnecting forever.

The state machine is pure and standard-library-only; the websocket loop in
``listener.WebsocketGateway`` consumes it. Keeping the two apart is what makes the
lifecycle testable without a network or the optional ``websockets`` package.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
INTENTS_GUILD_MESSAGES = 1 << 9          # GUILD_MESSAGES
INTENT_MESSAGE_CONTENT = 1 << 15         # MESSAGE_CONTENT (privileged)
DEFAULT_INTENTS = INTENTS_GUILD_MESSAGES | INTENT_MESSAGE_CONTENT

# Discord close codes that a bot cannot recover from without a human changing something:
# authentication failed, invalid shard, sharding required, invalid API version, invalid
# intents, disallowed intents.
FATAL_CLOSE_CODES = frozenset({4004, 4010, 4011, 4012, 4013, 4014})
# A sequence that can no longer be resumed, and a session that timed out: both must
# start over with a fresh IDENTIFY rather than attempting a RESUME.
REIDENTIFY_CLOSE_CODES = frozenset({4007, 4009})
NORMAL_CLOSE_CODE = 1000
ZOMBIE_CLOSE_CODE = 4000


class GatewayState(StrEnum):
    DISCONNECTED = "disconnected"
    HANDSHAKE = "handshake"
    READY = "ready"
    RESUMING = "resuming"
    RECONNECTING = "reconnecting"
    CLOSED = "closed"


class CloseAction(StrEnum):
    """What the transport should do after a connection ends."""

    RESUME = "resume"
    IDENTIFY = "identify"
    FATAL = "fatal"


class GatewayFatalError(RuntimeError):
    """A Gateway failure the bot cannot recover from without operator action."""


@dataclass(frozen=True, slots=True)
class Dispatch:
    """One decoded op-0 DISPATCH, with the sequence checks already applied."""

    event_type: str
    sequence: int
    payload: dict[str, Any]
    gap: bool = False
    duplicate: bool = False
    reason: str = ""


@dataclass
class GatewaySession:
    """The resume/heartbeat state one Discord bot session must carry across sockets."""

    token: str
    intents: int = DEFAULT_INTENTS
    session_id: str | None = None
    resume_url: str = ""
    seq: int | None = None
    state: GatewayState = GatewayState.DISCONNECTED
    heartbeat_interval_s: float = 0.0
    pending_ack: bool = False
    identifies: int = 0
    resumes: int = 0
    gaps: int = 0
    zombies: int = 0
    last_note: str = ""
    next_action: CloseAction = CloseAction.IDENTIFY

    @property
    def can_resume(self) -> bool:
        """A RESUME needs both a session id and a sequence to replay from."""
        return bool(self.session_id) and self.seq is not None

    def begin(self) -> None:
        """Prepare for a new socket without discarding the prior session."""
        self.pending_ack = False
        self.last_note = ""
        self.next_action = (
            CloseAction.RESUME if self.can_resume else CloseAction.IDENTIFY
        )

    def on_hello(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Record the heartbeat interval and return IDENTIFY or RESUME."""
        raw = payload.get("heartbeat_interval")
        if not isinstance(raw, (int, float, str)):
            raise GatewayFatalError(
                f"Gateway HELLO has no usable heartbeat_interval: {raw!r}"
            )
        try:
            interval_ms = float(raw)
        except (TypeError, ValueError) as exc:
            raise GatewayFatalError(
                f"Gateway HELLO has no usable heartbeat_interval: {raw!r}"
            ) from exc
        if interval_ms <= 0:
            raise GatewayFatalError(f"Gateway HELLO interval must be positive: {raw!r}")
        self.heartbeat_interval_s = interval_ms / 1000.0
        self.state = GatewayState.HANDSHAKE
        return self.handshake_frame()

    def handshake_frame(self) -> dict[str, Any]:
        """RESUME when the prior session is still usable, else IDENTIFY."""
        if self.can_resume:
            self.resumes += 1
            self.state = GatewayState.RESUMING
            return {
                "op": 6,
                "d": {
                    "token": self.token,
                    "session_id": self.session_id,
                    "seq": self.seq,
                },
            }
        self.identifies += 1
        self.state = GatewayState.HANDSHAKE
        return {
            "op": 2,
            "d": {
                "token": self.token,
                "intents": self.intents,
                "properties": {
                    "os": "windows",
                    "browser": "scorpion-mff",
                    "device": "scorpion-mff",
                },
            },
        }

    def on_dispatch(self, event: dict[str, Any]) -> Dispatch:
        """Decode a DISPATCH and detect a gap or a redelivery from the sequence alone."""
        event_type = str(event.get("t") or "")
        sequence = event.get("s")
        payload = event.get("d")
        body: dict[str, Any] = payload if isinstance(payload, dict) else {}
        if not isinstance(sequence, int):
            self.gaps += 1
            return Dispatch(
                event_type=event_type,
                sequence=-1,
                payload=body,
                gap=True,
                reason="dispatch_without_sequence",
            )
        gap = self.seq is not None and sequence > self.seq + 1
        duplicate = self.seq is not None and sequence <= self.seq
        if gap:
            self.gaps += 1
        if not duplicate:
            self.seq = sequence
        if event_type == "READY":
            self.session_id = str(body.get("session_id") or "") or None
            resume_url = body.get("resume_gateway_url")
            if isinstance(resume_url, str) and resume_url:
                self.resume_url = resume_url
            self.state = GatewayState.READY
        elif event_type == "RESUMED":
            self.state = GatewayState.READY
        return Dispatch(
            event_type=event_type,
            sequence=sequence,
            payload=body,
            gap=gap,
            duplicate=duplicate,
            reason="sequence_gap" if gap else "",
        )

    def heartbeat_frame(self) -> dict[str, Any]:
        """A heartbeat carries the last sequence so RESUME can replay from exactly there."""
        self.pending_ack = True
        return {"op": 1, "d": self.seq}

    def on_heartbeat_ack(self) -> None:
        self.pending_ack = False

    def on_zombie(self) -> None:
        """A heartbeat went unacknowledged: the socket is a zombie, not a healthy one."""
        self.zombies += 1
        self.last_note = "heartbeat ACK missing (zombie connection)"
        self.next_action = (
            CloseAction.RESUME if self.can_resume else CloseAction.IDENTIFY
        )
        self.state = GatewayState.RECONNECTING

    def on_reconnect(self) -> None:
        self.last_note = "Gateway requested reconnect (op 7)"
        self.next_action = (
            CloseAction.RESUME if self.can_resume else CloseAction.IDENTIFY
        )
        self.state = GatewayState.RECONNECTING

    def on_invalid_session(self, resumable: bool) -> None:
        self.last_note = f"invalid session (resumable={resumable})"
        if not resumable:
            self.forget_session()
        self.next_action = (
            CloseAction.RESUME if self.can_resume else CloseAction.IDENTIFY
        )
        self.state = GatewayState.RECONNECTING

    def on_close(self, code: int) -> CloseAction:
        """Classify a close code; a fatal code stops instead of reconnecting forever."""
        self.last_note = f"Gateway closed: {code}"
        if code in FATAL_CLOSE_CODES:
            self.state = GatewayState.CLOSED
            self.next_action = CloseAction.FATAL
            return self.next_action
        if code in REIDENTIFY_CLOSE_CODES:
            self.forget_session()
        self.next_action = (
            CloseAction.RESUME if self.can_resume else CloseAction.IDENTIFY
        )
        self.state = GatewayState.RECONNECTING
        return self.next_action

    def connect_url(self, default_url: str) -> str:
        """Prefer the resume URL Discord returned in READY; fall back to the default."""
        return self.resume_url or default_url

    def forget_session(self) -> None:
        """Drop everything a RESUME needs. Only a non-resumable close may call this."""
        self.session_id = None
        self.seq = None
        self.resume_url = ""
