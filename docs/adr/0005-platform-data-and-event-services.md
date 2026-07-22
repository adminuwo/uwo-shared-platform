# ADR 0005: Platform data and event services

- Status: Accepted
- Date: 2026-07-22

## Context

UWO products need shared object metadata, reliable notification delivery, privacy-safe operational analytics, and durable audit evidence. These capabilities must preserve the Phase 3A identity boundary and Phase 3B transactional/idempotency standards without selecting cloud storage, messaging vendors, brokers, or production databases. Prompts, outputs, file bytes, message bodies, credentials, and payment data must not leak into cross-service events.

## Decision

Create separate `platform_storage`, `platform_notifications`, `platform_analytics`, and `platform_audit` services with schema-versioned canonical contracts in `packages/contracts`. Each service owns repository protocols and a UnitOfWork. Committed thread-safe in-memory repositories implement rollback for tests only; production startup refuses to instantiate them.

Storage is metadata-only. Raw bytes remain behind an injected `BlobStore`; callers never choose storage keys. Object versions append immutably and bind content length, SHA-2 digest, tenant, product, region, creator, and scan state. Regional rules, classification, retention, legal hold, deletion, checksum, and malware state fail closed. Download authorization contains an opaque short-lived token, not a provider URL or credential. Restricted and regulated content requires explicit region, retention, and authorized access controls.

Notification templates are immutable versions. Creation of an eligible notification and its delivery outbox record commits atomically. Delivery is acknowledged only after a provider returns redacted acceptance metadata. Exact deduplication prevents duplicate enqueue; bounded exponential retry has no sleep dependency; permanent or exhausted failures create a dead-letter record and event. Preferences, opt-out, cancellation, suppression, and an injected webhook destination allowlist are evaluated before provider delivery.

Analytics events are append-only and globally unique. Their contract contains only tenant, product, region, an allowlisted operational event type, fixed outcome/bucket/error dimensions, UTC time, and optionally an approved pseudonymous subject. Arbitrary metadata and sensitive content have no field. Half-open UTC windows aggregate integer counters deterministically. Immutable snapshot hashes preserve reproducibility; exports suppress groups below the configured minimum and cross-tenant export is platform-admin only.

Audit uses append-only tenant streams with transactionally allocated monotonic sequence numbers. Every current SHA-256 hash binds the prior hash and canonical allowlisted scalar event content. Verification returns the first invalid sequence; immutable checkpoints bind a verified chain position; filtered export manifests bind event count, range, and integrity hash. Retention and legal-hold contracts preserve governance metadata. Audit events never retain request bodies, content, credentials, stack traces, or arbitrary exceptions.

Use provider-neutral deterministic `PlatformEvent` records to integrate the gateway, control plane, billing, and Phase 3C services. Where a business mutation creates downstream work, the immutable outbox record shares its transaction. Dispatch state is optimistic-versioned; retries preserve event identity; acceptance precedes acknowledgement; poison events become visible dead letters. No broker is selected in this phase.

## Consequences

Tests can prove storage integrity, notification recovery, aggregation privacy, audit tamper detection, transactional rollback, and cross-service deduplication without credentials or live providers. Production enablement still requires regional durable repositories and blob storage, encryption and workload identity, approved malware/retention/legal-hold systems, real notification adapters, durable dispatch/reconciliation workers, integrity monitoring, backup/recovery, and Phase 3D telemetry/SLO/runbooks. This decision does not deploy infrastructure or store sensitive content in analytics or audit.
