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
- `services/platform_storage`: metadata-only object lifecycle, immutable versions, integrity, retention, legal-hold, and provider-neutral blob boundaries.
- `services/platform_notifications`: immutable templates, preferences, reliable delivery lifecycle, transactional outbox, and provider-neutral notification boundaries.
- `services/platform_analytics`: privacy-safe allowlisted event ingestion, deterministic aggregate windows, and thresholded snapshots.
- `services/platform_audit`: durable tenant-scoped hash chains, checkpoints, verification, retention metadata, and evidence exports.
- `infrastructure/config`: reviewed provider catalog and tenant policies.
- `architecture/manifest.json`: machine-readable component ownership and capability mapping.
- `tooling/validate_architecture.py`: manifest-to-contract and filesystem consistency validation.

The AI Gateway filters providers by explicit tenant allowlist, blocklist, requested model, and allowed region. Unique provider priorities produce a stable primary and ordered fallback list. Unknown tenants or requests with no eligible provider fail closed.

Secure execution adds an authenticated edge assertion, verified tenant binding, product/model entitlements, billing authorization, and a provider-neutral input content-safety gate before provider selection. Public routing retains stable UWO model aliases; every provider declares an exact alias-to-provider `model_map`, resolved before secret access or transport. Provider adapters resolve credentials through a secret-manager contract only at execution time. Azure OpenAI and OpenAI Responses API scaffolds are wrapped by bounded timeouts, retry, fallback, and per-provider circuit breakers. Raw Responses JSON is parsed through a shared fail-closed contract. Completed text is usable only when explicit token usage is present, complete, non-negative, and internally consistent; zero is valid only when explicitly reported. Output then passes a second content-safety gate before release. Structured audit events contain allowlisted identifiers and decisions, never prompts, outputs, bearer tokens, or credential values.

Phase 3A keeps tenant administration out of the AI Gateway. Canonical versioned domain contracts live in `packages/contracts`; the platform control plane coordinates authorization and injected tenant, membership, role, entitlement, and policy repositories. Its committed in-memory implementations are test-only. Deployment must supply durable repositories and trusted authentication before the service can start.

Platform-level administration is explicitly allowlisted. Tenant administrators derive deterministic permissions from active memberships and known roles, are bound to their verified tenant, and cannot cross tenant boundaries. The subject directory is revalidated before membership updates, role changes, permission calculation, and membership-derived authorization. Unknown or deprovisioned subjects fail closed immediately. Suspended tenants have no effective entitlement access and only a platform administrator can reactivate them.

Aggregate versions reject stale writes. A provider-neutral UnitOfWork makes tenant creation, entitlement initialization, initial policy creation, and idempotency persistence atomic. A scoped idempotency ledger stores immutable original result snapshots keyed by operation, tenant, actor, and caller key; replay never dereferences current or deleted resources and never emits a second mutation-success audit event. Durable repository implementations must preserve the same rollback and snapshot guarantees.

Policy configuration is deeply immutable canonical JSON with deterministic serialization and fingerprints. Tenant lifecycle remains exclusively authoritative in `Tenant.status`; policy bodies do not mirror operational status. Success and transaction-rollback auditing belongs to the application service, while the HTTP boundary owns exactly one authentication/authorization/resource-denial event and returns redacted `internal_error` responses for unexpected persistence failures.

Phase 3B keeps billing lifecycle out of both the AI Gateway and tenant control-plane handlers. Canonical contracts use integer credit microunits and integer usage quantities only. The platform billing application service coordinates injected account, ledger, reservation, usage, rate-card, and scoped-idempotency repositories behind a provider-neutral UnitOfWork. Committed in-memory repositories are thread-safe, rollback-capable test integrations and are never selected by executable startup.

The credit ledger is immutable and append-only. Entry-type semantics are canonical: grants and refunds increase available credit; adjustments change available credit by exactly their signed amount; reservations move equal credit from available to reserved; captures decrease reserved credit; releases move equal credit from reserved to available. The repository revalidates entry semantics, account/tenant binding, IDs, and sequential versions both when appending and deriving balances. Optimistic sequence versions serialize financial mutations before either balance can become negative. Captured plus released credit cannot exceed a reservation. Active or partially captured reservations can capture usage; any remaining credit can be released. Expired capture requires an explicit audited platform-administrator override.

Rate cards are immutable versions keyed by product, shared UWO model alias, provider, and region. Rates use integer microunits per 1,000 tokens. Input and output components independently round toward positive infinity, and an integer fixed request charge is added. Repositories select the newest version with `effective_at <= usage.occurred_at`; exact activation time is inclusive, future cards never activate early, ambiguous same-family effective versions are rejected, and `(effective_at, rate_card_id, version)` is the deterministic cross-family order. Every usage event binds to one exact version, preserving historical calculations after later rate-card changes. Repository example pricing is test-only.

The Gateway consumes a provider-neutral authorize/reserve/capture/release interface. Reservation occurs after input safety and routing authorization but before provider transport. The receipt is an identity token, not an optimistic-version carrier. Gateway mutations load current reservation and balance state within the same UnitOfWork, then capture and release unused credit atomically; unrelated ledger activity, concurrent reservations, and adapter recreation therefore cannot create stale lifecycle writes. Successful output passes the post-execution safety gate and must include mandatory provider usage before capture. Provider failure, missing usage, or output-safety denial invokes an idempotent release.

Phase 3C separates data-plane support services from tenant administration, billing, and provider execution. Storage records provider-neutral metadata and immutable object versions; raw bytes remain behind `BlobStore`. Restricted and regulated objects require explicit region, retention, and authorized access controls. Download authorization is opaque, short-lived, and unavailable for deleted, checksum-invalid, unscanned, or malware-positive content.

Notifications use immutable template versions and a transactional outbox. A notification and its delivery request commit atomically; provider acceptance precedes acknowledgement; retry timing is deterministic exponential backoff; terminal failure creates a dead-letter record and event. Destinations are opaque references and webhook references require an injected tenant-scoped allowlist.

Analytics events contain a fixed operational schema rather than arbitrary metadata. Append-only unique events aggregate into deterministic half-open UTC windows. Snapshot hashes preserve reproducibility, and exports suppress groups below the configured minimum. Cross-tenant exports require explicit platform-admin authorization.

Durable audit events use explicit scalar fields and tenant-local monotonic sequences. Each SHA-256 hash binds the previous hash and canonical event content. Verification identifies the first invalid sequence; checkpoints and export manifests are immutable integrity evidence. Retention and legal-hold metadata are administrative controls, not delete implementations in the test repository.

Provider-neutral `PlatformEvent` publishers allow the AI Gateway, control plane, billing, storage, notifications, and analytics to feed durable audit and other consumers without depending on an in-memory repository or broker. Business mutation plus outbox insertion is atomic inside Phase 3C UnitOfWork implementations; production backends must retain that guarantee. In-memory repositories, fake blob/provider adapters, and collecting publishers are test-only.

When release fails, execution emits one redacted `billing-compensation-failed` event, returns `billing_compensation_failed`, preserves the original exception as internal cause, and leaves the reservation available for reconciliation. When capture fails after provider success, same-process retries reuse the completed provider result and retry only billing capture. Production replaces this bootstrap recovery cache with durable workflow/outbox state. Control-plane authorization denials and not-found decisions alone translate to billing `403`/`404`; unexpected authorization dependencies propagate to the HTTP boundary as one redacted `500 internal_error`. The usage schema carries only allowlisted identifiers, region, integer token counts, rate-card version, and charge, never prompts or outputs.

## API Boundaries

- `GET /healthz` returns process health.
- `POST /v1/route` authenticates and authorizes a tenant, product, model, and region and returns a deterministic routing plan.
- `POST /v1/execute` applies authentication, entitlement, billing, and routing policy before invoking a provider adapter.
- `GET /healthz` on the platform control plane returns process health without exposing tenant data.
- `/v1/tenants` and tenant-scoped `/v1` subresources provide authenticated tenant lifecycle, membership, role, permission, entitlement, and policy-version administration.
- `/v1/billing/accounts` and tenant-scoped billing subresources provide authenticated account lifecycle, balances, grants, adjustments, refunds, usage, ledger, and rate-card reads.
- `/v1/billing/reservations` provides authenticated internal reserve, capture, and release operations for explicitly trusted executors.
- `/v1/uploads` and `/v1/objects` provide authenticated storage metadata and lifecycle operations.
- `/v1/templates` and `/v1/notifications` provide authenticated template, preference, enqueue, delivery, retry, cancellation, and status boundaries.
- `/v1/events` and `/v1/snapshots` on analytics provide allowlisted ingestion and tenant-isolated aggregate reads.
- `/v1/events`, `/v1/verify`, `/v1/checkpoints`, and `/v1/exports` on audit provide durable append and evidence boundaries.

Every service response carries `X-Request-ID`. Callers may provide a constrained request ID or allow the service to generate one. List responses use bounded `limit` and continuation-cursor fields. Mutations use scoped `Idempotency-Key` values where replay is supported and explicit optimistic versions for mutable aggregates. Body limits and stable redacted `400/401/403/404/409/413/422/500` errors are enforced at each Phase 3C boundary.

## Architecture Governance

Every implemented component must be listed in `architecture/manifest.json`, reference only canonical capabilities, and point to an existing repository path. CI runs the architecture validator and automated tests on every pull request.
