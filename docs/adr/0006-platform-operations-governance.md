# ADR 0006: Platform operations governance, SLOs, incidents, and runbooks

- Status: Accepted
- Date: 2026-07-24

## Context

The merged identity, billing, data, event, and provider foundations need governed tenant orchestration, immutable policy promotion, privacy-safe operational telemetry, reliability objectives, incident evidence, and executable-free operating guidance. Cross-service onboarding and suspension cannot be a distributed database transaction. Policy release history must survive retries and rollback. Telemetry and operating records must never become a new path for tenant content, credentials, personal data, or arbitrary code.

Phase 3D must establish these boundaries without selecting a production database, observability vendor, incident platform, paging service, broker, deployment system, or remote-execution mechanism.

## Decision

Create three separate services with canonical schema-versioned contracts in `packages/contracts`.

`platform_tenant_admin` is a persisted saga coordinator. It calls the control plane, billing, notifications, governance, and operations through injected provider-neutral client protocols and never through their repositories. A workflow has an immutable ID, deterministic ordered step IDs, stable per-step request/idempotency keys, optimistic versions, exclusive expiring claims, immutable external-result receipts, and explicit pending/running/blocked/failed/completed/cancelled states. A worker commits its claim before an external call, then records the receipt and transition in a new transaction. A crash safely repeats the same external key. Cancellation cannot pre-empt a claimed external mutation, and completed workflows cannot reopen. Compensation is permitted only when a future client explicitly declares a reversible operation; no automatic compensation deletes durable evidence. Decommissioning creates a preservation plan only.

`platform_governance` owns policy drafts, validation, change requests, approvals, releases, environment promotions, rollback records, idempotency, and outbox events. Policy content is deeply immutable canonical JSON. It excludes secrets, tokens, credentials, endpoints, and executable fields at every nesting level. Each immutable release carries a canonical SHA-256 digest and compatibility version. Development, staging, and production promotion compare the release base to the active release and atomically append a new promotion under an optimistic environment version. Production requires platform authority and at least two distinct directory-valid approvers. Proposers cannot self-approve, rejected changes cannot promote, and high-risk regional, entitlement, retention, audit, billing, and provider-allowlist changes require separated approval. Rollback creates a new immutable release and promotion referencing earlier content; it never rewrites an active release or history.

`platform_operations` owns registered service identities, metric definitions and samples, dependency health, snapshots, telemetry checkpoints, SLI/SLO definitions and evaluations, alert rules and occurrences, incidents and timelines, runbooks and executions, maintenance windows, idempotency, and outbox events. Telemetry accepts an allowlisted scalar operational schema only. Deterministic sample IDs, UTC bounds, monotonic counters, cumulative ordered histograms, tenant isolation, service allowlisting, and exact replay prevent ambiguous counts. Missing telemetry is `UNKNOWN`. Export runs after the owning transaction, so exporter failure cannot undo accepted state.

SLO targets and completeness are integer basis points; burn rates are integer microunits. Windows are explicit UTC intervals. Evaluations are immutable and idempotent, maintenance exclusions remain evidence, missing or incomplete data is unknown, and error budgets clamp at zero. Alerts retain deduplication and suppression evidence; audit-integrity failures cannot be suppressed. Active alert escalation keys identify one incident. Incidents follow open, acknowledged, mitigating, resolved, and closed states with immutable timeline entries and no delete operation.

Runbooks are versioned guidance, not remote execution. Allowed steps are manual checks, read-only queries, approvals, communications, mitigation instructions, verification, and escalation. Contracts reject shell commands, scripts, SQL, cloud commands, arbitrary code, binaries, and credentials. Executions permanently bind one immutable version, enforce ordered results, and cannot mutate after completion or abort.

Maintenance windows have bounded UTC duration, explicit service/tenant/environment scope, stable reason codes, and optimistic lifecycle versions. Production requester and approver must differ. Maintenance cannot erase telemetry, suppress audit integrity, or turn unknown health into healthy.

All new business mutations that publish an operational event write state and an outbox record in the same owning UnitOfWork. External telemetry and notification publishing is failure-isolated after commit. Authorization reuses Phase 3A, including tenant isolation, directory revalidation, explicit service executors, platform-only cross-tenant/production authority, and stable redacted HTTP/audit ownership.

## Consequences

The platform can test restart-safe tenant onboarding, non-duplicating retries, approval-separated policy promotion, immutable rollback history, allowlisted telemetry, deterministic fixed-point SLOs, alert/incident deduplication, version-bound runbooks, maintenance evidence, rollback, and redacted HTTP failure behavior without credentials or external services.

The committed in-memory repositories, service clients, exporters, clocks, and outboxes are test integrations only. Production requires regional durable workflow/governance/operations transactions, durable outbox workers, workload identity, policy signing and release approval operations, approved telemetry storage/export, backup and recovery, on-call ownership, exercised incident/runbook processes, retention governance, and Phase 4 regional infrastructure. This decision adds no production database, vendor, paging system, broker, remote execution, Kubernetes, Terraform, cloud deployment, or credentials.
