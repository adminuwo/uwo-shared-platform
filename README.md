# UWO Shared Platform

Canonical shared platform for UWO products: identity, tenancy, entitlements, billing, AI gateway, storage, notifications, analytics, audit, security, connectors, knowledge, and shared UI.

## Secure AI Gateway

The platform provides shared product/capability contracts and a tenant-aware AI Gateway. Routing is deterministic and fails closed when a tenant, product, model, provider, or region is not explicitly allowed. Provider execution additionally requires a verified bearer identity, tenant binding, entitlement approval, and billing authorization.

Run the service with Python 3.9 or newer after supplying secrets through the deployment environment or secret-manager injector:

```bash
python -m services.ai_gateway.app
```

Check service health at `GET /healthz`. Request a policy decision with `POST /v1/route`:

```json
{
  "tenant_id": "tenant-demo-in",
  "product": "aisa",
  "model": "uwo-general-v1",
  "region": "in"
}
```

Authenticated requests use `Authorization: Bearer <signed-edge-assertion>` and may supply `X-Request-ID`; otherwise the gateway generates a request ID. `POST /v1/execute` accepts the routing fields plus `prompt`, performs authorization, billing, and pre-execution content-safety checks, then invokes a configured provider adapter with bounded timeout, retry, fallback, and circuit-breaker controls. Provider output must pass a second content-safety gate before it can be returned.

The committed provider endpoints are non-routable examples and all credentials are `env://` secret references. Never commit API keys. A real runtime must set `UWO_AUTH_SIGNING_KEY` and provider secrets in its managed secret environment. The included config content-safety authorizer is for internal/test use; `UWO_ENVIRONMENT=production` fails startup until an external production authorizer is integrated.

Public requests always use stable UWO aliases such as `uwo-general-v1` and `uwo-legal-v1`. Each provider must declare an exact `model_map` from every supported UWO alias to its provider-specific model ID or Azure deployment. Adapters fail closed before any provider call when a mapping is unavailable.

## Identity and Tenancy Control Plane

Phase 3A adds canonical, schema-versioned contracts for tenants, verified subjects, memberships, roles, permissions, product/model entitlements, and policy documents under `packages/contracts`. Identifiers are stable, mutable aggregates carry optimistic versions, and timestamps are explicit UTC ISO-8601 values.

The separate `services/platform_control_plane` service exposes authenticated internal administration boundaries under `/v1` for tenant lifecycle, membership and role administration, deterministic effective permissions, entitlements, and policy-version reads. Platform administrators may operate across tenants; tenant administrators are bound to their verified tenant and cannot administer another tenant. Membership-derived authority is revalidated against the subject directory on every use, so deprovisioning takes effect immediately. Suspended tenants fail closed. Every response uses a consistent JSON envelope and correlation ID, and mutating operations require optimistic versions.

Tenant provisioning is one UnitOfWork covering the tenant, empty entitlement aggregate, initial policy document, and idempotency ledger record. Test repositories roll every write back on failure. Idempotency keys are scoped by operation, tenant, and actor; the ledger stores the immutable original result rather than a live resource reference. Exact retries therefore replay the original version even after later updates or revocation, conflicting key reuse fails closed, and replay does not duplicate mutation audit events.

Policy documents accept JSON values only, recursively freeze objects and arrays, and expose deterministic canonical serialization and fingerprints. `Tenant.status` is the sole operational-status authority; policy bodies contain configuration metadata and never duplicate tenant lifecycle state. The HTTP layer owns one denial audit event per denied request and maps unexpected repository failures to a redacted `internal_error` response.

The committed in-memory repositories and static subject directory are test integrations only. The HTTP server accepts injected authentication and repository dependencies; its executable entry point intentionally refuses to start until deployment supplies trusted authentication and durable repositories. No production database or infrastructure is introduced in Phase 3A.

## Billing, Credits, and Usage Ledger

Phase 3B adds schema-versioned billing accounts, integer credit balances, reservations, redacted usage, immutable rate cards, and append-only ledger entries under `packages/contracts`. One credit is 1,000,000 microunits. Token rates are integer microunits per 1,000 tokens; input and output components independently round up with `ceil(tokens × rate / 1000)` before adding an integer fixed request charge. Floating-point credit arithmetic is not permitted.

The separate `services/platform_billing` service exposes authenticated internal `/v1` account, balance, credit, reservation, usage, ledger, rate-card, and health boundaries. Financial mutations use optimistic versions, a scoped immutable-result idempotency ledger, and one UnitOfWork spanning every aggregate and ledger write. The committed thread-safe in-memory repositories support rollback and concurrency tests only; executable startup refuses to select them as production persistence.

The ledger is append-only and derives non-negative available and reserved balances. A reservation is created before provider execution, captures redacted token usage only after output safety succeeds, releases unused credit after partial capture, and releases fully on provider or safety failure. Retries replay original reservation/capture/release results without duplicate charges, usage events, ledger entries, or successful audit events. Rate-card versions are immutable and all committed prices are illustrative test data, not live commercial pricing.

Billing authorization composes with Phase 3A: platform administrators manage accounts and credits; tenant members need `billing.read`; and only explicitly configured, directory-revalidated internal executors may reserve, capture, or release. Unknown and suspended tenants, cross-tenant access, insufficient balances, and closed accounts fail closed. Usage and audit contracts exclude prompts, outputs, bearer tokens, API keys, secrets, request bodies, and payment credentials.

## Validation

```bash
python tooling/validate_architecture.py
python tooling/validate_security.py
python -m unittest discover -s tests -v
```

See [ARCHITECTURE.md](ARCHITECTURE.md), [security baseline](docs/SECURITY.md), [control-plane decision](docs/adr/0003-identity-tenancy-control-plane.md), [billing decision](docs/adr/0004-billing-credits-usage-ledger.md), and [roadmap](docs/ROADMAP.md).
