# ADR 0004: Billing credits and usage ledger

- Status: Accepted
- Date: 2026-07-21

## Context

Provider execution needs a tenant-isolated credit decision before work begins, a reservation before external cost is incurred, and a final usage charge after a safe response is accepted. Billing records must survive retries, concurrency, later price changes, and operational investigation without storing tenant prompts, model output, credentials, or payment data. Phase 3B must establish these invariants without selecting a production database or payment processor.

## Decision

Implement billing in the separate `services/platform_billing` boundary and keep its canonical schema-versioned contracts in `packages/contracts`. One credit equals 1,000,000 credit microunits. All credit amounts, rates, tokens, and usage quantities are integers; floating-point arithmetic is prohibited. Token rates are expressed per 1,000 tokens. Each input and output component is calculated independently as `ceil(tokens × rate / 1000)`, then the integer fixed request charge is added. This explicit round-toward-positive-infinity rule prevents systematic undercharging and is deterministic across runtimes.

Maintain an immutable append-only ledger. A grant, adjustment, reservation, capture, release, or refund appends exactly one immutable entry containing available and reserved balance deltas. Balances are derived from the ordered ledger and checked atomically: neither available nor reserved credit can become negative. Ledger sequence versions and aggregate versions provide optimistic concurrency; entries have no update or delete contract.

Reservations move from pending or active to partially captured, captured, or released. A partial capture may later be captured again or release its remaining amount. Captured plus released credit can never exceed the original reservation. An expired reservation cannot be captured without an explicit platform-administrator override. The normal gateway releases any unused amount after final capture, and releases the full reservation when provider execution or output safety fails.

Store immutable rate-card versions keyed by product, shared UWO model alias, provider, and region. Usage binds to the exact rate-card ID and version used for calculation, so a later active card cannot alter historical charges. Committed prices are clearly marked test/example data and are not commercial provider prices.

Use one UnitOfWork for each credit grant and ledger entry, reservation and ledger entry, capture plus usage plus ledger entry, release and ledger entry, and refund and ledger entry. Test repositories are thread-safe, snapshot every participant, and roll back on injected failure. Production repositories must provide equivalent atomicity. Scope idempotency by operation, tenant, actor, and caller key; persist immutable original result snapshots. Exact replay returns the original result and emits no duplicate ledger, usage, or success-audit event. Different input under the same scope and key fails with a conflict.

Compose authorization with the identity and tenancy control plane. Explicit platform administrators manage accounts and credits. Active tenant members require `billing.read` to read their tenant. Explicitly configured, directory-revalidated internal executors may reserve, capture, and release. Tenant IDs select resources but never confer authority. Unknown or suspended tenants and suspended or closed billing accounts fail closed for debit activity.

Expose a provider-neutral gateway lifecycle: authorize an estimated charge, reserve before provider execution, capture redacted token usage after output safety passes, and release on failure. Billing usage may contain tenant, product, model aliases, provider/model identifiers where allowed, region, correlation identifiers, integer token counts, rate-card version, and calculated charge. It must never contain prompts, model output, bearer tokens, API keys, provider secrets, request bodies, or payment credentials.

## Consequences

Credit state is reproducible from immutable entries, concurrent reservations cannot overdraw a tenant, historical pricing remains stable, and provider failures do not strand or double-charge reservations. Service and gateway tests require no live provider or credential. The in-memory repositories, in-process gateway adapter, example pricing, and JSON audit sink are test/bootstrap implementations only. Production enablement still requires durable regional transactional storage, workload identity, immutable audit ingestion, rate-card approval, reconciliation, expiration workers, monitoring, and recovery procedures. This decision does not connect a payment gateway or deploy infrastructure.
