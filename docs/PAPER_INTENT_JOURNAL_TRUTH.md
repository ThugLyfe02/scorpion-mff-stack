# Paper intent and evidence integrity gate

The forward equity service previously used a test double that returned PAPER_FILLED
even for resting limits and exits without a price. It now records PAPER_INTENT_ONLY,
separates requested limit from fill price, and labels chase and exit alternatives
conditional. The legacy replay broker and frozen forward strategy remain unchanged.

The journal is bounded, fails visibly on saturation or writer/fsync failure,
and never silently restarts into an existing session file. Close proves drain;
asynchronous decision timing is distinct from durable ack timing. A proper
resume protocol must restore working calls and watermarks before same-file reuse.

Discord ingress shares supervision with the session. Unexpected feed/ingress
termination is a session fault; an explicit author allowlist is required for the
Discord-enabled service. No real credentials or order submission are involved.

Remaining: causal equity fill-state simulation, fees, real licensed feed
validation, complete restart recovery, and external paper comparison. This
change does not claim to fill or certify trades.
