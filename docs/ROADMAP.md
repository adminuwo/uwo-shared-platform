# Phased Implementation Roadmap

## Phase 1 — Bootstrap foundation (complete)

- Canonical product and capability contracts
- Tenant-aware deterministic model routing and ordered fallback
- Provider allow/block and regional enforcement
- Health and route HTTP endpoints
- Architecture manifest, validation, tests, CI, security baseline, and ADR

Exit: repository checks pass and policy decisions are reproducible without provider credentials.

## Phase 2 — Secure provider execution (implemented foundation)

- Edge authentication and tenant identity binding
- Model entitlement and billing/credit checks
- Provider adapters, secret-manager integration, timeouts, retries, and circuit breakers
- Structured, redacted audit events and correlation IDs
- Provider-neutral fail-closed pre-execution and post-execution content-safety gates
- External content-safety service integration before production traffic

Exit: controlled internal workloads can execute models with end-to-end auditability after deployment supplies managed secrets, trusted edge assertions, provider endpoints, and durable audit ingestion. External production startup remains blocked until a production content-safety authorizer replaces the included internal/test implementation and Phase 4 controls are satisfied.

## Phase 3 — Platform integration

- Phase 3A foundation: versioned identity/tenancy contracts; isolated administration; rollback-safe UnitOfWork repositories; immutable-result idempotency ledger; subject-directory revalidation; canonical immutable policies; optimistic concurrency; and redacted audit/error boundaries
- Phase 3B foundation: schema-versioned billing and mandatory provider-usage contracts; integer credit microunits; canonical append-only ledger semantics; version-free gateway receipts with transaction-local lifecycle state; as-of immutable example rate cards; transactional and idempotent financial mutations; tenant-isolated billing APIs; redacted authorization failure ownership; and retry-safe AI Gateway capture/compensation integration
- Phase 3C foundation: metadata-only storage with immutable versions, append-only scan history, and governed retention overrides; claim/lease notification and shared event outboxes with stable downstream idempotency; privacy-safe analytics with server-owned recording time, half-open windows, and thresholded cross-tenant aggregates; full-genesis audit checkpoint verification, canonical export manifests, and idempotent source ingestion; plus restart-safe provider outcomes and failure-isolated event recording from existing services
- Durable identity directory and control-plane repositories, policy promotion, and workload-identity integration
- Durable billing, identity, storage, notification, analytics, audit, and transactional-outbox backends; reservation expiry/reconciliation workers; and rate-card approval
- Tenant administration API and policy promotion workflow
- OpenTelemetry traces, metrics, SLOs, alerting, and operational runbooks

Exit: all named UWO products can integrate through versioned contracts and platform SLOs.

Phase 3B exit: all local validation passes without credentials or live providers; insufficient balances deny execution, successful safe usage captures once, missing usage fails closed, unrelated ledger activity cannot stale gateway capture/release, failures compensate idempotently, and retries cannot repeat provider execution or duplicate charges. Production billing remains blocked on durable transactional storage and workflow recovery, workload identity, reconciliation, approved pricing governance, immutable audit ingestion, and operational controls.

Phase 3C exit: all local validation passes without cloud storage, messaging vendors, brokers, or production databases; storage versions and scans remain immutable and governance-gated; notification provider calls occur outside transactions while leases, recovery, cancellation, and due times remain deterministic; analytics exports contain only sufficiently aggregated allowlisted points; checkpoints verify from genesis and export tampering is detected; provider retries survive process recreation without duplicate capture; and transactional outbox tests prove rollback, concurrency exclusion, restart delivery, and downstream deduplication. Production remains blocked on regional durable repositories/outcomes/outboxes and blob stores, workload identity, encryption/key management, approved retention/legal-hold enforcement, real provider adapters, dispatcher/reconciliation workers, and Phase 3D observability/runbooks.

## Phase 4 — Regional production readiness

- Region-isolated deployments and data stores
- Automated residency and disaster-recovery evidence
- Workload identity, key rotation, penetration testing, and compliance controls
- Health-aware routing with deterministic snapshots and cost governance

Exit: production launch criteria, recovery objectives, and regional compliance evidence are approved.
