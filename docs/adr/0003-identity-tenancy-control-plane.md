# ADR 0003: Identity and tenancy control-plane separation

- Status: Accepted
- Date: 2026-07-20

## Context

UWO products need one canonical definition of tenants, verified subjects, memberships, roles, permissions, entitlements, and policy versions. These resources are security-sensitive administrative state, not AI-provider routing concerns. Placing their lifecycle inside the AI Gateway would couple administrative availability and authorization to provider execution, expand the gateway's privilege, and make tenant isolation harder to audit.

Phase 3A also needs safe mutation behavior before selecting a production database. Concurrent administrators must not silently overwrite state, retries must not duplicate tenant or entitlement grants, and tests must exercise the complete boundary without credentials or infrastructure.

## Decision

Keep versioned domain contracts in `packages/contracts` and implement administration in a separate `services/platform_control_plane` service. Expose authenticated internal `/v1` boundaries and inject authentication, subject verification, audit, and repository dependencies. The executable service will not instantiate test repositories or start as a production service until deployment supplies trusted authentication and durable repository implementations.

Authorize cross-tenant operations only for explicitly configured platform administrators. A tenant administrator must have an active membership in the verified tenant, known role assignments, and the exact required permission. Permissions are the sorted union of permissions from known assigned roles. Unknown resources or role data, inactive memberships, and suspended tenants fail closed. Tenant identifiers supplied by callers select a requested resource but never confer authority.

Separate persistence behind tenant, membership, role, entitlement, and policy-version repository protocols. Commit thread-safe in-memory implementations for tests only. Maintain aggregate versions and require expected versions for every mutation. Reject stale updates, duplicate memberships, duplicate or missing role assignments, and duplicate entitlements. Tenant creation and entitlement grants require idempotency keys bound to canonical request fingerprints; exact retries return the original result and conflicting reuse fails.

Emit structured audit events from an allowlisted schema for successful changes and denied administration attempts. Correlation, actor, tenant, target, resource, outcome, and reason identifiers are permitted; bearer tokens, secrets, and request bodies are not. Expose a provider-neutral effective-entitlement lookup so the AI Gateway can integrate later without coupling the control plane to provider adapters.

## Consequences

Tenant administration has an independently testable security and persistence boundary, deterministic permission evaluation, explicit concurrency behavior, and auditable mutation semantics. The AI Gateway remains focused on secure model routing and execution. Callers must handle conflict responses, retain entity versions, and use stable idempotency keys.

This phase does not supply a production identity directory, database, deployment, policy-promotion workflow, or durable audit sink. Those integrations must preserve these contracts, tenant isolation, optimistic concurrency, and idempotency semantics before production startup is enabled.
