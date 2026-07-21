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
- Phase 3B foundation: schema-versioned billing and usage contracts; integer credit microunits; append-only ledger; reservation/capture/release lifecycle; immutable example rate-card versions; transactional and idempotent financial mutations; tenant-isolated billing APIs; and AI Gateway billing lifecycle integration
- Durable identity directory and control-plane repositories, policy promotion, and workload-identity integration
- Durable billing and identity repositories, reservation expiry/reconciliation workers, rate-card approval, storage, notification, analytics, and durable audit services
- Tenant administration API and policy promotion workflow
- OpenTelemetry traces, metrics, SLOs, alerting, and operational runbooks

Exit: all named UWO products can integrate through versioned contracts and platform SLOs.

Phase 3B exit: all local validation passes without credentials or live providers; insufficient balances deny execution, successful safe usage captures once, failures release credit, and retries cannot duplicate charges. Production billing remains blocked on durable transactional storage, workload identity, reconciliation, approved pricing governance, immutable audit ingestion, and operational controls.

## Phase 4 — Regional production readiness

- Region-isolated deployments and data stores
- Automated residency and disaster-recovery evidence
- Workload identity, key rotation, penetration testing, and compliance controls
- Health-aware routing with deterministic snapshots and cost governance

Exit: production launch criteria, recovery objectives, and regional compliance evidence are approved.
