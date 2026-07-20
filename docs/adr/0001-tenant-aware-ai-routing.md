# ADR 0001: Deterministic tenant-aware AI routing

- Status: Accepted
- Date: 2026-07-20

## Context

UWO products need a shared model-routing decision that respects tenant provider restrictions and data residency. A bootstrap must be testable without provider credentials and must not make nondeterministic choices that complicate audit and incident replay.

## Decision

Maintain a versioned provider catalog and explicit tenant policies in configuration. Filter providers by tenant allowlist, blocklist, requested model, and region, then select by a unique numeric priority with provider ID as a stable secondary ordering. Return the remaining eligible providers as an ordered fallback plan. Unknown tenants and unsatisfied constraints fail closed.

The HTTP service exposes `GET /healthz` and `POST /v1/route`. Routing remains a pure function; provider invocation will be implemented behind adapters in a later phase.

## Consequences

Routing decisions are reproducible, policy violations are rejected before provider access, and tests need no cloud credentials. Policy configuration becomes security-sensitive and must be reviewed and deployed atomically. Static priority does not account for live provider health, cost, or latency; future health-aware routing must preserve deterministic inputs and an auditable decision record.
