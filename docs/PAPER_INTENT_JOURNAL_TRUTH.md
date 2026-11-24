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


## Follow-up: immutable paper evidence and explicit admission

The existing JSONL writer now freezes records at admission and binds its own byte digest,
record count and sequence to a sidecar receipt only after fsync, close and independent readback.
A missing, torn, changed or reordered journal cannot verify. The receipt is local evidence,
not a signature or proof of source authenticity. Use:

```sh
python -m scorpion verify-paper-journal --journal /private/session.jsonl
```

The forward service records content/revision hashes and source/receive timestamps, the
locked rules hash and the quote actually used for the intent. It rejects foreign guilds,
noncausal messages and unauthorized authors. Exact creates are deduplicated, and a conflicting
create trips the existing latch. Revisions retain the existing ignore policy. Bounded Discord
queue overflow explicitly reaches supervision instead of relying on callback exceptions.

A valid receipt establishes journal closure and byte identity only. No fill or profitability
is asserted. Raw private messages and market captures must remain outside Git. Crash recovery
and provider/venue authentication remain external prerequisites.

## Crash recovery checkpoint

The writer now fsyncs the journal batch, atomically publishes its verified-prefix checkpoint,
then issues durable callbacks. Directory entries are fsync'd on the POSIX/systemd deployment;
other platforms do not claim equivalent power-loss directory durability. The sidecar is not a
clean-close receipt or authenticated signature. Offline recovery requires the writer to be stopped:

```sh
python -m scorpion recover-paper-journal --journal /private/session.jsonl --out /private/recovery
```

The verified prefix and any unverified trailing bytes are preserved separately, with source and
checkpoint digests; the original file is untouched. A missing or mismatched checkpoint refuses
rather than guessing which events were durable. No recovered file is represented as a clean run.
This is evidence recovery, not automatic working-call resume or external execution recovery.


## Point-in-time research features and conditional alternatives

Use the existing causal_features and counterfactual modules to extract a sealed journal's
unlabeled research features or compare its recorded initial and conditional-chase intentions:

```sh
python -m scorpion paper-intent-features --journal /private/session.jsonl
python -m scorpion paper-intent-alternatives --journal /private/session.jsonl
```

Every accepted row binds the journal bytes, earlier decision sequence, message revision and
content fingerprints, rules hash, quote event clock, receive-to-decision age, limit and quantity.
Missing/noncausal evidence is refused, not imputed. The two alternatives are paired by call ID;
only requested-limit, quantity and timing differences are measurable. No execution or P&L delta
is imputed. No row is a labeled TrainingRow and no optimizer is invoked. Authenticated venue or
adjudicated outcome evidence and independent qualification remain prerequisites for learning.
