# ADR 0003: Identity and tenancy control-plane separation

- Status: Accepted
- Date: 2026-07-20

## Context

UWO products need one canonical definition of tenants, verified subjects, memberships, roles, permissions, entitlements, and policy versions. These resources are security-sensitive administrative state, not AI-provider routing concerns. Placing their lifecycle inside the AI Gateway would couple administrative availability and authorization to provider execution, expand the gateway's privilege, and make tenant isolation harder to audit.

Phase 3A also needs safe mutation behavior before selecting a production database. Concurrent administrators must not silently overwrite state, retries must not duplicate tenant or entitlement grants, and tests must exercise the complete boundary without credentials or infrastructure.

## Decision

Keep versioned domain contracts in `packages/contracts` and implement administration in a separate `services/platform_control_plane` service. Expose authenticated internal `/v1` boundaries and inject authentication, subject verification, audit, and repository dependencies. The executable service will not instantiate test repositories or start as a production service until deployment supplies trusted authentication and durable repository implementations.

Authorize cross-tenant operations only for explicitly configured platform administrators. A tenant administrator must have an active membership in the verified tenant, known role assignments, and the exact required permission. Permissions are the sorted union of permissions from known assigned roles. Unknown resources or role data, inactive memberships, and suspended tenants fail closed. Tenant identifiers supplied by callers select a requested resource but never confer authority.

Separate persistence behind tenant, membership, role, entitlement, policy-version, and scoped idempotency-ledger repository protocols. A provider-neutral UnitOfWork makes tenant creation, entitlement aggregate initialization, initial policy creation, and idempotency persistence one atomic operation. The test-only in-memory implementation snapshots every participating repository and restores all of them when any write or commit path fails. Durable implementations must provide equivalent transaction guarantees.

Maintain aggregate versions and require expected versions for every mutation. Reject stale updates, duplicate memberships, duplicate or missing role assignments, and duplicate entitlements. Scope idempotency keys by operation, tenant, and actor and bind them to canonical request fingerprints. Ledger records store the immutable original operation result, not an identifier or pointer to mutable state. Exact retries return that original result even after a tenant changes or an entitlement is revoked; conflicting reuse fails and replay emits no second success event.

Revalidate membership subjects through the identity directory before membership updates, role assignment or revocation, effective-permission evaluation, and any authorization derived from an existing membership. Caller authentication and target-subject validation remain distinct. A deprovisioned administrator immediately loses authority and a deprovisioned target cannot receive changes.

Canonicalize policy bodies as deeply immutable JSON: recursively sorted objects, immutable arrays, and JSON scalar values only. Reject non-string keys, unsupported objects, and non-finite numbers. Canonical serialization and fingerprints are deterministic. `Tenant.status` is the authoritative operational lifecycle value; policy documents contain configuration metadata and do not duplicate status.

Emit structured audit events from an allowlisted schema. The application service owns successful mutation and transaction-rollback events; the HTTP boundary owns authentication, authorization, and unknown-resource denial events so a request is recorded exactly once. Correlation, actor, tenant, target, resource, outcome, and stable reason identifiers are permitted; bearer tokens, secrets, exception details, and request bodies are not. Unexpected repository failures map to a generic `500 internal_error`, never a caller input error. Expose a provider-neutral effective-entitlement lookup so the AI Gateway can integrate later without coupling the control plane to provider adapters.

## Consequences

Tenant administration has an independently testable security and persistence boundary, deterministic permission evaluation, immediate deprovisioning behavior, atomic provisioning, immutable replay semantics, and auditable mutation/error ownership. The AI Gateway remains focused on secure model routing and execution. Callers must handle conflict responses, retain entity versions, and use stable idempotency keys within their operation/tenant/actor scope.

This phase does not supply a production identity directory, database, deployment, policy-promotion workflow, or durable audit sink. Those integrations must preserve these contracts, tenant isolation, optimistic concurrency, and idempotency semantics before production startup is enabled.
