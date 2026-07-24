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
- `services/platform_tenant_admin`: resumable tenant onboarding, suspension, reactivation, decommission planning, and operational profiles through provider-neutral service clients.
- `services/platform_governance`: immutable policy drafts, approvals, releases, environment promotions, history, and rollback.
- `services/platform_operations`: allowlisted telemetry, health, SLO/error-budget evaluation, alerts, incidents, runbooks, and maintenance windows.
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

Phase 3C separates data-plane support services from tenant administration, billing, and provider execution. Storage records provider-neutral metadata and immutable object versions; raw bytes remain behind `BlobStore`. Malware results append to immutable scan history and deterministic current status is derived from that history, never by rewriting `ObjectVersion`. Each scanner source ID is an immutable replay key whose fingerprint binds verified executor, tenant, object version, and status. Exact replay returns the original scan; conflicting cross-object, cross-tenant, or cross-status reuse fails closed; scan insertion, idempotency record, and malware event share one UnitOfWork. Upload expiry and retention use parsed UTC instants. Restricted/regulated retention is future-dated; shortening requires a reasoned, audited platform-admin override. Download authorization has bounded positive TTL and is unavailable when region, retention, legal-hold, scan, or integrity policy cannot be established.

Notifications use immutable template versions and a transactional outbox. A notification and its delivery request commit atomically. A dispatcher transactionally claims an eligible lease, commits, calls the provider outside the UnitOfWork with a stable idempotency key, and transactionally finalizes the result. Generic outbox claim reports exhausted work but does not dead-letter an owning aggregate. When a final claimed lease expires, notification reconciliation performs no provider call and atomically writes the final immutable attempt, one dead letter, `NotificationStatus.DEAD_LETTERED`, and `OutboxStatus.DEAD_LETTERED`. Repeated workers replay that terminal result, preserving notification/outbox state alignment. Destinations are opaque references and webhook references require an injected tenant-scoped allowlist.

Analytics events contain a fixed operational schema rather than arbitrary metadata. The service replaces caller `recorded_at`, rejects late and excessive-future-skew events, and uses parsed UTC half-open `[start, end)` windows. Metric IDs bind tenant, product, region, event type, and both boundaries. Tenant and platform cross-tenant exports suppress every group below the minimum; cross-tenant output contains aggregate `MetricPoint` values only, never raw events or snapshots.

Durable audit events use explicit scalar fields and tenant-local monotonic sequences. Each SHA-256 hash binds the previous hash and canonical event content. Checkpoint verification walks the complete contiguous chain from genesis and rejects missing or duplicate sequences. Range export first verifies the full source chain; offline export verification rechecks tenant equality, count, contiguous sequence boundaries, previous/current hash linkage, and the canonical content digest recorded by the manifest. Source-event ingestion stores an immutable fingerprint, so exact replay does not append and conflicting reuse fails closed. The verified service executor is always retained as `actor_subject`; pseudonymous provenance remains only in `pseudonymous_subject_id` and cannot become actor identity. Retention and legal-hold metadata are administrative controls, not delete implementations in the test repository.

Provider-neutral `PlatformEvent` records allow the AI Gateway, control plane, billing, storage, notifications, and analytics to feed durable audit and other consumers without depending on a selected broker. Events expose only stable identifiers, UTC/schema metadata, and deeply immutable allowlisted scalars; their deterministic ID fingerprints type, tenant, request, and safe attributes. Business mutation plus outbox insertion is atomic inside owning UnitOfWork implementations, including control-plane tenant-status changes, scan ingestion, and AI Gateway outcome transitions. Dispatch claims an optimistic lease, prevents concurrent processing, enforces retry due time and maximum attempts, recovers expired claims, and acknowledges only after idempotent downstream acceptance. Generic infrastructure never terminalizes an owning business aggregate independently. Production backends must preserve these guarantees; committed in-memory repositories, outcome stores, fake adapters, and publishers are test-only.

Phase 3D preserves those boundaries while adding orchestration and operations. Tenant administration is a persisted saga: each external mutation is addressed through an injected service client with a deterministic idempotency key, and each local claim, receipt, aggregate transition, and event is owned by the tenant-administration UnitOfWork. Cross-service calls never occur through another service’s repository. A crash after an external commit safely retries the same external key; a current lease excludes another worker; terminal workflows cannot reopen; and decommission plans preserve durable evidence rather than invoking deletion.

Policy governance keeps drafts, validation, change requests, approvals, immutable releases, environment promotions, rollback records, idempotency, and outbox records behind one service boundary. Canonical content digests bind deeply immutable secret-free JSON. Promotion compares the release base to the active environment release and uses an optimistic environment version. Production promotion requires platform authority and two distinct directory-valid approvers; high-risk regional, model, retention, audit, billing, and provider-allowlist changes require separated approval. Rollback copies an earlier configuration into a new immutable release and promotion; history is never rewritten.

Operational telemetry is a separate allowlisted metadata plane. Registered service identities and trusted executors ingest deterministic UTC samples. The schema has no content, prompt, output, body, credential, personal-data, stack-trace, or arbitrary exception field. Lateness/skew, non-negative integers, monotonic counters, ordered cumulative histograms, tenant binding, and duplicate fingerprints fail closed. Missing samples produce `UNKNOWN`. Deterministic test exporters run after commit, so exporter failure cannot roll back accepted telemetry.

SLIs cover availability, request success, latency compliance, provider execution, billing capture, notification delivery, storage integrity, audit verification, and outbox dispatch. SLO targets and completeness use basis points; burn rates use integer microunits; windows are explicit UTC half-open intervals. Immutable evaluations retain included counts and maintenance-window evidence. Missing/incomplete data is `UNKNOWN`, error budgets clamp at zero, and historical evaluations are idempotent and immutable.

Alert rules and occurrences, incidents and immutable timelines, runbooks and executions, and maintenance windows share the operations UnitOfWork and outbox. Alert keys deduplicate retries; suppression retains a maintenance reason and never applies to audit-integrity failures. Incident state advances only through the canonical lifecycle and active alert escalation keys identify one incident. Runbook versions permit guidance-only step types and reject executable content; an execution permanently binds one version and records ordered immutable results. Production maintenance has separated request/approval identities, bounded UTC duration, explicit scope, and cannot erase telemetry or change unknown health into healthy.

When release fails, execution emits one redacted `billing-compensation-failed` audit event, returns `billing_compensation_failed`, preserves the original exception as internal cause, and leaves the reservation available for reconciliation. An injected execution-outcome UnitOfWork stores a provider result before capture. After billing capture, one transaction marks the outcome captured and enqueues the mandatory success event. If that transaction rolls back, retry reuses the result, calls capture idempotently, and restores the event without calling the provider. Captured replay verifies the outbox event before returning; failure and compensation-failure states/events are also durable and replayable. Optional telemetry is outside this mandatory boundary. Production replaces the in-memory implementation with durable workflow/outbox state. Control-plane authorization denials and not-found decisions alone translate to billing `403`/`404`; unexpected authorization dependencies propagate to the HTTP boundary as one redacted `500 internal_error`. Usage and platform-event schemas never contain prompts or outputs.

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
- `/v1/tenant-administration` provides authenticated onboarding/suspension/reactivation sagas, decommission plans, workflow continuation, cancellation, and operational profiles.
- `/v1/governance` provides authenticated policy drafts, validation, decisions, immutable releases, promotion, comparison, history, and rollback.
- `/v1/operations` provides authenticated telemetry, health, SLI/SLO, error-budget, alert, incident, runbook, and maintenance boundaries.

Every service response carries `X-Request-ID`. Callers may provide a constrained request ID or allow the service to generate one. List responses use bounded `limit` and continuation-cursor fields. Mutations use scoped `Idempotency-Key` values where replay is supported and explicit optimistic versions for mutable aggregates. Body limits and stable redacted `400/401/403/404/409/413/422/500` errors are enforced at each Phase 3C boundary.

## Architecture Governance

Every implemented component must be listed in `architecture/manifest.json`, reference only canonical capabilities, and point to an existing repository path. CI runs the architecture validator and automated tests on every pull request.
