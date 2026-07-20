# Security Baseline

The gateway is deny-by-default: a tenant without an explicit policy cannot route, and a request must satisfy authenticated identity binding, product/model entitlements, billing authorization, provider policy, and regional constraints before provider execution.

## Required controls

- Authenticate clients at the deployment edge and pass a short-lived signed assertion. The gateway verifies its signature, expiry, issuer, and audience; the verified tenant claim must exactly match the request tenant.
- Authorize product/model entitlements and billing credits before routing. Never trust caller-supplied provider or credential values.
- Store provider credentials in the deployment secret manager, rotate them, and keep them out of configuration, logs, and source control.
- Encrypt transport with TLS 1.2 or newer and use private provider endpoints where available.
- Treat prompts and outputs as tenant-confidential data. Do not log them. Apply tenant-scoped retention, deletion, and data-residency controls.
- Require provider-neutral content-safety authorization before provider execution and again before provider output is returned. Missing or denied safety decisions fail closed.
- Emit immutable audit events for policy decisions and provider invocations. The allowlisted event schema excludes prompts, outputs, bearer tokens, and credentials.
- Apply request and prompt size limits, edge rate limits, provider timeouts, bounded retries, ordered fallback, and per-provider circuit breakers.
- Pin CI actions to reviewed revisions before environments require SLSA provenance; enable dependency, secret, and code scanning.
- Separate production tenants and credentials from development fixtures. The included tenant configuration is illustrative only.

## Credential and identity operations

- `UWO_AUTH_SIGNING_KEY` must be generated and rotated in a managed secret store and contain at least 32 unpredictable characters. Shared HMAC identity is an internal foundation; migrate to asymmetric workload identity before untrusted external access.
- Provider configuration contains only `env://` references. Deployment automation maps those names to secret-manager values; credentials must never be supplied in HTTP requests.
- Provider-specific model IDs and Azure deployments come only from reviewed `model_map` configuration. Caller-supplied UWO aliases cannot select arbitrary provider models, and missing mappings fail before secret access or transport.
- Request IDs are constrained to 128 safe characters and returned in `X-Request-ID`. They are correlation metadata, not authorization credentials.
- Provider endpoints must use HTTPS. The committed `.example.invalid` endpoints cannot reach production services.
- The configuration-backed content-safety authorizer is internal/test-only. Setting `UWO_ENVIRONMENT=production` blocks startup until a real production integration is supplied.

## Threat boundaries

The execution endpoint can invoke providers only after all configured gates pass, but this repository does not deploy the service or contain real credentials. Durable audit storage, a live billing service, rate limiting, an external content-safety service, asymmetric workload identity, provider-private networking, and production incident controls remain required before serving external traffic. These gaps are tracked in the roadmap.

## Identity and tenancy control plane

- Authenticate every `/v1` administration request through a deployment-supplied trusted authenticator. The public health check exposes no tenant data.
- Grant cross-tenant authority only to explicitly configured platform administrators. Tenant administrators must have an active membership, a known role, the required permission, and a verified tenant matching the target path.
- Treat tenant identifiers in paths or payloads only as requested resources, never as authorization evidence. Unknown tenants, subjects, roles, permissions, entitlements, and policy versions fail closed.
- A suspended tenant cannot perform administration or receive effective entitlements. Only platform administrators may reactivate it.
- Require optimistic aggregate versions for mutations and reject stale writes. Require bounded idempotency keys for tenant creation and entitlement grants; key reuse with conflicting input is rejected.
- Keep mutable state behind repository interfaces. In-memory repositories and the static subject directory are test-only; the executable entry point fails startup until trusted authentication and durable repositories are injected.
- Control-plane audit events use an allowlisted schema containing correlation and resource identifiers only. They exclude bearer tokens, authentication material, secrets, and request bodies.
- Limit request bodies to 65,536 bytes, constrain request IDs and idempotency keys, bound list pages to 100 records, return `no-store` responses, and preserve tenant isolation at the service layer as well as the HTTP boundary.

Phase 3A does not add production persistence, credentials, or deployment infrastructure. Before production use, integrate workload identity, a durable identity directory and repositories, encrypted regional storage, immutable audit ingestion, rate limiting, policy promotion approval, and recovery controls.

Report vulnerabilities privately to the repository owners. Do not open public issues containing secrets or exploitable details.
