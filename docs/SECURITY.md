# Security and repository hygiene

## Current exposure

This project contains strategy rules, Discord identifiers, operational incident data, and historical
signal material. It should be treated as sensitive operational data even when no credential is
present.

## Required operator actions

1. Make the repository private.
2. Review Git history, not only `HEAD`, for broker identifiers and secrets.
3. Rotate/revoke anything credential-like that was ever committed.
4. Purge sensitive history with `git-filter-repo` or BFG from a trusted workstation.
5. Require pull requests + CI on the default branch.
6. Keep `.env`, tokens, cookies, browser profiles, account identifiers, and raw private exports out
   of Git.
7. Prefer GitHub environments/secrets or runtime secret managers for deployed services.

`./scripts/audit_public_history.sh` can help identify historical string exposure locally.

## Threat model

Protect against:

- duplicate Discord delivery/reconnect replay;
- stale messages resurrecting a closed generation;
- out-of-order follow-ups;
- wrong-channel or wrong-guild messages;
- compromised/incorrect author association;
- parser ambiguity;
- process crash between persistence and effect creation;
- queue starvation by research/chat tasks;
- stale market data;
- accidental live execution from scanner/research output;
- secrets committed to repository history.

## Fail-closed rules

- unsupported/ambiguous prose -> review;
- unassociated follow-up -> review;
- generation mismatch -> reject/review;
- system halt -> no new money-path effects;
- live broker submission -> unavailable in this package.
