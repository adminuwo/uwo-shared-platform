# UWO Shared Platform Architecture

## Purpose

This repository is the canonical shared platform for UWO products.

## Product Consumers

- AISA
- AI Mall
- AISA Connect
- AI Legal Professional
- AI Ads
- AI CashFlow

## Shared Capabilities

- Identity
- Organisation and Tenant
- Roles and Entitlements
- Dashboard Shell
- Billing and Credits
- AI Gateway and Model Router
- Storage
- Notifications
- Analytics
- Audit
- Security
- Connectors
- Knowledge Layer

## Repository Structure

```text
apps/
packages/
services/
infrastructure/
docs/
tooling/
```

## Bootstrap Components

- `packages/contracts`: canonical product and capability identifiers.
- `services/ai_gateway`: pure routing policy plus HTTP health and route boundaries.
- `services/platform_control_plane`: isolated identity, tenant, role, and entitlement administration boundaries.
- `services/platform_billing`: isolated billing-account, credit, reservation, usage, rate-card, and ledger boundaries.
- `infrastructure/config`: reviewed provider catalog and tenant policies.
- `architecture/manifest.json`: machine-readable component ownership and capability mapping.
- `tooling/validate_architecture.py`: manifest-to-contract and filesystem consistency validation.

The AI Gateway filters providers by explicit tenant allowlist, blocklist, requested model, and allowed region. Unique provider priorities produce a stable primary and ordered fallback list. Unknown tenants or requests with no eligible provider fail closed.

Secure execution adds an authenticated edge assertion, verified tenant binding, product/model entitlements, billing authorization, and a provider-neutral input content-safety gate before provider selection. Public routing retains stable UWO model aliases; every provider declares an exact alias-to-provider `model_map`, resolved before secret access or transport. Provider adapters resolve credentials through a secret-manager contract only at execution time. Azure OpenAI and OpenAI Responses API scaffolds are wrapped by bounded timeouts, retry, fallback, and per-provider circuit breakers. Raw Responses JSON is parsed through a shared fail-closed contract, and output passes a second content-safety gate before release. Structured audit events contain allowlisted identifiers and decisions, never prompts, outputs, bearer tokens, or credential values.

Phase 3A keeps tenant administration out of the AI Gateway. Canonical versioned domain contracts live in `packages/contracts`; the platform control plane coordinates authorization and injected tenant, membership, role, entitlement, and policy repositories. Its committed in-memory implementations are test-only. Deployment must supply durable repositories and trusted authentication before the service can start.

Platform-level administration is explicitly allowlisted. Tenant administrators derive deterministic permissions from active memberships and known roles, are bound to their verified tenant, and cannot cross tenant boundaries. The subject directory is revalidated before membership updates, role changes, permission calculation, and membership-derived authorization. Unknown or deprovisioned subjects fail closed immediately. Suspended tenants have no effective entitlement access and only a platform administrator can reactivate them.

Aggregate versions reject stale writes. A provider-neutral UnitOfWork makes tenant creation, entitlement initialization, initial policy creation, and idempotency persistence atomic. A scoped idempotency ledger stores immutable original result snapshots keyed by operation, tenant, actor, and caller key; replay never dereferences current or deleted resources and never emits a second mutation-success audit event. Durable repository implementations must preserve the same rollback and snapshot guarantees.

Policy configuration is deeply immutable canonical JSON with deterministic serialization and fingerprints. Tenant lifecycle remains exclusively authoritative in `Tenant.status`; policy bodies do not mirror operational status. Success and transaction-rollback auditing belongs to the application service, while the HTTP boundary owns exactly one authentication/authorization/resource-denial event and returns redacted `internal_error` responses for unexpected persistence failures.

Phase 3B keeps billing lifecycle out of both the AI Gateway and tenant control-plane handlers. Canonical contracts use integer credit microunits and integer usage quantities only. The platform billing application service coordinates injected account, ledger, reservation, usage, rate-card, and scoped-idempotency repositories behind a provider-neutral UnitOfWork. Committed in-memory repositories are thread-safe, rollback-capable test integrations and are never selected by executable startup.

The credit ledger is immutable and append-only. Entries contain available and reserved deltas, and balances are reproducible from entry order. Optimistic ledger sequence versions serialize all financial mutations and reject stale concurrent reservations before either balance becomes negative. Captured plus released credit cannot exceed a reservation. Active or partially captured reservations can capture usage; any remaining credit can be released. Expired capture requires an explicit audited platform-administrator override.

Rate cards are immutable versions keyed by product, shared UWO model alias, provider, and region. Rates use integer microunits per 1,000 tokens. Input and output components independently round toward positive infinity, and an integer fixed request charge is added. Every usage event binds to one exact version, preserving historical calculations after later rate-card changes. Repository example pricing is test-only.

The Gateway consumes a provider-neutral authorize/reserve/capture/release interface. Reservation occurs after input safety and routing authorization but before provider transport. Successful output passes the post-execution safety gate before token usage is captured; unused credit is released. Provider failure or output-safety denial releases the reservation. The usage schema carries only allowlisted identifiers, region, integer token counts, rate-card version, and charge, never prompts or outputs.

## API Boundaries

- `GET /healthz` returns process health.
- `POST /v1/route` authenticates and authorizes a tenant, product, model, and region and returns a deterministic routing plan.
- `POST /v1/execute` applies authentication, entitlement, billing, and routing policy before invoking a provider adapter.
- `GET /healthz` on the platform control plane returns process health without exposing tenant data.
- `/v1/tenants` and tenant-scoped `/v1` subresources provide authenticated tenant lifecycle, membership, role, permission, entitlement, and policy-version administration.
- `/v1/billing/accounts` and tenant-scoped billing subresources provide authenticated account lifecycle, balances, grants, adjustments, refunds, usage, ledger, and rate-card reads.
- `/v1/billing/reservations` provides authenticated internal reserve, capture, and release operations for explicitly trusted executors.

Every service response carries `X-Request-ID`. Callers may provide a constrained request ID or allow the service to generate one. Control-plane and billing list responses use bounded `limit` and continuation-cursor fields. Billing mutations require an `Idempotency-Key` and explicit optimistic versions.

## Architecture Governance

Every implemented component must be listed in `architecture/manifest.json`, reference only canonical capabilities, and point to an existing repository path. CI runs the architecture validator and automated tests on every pull request.
