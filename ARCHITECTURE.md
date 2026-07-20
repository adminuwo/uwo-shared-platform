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
- `infrastructure/config`: reviewed provider catalog and tenant policies.
- `architecture/manifest.json`: machine-readable component ownership and capability mapping.
- `tooling/validate_architecture.py`: manifest-to-contract and filesystem consistency validation.

The AI Gateway filters providers by explicit tenant allowlist, blocklist, requested model, and allowed region. Unique provider priorities produce a stable primary and ordered fallback list. Unknown tenants or requests with no eligible provider fail closed.

Secure execution adds an authenticated edge assertion, verified tenant binding, product/model entitlements, billing authorization, and a provider-neutral input content-safety gate before provider selection. Provider adapters resolve credentials through a secret-manager contract only at execution time. Azure OpenAI and OpenAI Responses API scaffolds are wrapped by bounded timeouts, retry, fallback, and per-provider circuit breakers. Raw Responses JSON is parsed through a shared fail-closed contract, and output passes a second content-safety gate before release. Structured audit events contain allowlisted identifiers and decisions, never prompts, outputs, bearer tokens, or credential values.

## API Boundaries

- `GET /healthz` returns process health.
- `POST /v1/route` authenticates and authorizes a tenant, product, model, and region and returns a deterministic routing plan.
- `POST /v1/execute` applies authentication, entitlement, billing, and routing policy before invoking a provider adapter.

Every response carries `X-Request-ID`. Callers may provide a constrained request ID or allow the gateway to generate one.

## Architecture Governance

Every implemented component must be listed in `architecture/manifest.json`, reference only canonical capabilities, and point to an existing repository path. CI runs the architecture validator and automated tests on every pull request.
