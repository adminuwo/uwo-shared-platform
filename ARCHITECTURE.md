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
- `infrastructure/config`: reviewed provider catalog and tenant policies.
- `architecture/manifest.json`: machine-readable component ownership and capability mapping.
- `tooling/validate_architecture.py`: manifest-to-contract and filesystem consistency validation.

The AI Gateway filters providers by explicit tenant allowlist, blocklist, requested model, and allowed region. Unique provider priorities produce a stable primary and ordered fallback list. Unknown tenants or requests with no eligible provider fail closed.

Secure execution adds an authenticated edge assertion, verified tenant binding, product/model entitlements, billing authorization, and a provider-neutral input content-safety gate before provider selection. Public routing retains stable UWO model aliases; every provider declares an exact alias-to-provider `model_map`, resolved before secret access or transport. Provider adapters resolve credentials through a secret-manager contract only at execution time. Azure OpenAI and OpenAI Responses API scaffolds are wrapped by bounded timeouts, retry, fallback, and per-provider circuit breakers. Raw Responses JSON is parsed through a shared fail-closed contract, and output passes a second content-safety gate before release. Structured audit events contain allowlisted identifiers and decisions, never prompts, outputs, bearer tokens, or credential values.

Phase 3A keeps tenant administration out of the AI Gateway. Canonical versioned domain contracts live in `packages/contracts`; the platform control plane coordinates authorization and injected tenant, membership, role, entitlement, and policy repositories. Its committed in-memory implementations are test-only. Deployment must supply durable repositories and trusted authentication before the service can start.

Platform-level administration is explicitly allowlisted. Tenant administrators derive deterministic permissions from active memberships and known roles, are bound to their verified tenant, and cannot cross tenant boundaries. Unknown tenants, subjects, roles, permissions, or entitlements fail closed. Suspended tenants have no effective entitlement access and only a platform administrator can reactivate them. Aggregate versions reject stale writes, while idempotency records make tenant creation and entitlement grants safely repeatable.

## API Boundaries

- `GET /healthz` returns process health.
- `POST /v1/route` authenticates and authorizes a tenant, product, model, and region and returns a deterministic routing plan.
- `POST /v1/execute` applies authentication, entitlement, billing, and routing policy before invoking a provider adapter.
- `GET /healthz` on the platform control plane returns process health without exposing tenant data.
- `/v1/tenants` and tenant-scoped `/v1` subresources provide authenticated tenant lifecycle, membership, role, permission, entitlement, and policy-version administration.

Every service response carries `X-Request-ID`. Callers may provide a constrained request ID or allow the service to generate one. Control-plane list responses use bounded `limit` and continuation-cursor fields.

## Architecture Governance

Every implemented component must be listed in `architecture/manifest.json`, reference only canonical capabilities, and point to an existing repository path. CI runs the architecture validator and automated tests on every pull request.
