"""Scorpion MFF hot path - deterministic Discord -> Robinhood Agentic executor.

Design constraint from docs/HANDOFF.md and live/RULES.lock.md: the live hone must
be EVENT-DRIVEN (Discord Gateway) and deterministic. No LLM in the exit path, no
shared chat queue. The 9/8 post-mortem (31-minute queue lag, ~$295-315 of missed
fills, $0 filled) is the failure this package exists to remove.

Layering, all stdlib:
    types     - wire-format dataclasses, no logic
    parser    - message text -> typed Signal (deterministic, no I/O)
    pricing   - the locked entry-price rule and the +25% far-off gate
    sizing    - the locked clip table, pipe test, max concurrent, add-once
    engine    - the state machine that turns Signals into OrderIntents
    broker    - Broker protocol: PaperBroker + Robinhood Agentic HTTP adapter
    fills     - append-only structured fill log
    killswitch- NO_TRADE file, STOP/stand-down, fail-closed
    health    - per-event latency budget + deadman heartbeat
    gateway   - Discord session resume, heartbeat ACK, sequence-gap alarm, fatal closes
    listener  - Gateway transport -> parser -> engine -> broker, event driven
    replay    - the same pipeline over a recorded tape (offline proof)
    calendar  - the ET clock, sessions, half-days, holidays; fails closed on an unknown year
    expiry    - 0DTE cutoff verdicts and assignment exposure, read-only; the caller acts
    execution - the one-probe transport breaker and the fail-closed spread gate
    ledger    - intent -> terminal matching; a transport failure stays unresolved
    risk      - pre-trade worst-case-loss budget; shrinks size, never grows it
    reconcile - engine book vs venue book; drift halts buys, never exits
    execution_book - confirmed execution state: cumulative acks, partials, restart replay
    venue     - the venue positions feed: a stamped snapshot, refused when it is not evidence

Everything except the live Gateway transport and the RH HTTP call is pure and
unit-tested offline. `listener.WebsocketGateway` needs the optional `websockets`
package; every other path runs on the standard library alone.
"""

from .engine import Engine
from .killswitch import KillSwitch
from .parser import parse
from .pricing import far_off_bid, marketable_limit
from .sizing import Tier, clip_budget, contracts_for, tier_for_channel
from .types import (
    AlertKind,
    Fill,
    OrderIntent,
    Position,
    Quote,
    RawMessage,
    Side,
    Signal,
)

__all__ = [
    "AlertKind",
    "Engine",
    "Fill",
    "KillSwitch",
    "OrderIntent",
    "Position",
    "Quote",
    "RawMessage",
    "Side",
    "Signal",
    "Tier",
    "clip_budget",
    "contracts_for",
    "far_off_bid",
    "marketable_limit",
    "parse",
    "tier_for_channel",
]

__version__ = "0.1.0"
