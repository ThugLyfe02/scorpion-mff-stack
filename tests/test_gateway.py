"""Gateway session lifecycle: resume, heartbeat ACK, sequence gaps, fatal closes."""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

from scorpion_mff.gateway import (
    CloseAction,
    GatewayFatalError,
    GatewaySession,
    GatewayState,
)
from scorpion_mff.listener import WebsocketGateway

RESUME_URL = "wss://resume.example"
DEFAULT_URL = "wss://default.example"


def ready(seq: int = 1) -> GatewaySession:
    session = GatewaySession(token="token")
    session.begin()
    identify = session.on_hello({"heartbeat_interval": 41250})
    assert identify["op"] == 2
    dispatch = session.on_dispatch(
        {
            "op": 0,
            "t": "READY",
            "s": seq,
            "d": {"session_id": "session-1", "resume_gateway_url": RESUME_URL},
        }
    )
    assert dispatch.gap is False
    return session


def test_identify_then_resume_carries_session_and_last_sequence():
    session = ready(seq=7)
    session.begin()
    frame = session.on_hello({"heartbeat_interval": 41250})
    assert frame["op"] == 6
    assert frame["d"]["session_id"] == "session-1"
    assert frame["d"]["seq"] == 7
    assert session.resumes == 1 and session.identifies == 1
    assert session.state is GatewayState.RESUMING
    assert session.connect_url(DEFAULT_URL) == RESUME_URL


def test_heartbeat_carries_the_last_sequence_and_tracks_the_ack():
    session = ready(seq=7)
    assert session.heartbeat_frame() == {"op": 1, "d": 7}
    assert session.pending_ack is True
    session.on_heartbeat_ack()
    assert session.pending_ack is False


def test_a_sequence_gap_is_named_and_the_resume_point_advances():
    session = ready(seq=1)
    gap = session.on_dispatch({"op": 0, "t": "MESSAGE_CREATE", "s": 4, "d": {}})
    assert gap.gap is True
    assert gap.reason == "sequence_gap"
    assert session.gaps == 1 and session.seq == 4
    following = session.on_dispatch({"op": 0, "t": "MESSAGE_CREATE", "s": 5, "d": {}})
    assert following.gap is False and session.seq == 5


def test_a_redelivered_sequence_is_duplicate_and_does_not_move_the_point():
    session = ready(seq=5)
    same = session.on_dispatch({"op": 0, "t": "MESSAGE_CREATE", "s": 5, "d": {}})
    older = session.on_dispatch({"op": 0, "t": "MESSAGE_CREATE", "s": 4, "d": {}})
    assert same.duplicate is True and older.duplicate is True
    assert session.seq == 5


def test_a_dispatch_without_a_sequence_is_a_gap_not_a_silent_accept():
    session = ready(seq=1)
    gap = session.on_dispatch({"op": 0, "t": "MESSAGE_CREATE", "d": {}})
    assert gap.gap is True
    assert gap.sequence == -1
    assert gap.reason == "dispatch_without_sequence"
    assert session.gaps == 1 and session.seq == 1


def test_a_missing_heartbeat_ack_is_a_zombie_and_reconnects_by_resume():
    session = ready()
    session.heartbeat_frame()
    session.on_zombie()
    assert session.zombies == 1
    assert session.next_action is CloseAction.RESUME
    assert session.state is GatewayState.RECONNECTING
    assert "ACK" in session.last_note


@pytest.mark.parametrize("code", [4004, 4010, 4011, 4012, 4013, 4014])
def test_fatal_close_codes_stop_instead_of_reconnecting(code):
    session = ready()
    assert session.on_close(code) is CloseAction.FATAL
    assert session.state is GatewayState.CLOSED


@pytest.mark.parametrize("code", [4007, 4009])
def test_unresumable_close_codes_forget_the_session_and_identify(code):
    session = ready()
    assert session.on_close(code) is CloseAction.IDENTIFY
    assert session.can_resume is False
    assert session.connect_url(DEFAULT_URL) == DEFAULT_URL


def test_a_normal_close_keeps_the_session_for_resume():
    session = ready()
    assert session.on_close(1000) is CloseAction.RESUME
    assert session.can_resume is True


def test_invalid_session_resumable_keeps_the_session_and_non_resumable_drops_it():
    kept = ready()
    kept.on_invalid_session(True)
    assert kept.next_action is CloseAction.RESUME and kept.can_resume is True

    dropped = ready()
    dropped.on_invalid_session(False)
    assert dropped.next_action is CloseAction.IDENTIFY
    assert dropped.can_resume is False


def test_hello_without_a_usable_interval_is_fatal():
    with pytest.raises(GatewayFatalError):
        GatewaySession(token="token").on_hello({})
    with pytest.raises(GatewayFatalError):
        GatewaySession(token="token").on_hello({"heartbeat_interval": 0})


# ── the socket loop, without a socket ────────────────────────────────────────
class FakeSocket:
    def __init__(self, frames: list[dict]) -> None:
        self.frames = [json.dumps(frame) for frame in frames]
        self.sent: list[dict] = []
        self.closed_code: int | None = None
        self._index = 0

    def __aiter__(self) -> FakeSocket:
        return self

    async def __anext__(self) -> str:
        if self._index >= len(self.frames):
            raise StopAsyncIteration
        frame = self.frames[self._index]
        self._index += 1
        return frame

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def close(self, code: int = 1000) -> None:
        self.closed_code = code


def _message(channel_id: str, message_id: str) -> dict:
    return {
        "op": 0,
        "t": "MESSAGE_CREATE",
        "s": 2,
        "d": {"channel_id": channel_id, "content": "QQQ 719C TODAY @ 1.01",
              "id": message_id},
    }


def test_the_socket_loop_identifies_and_yields_only_allowlisted_messages():
    gateway = WebsocketGateway(token="token", channel_ids={"c1"})
    session = GatewaySession(token="token")
    session.begin()
    ws = FakeSocket([
        {"op": 10, "d": {"heartbeat_interval": 60_000}},
        {"op": 0, "t": "READY", "s": 1,
         "d": {"session_id": "s1", "resume_gateway_url": RESUME_URL}},
        _message("c1", "m1"),
        _message("other", "m2"),
    ])

    async def drive():
        return [message async for message in gateway._session(ws, session, {"c1"})]

    messages = asyncio.run(drive())
    assert ws.sent[0]["op"] == 2
    assert len(messages) == 1 and messages[0].message_id == "m1"
    assert session.seq == 2


def test_the_socket_loop_pages_a_gap_and_skips_a_duplicate():
    alerts: list[tuple[str, str]] = []
    gateway = WebsocketGateway(
        token="token", channel_ids={"c1"},
        alert=lambda reason, note: alerts.append((reason, note)),
    )
    session = GatewaySession(token="token")
    session.begin()
    ws = FakeSocket([
        {"op": 10, "d": {"heartbeat_interval": 60_000}},
        {"op": 0, "t": "READY", "s": 1, "d": {"session_id": "s1"}},
        {"op": 0, "t": "MESSAGE_CREATE", "s": 4,
         "d": {"channel_id": "c1", "content": "first", "id": "m1"}},
        {"op": 0, "t": "MESSAGE_CREATE", "s": 4,
         "d": {"channel_id": "c1", "content": "redelivered", "id": "m1"}},
    ])

    async def drive():
        return [message async for message in gateway._session(ws, session, {"c1"})]

    messages = asyncio.run(drive())
    assert [message.text for message in messages] == ["first"]
    assert alerts and alerts[0][0] == "gateway_gap"
    assert session.gaps == 1 and session.seq == 4


class FakeConnection:
    def __init__(self, socket: FakeSocket) -> None:
        self.socket = socket

    async def __aenter__(self) -> FakeSocket:
        return self.socket

    async def __aexit__(self, *exc_info) -> bool:
        return False


def test_the_transport_reconnects_with_resume_after_a_socket_ends(monkeypatch):
    first = FakeSocket([
        {"op": 10, "d": {"heartbeat_interval": 60_000}},
        {"op": 0, "t": "READY", "s": 1,
         "d": {"session_id": "s1", "resume_gateway_url": RESUME_URL}},
        {"op": 0, "t": "MESSAGE_CREATE", "s": 2,
         "d": {"channel_id": "c1", "content": "one", "id": "m1"}},
    ])
    second = FakeSocket([
        {"op": 10, "d": {"heartbeat_interval": 60_000}},
        {"op": 0, "t": "RESUMED", "s": 3, "d": {}},
        {"op": 0, "t": "MESSAGE_CREATE", "s": 4,
         "d": {"channel_id": "c1", "content": "two", "id": "m2"}},
    ])
    urls: list[str] = []

    class Connect:
        def __init__(self) -> None:
            self.sockets = [first, second]

        def __call__(self, url, max_size=None):
            del max_size
            urls.append(url)
            return FakeConnection(self.sockets.pop(0))

    fake = types.ModuleType("websockets")
    fake.connect = Connect()
    monkeypatch.setitem(sys.modules, "websockets", fake)

    alerts: list[tuple[str, str]] = []
    gateway = WebsocketGateway(
        token="token", channel_ids={"c1"}, url=DEFAULT_URL,
        backoff_s=0.0, max_backoff_s=0.0,
        alert=lambda reason, note: alerts.append((reason, note)),
    )

    async def drive():
        received: list[str] = []
        stream = gateway.messages()
        try:
            async for message in stream:
                received.append(message.text)
                if len(received) == 2:
                    break
        finally:
            await stream.aclose()
        return received

    received = asyncio.run(drive())
    assert received == ["one", "two"]
    assert urls == [DEFAULT_URL, RESUME_URL]
    assert first.sent[0]["op"] == 2 and second.sent[0]["op"] == 6
    assert any(reason == "gateway_reconnect" for reason, _ in alerts)
