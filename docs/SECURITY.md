# Security Baseline

The gateway is deny-by-default: a tenant without an explicit policy cannot route, and a request must satisfy authenticated identity binding, product/model entitlements, billing authorization, provider policy, and regional constraints before provider execution.

## Required controls

- Authenticate clients at the deployment edge and pass a short-lived signed assertion. The gateway verifies its signature, expiry, issuer, and audience; the verified tenant claim must exactly match the request tenant.
- Authorize product/model entitlements and billing credits before routing. Never trust caller-supplied provider or credential values.
- Store provider credentials in the deployment secret manager, rotate them, and keep them out of configuration, logs, and source control.
- Encrypt transport with TLS 1.2 or newer and use private provider endpoints where available.
- Treat prompts and outputs as tenant-confidential data. Do not log them. Apply tenant-scoped retention, deletion, and data-residency controls.
- Emit immutable audit events for policy decisions and provider invocations. The allowlisted event schema excludes prompts, outputs, bearer tokens, and credentials.
- Apply request and prompt size limits, edge rate limits, provider timeouts, bounded retries, ordered fallback, and per-provider circuit breakers.
- Pin CI actions to reviewed revisions before environments require SLSA provenance; enable dependency, secret, and code scanning.
- Separate production tenants and credentials from development fixtures. The included tenant configuration is illustrative only.

## Credential and identity operations

- `UWO_AUTH_SIGNING_KEY` must be generated and rotated in a managed secret store and contain at least 32 unpredictable characters. Shared HMAC identity is an internal foundation; migrate to asymmetric workload identity before untrusted external access.
- Provider configuration contains only `env://` references. Deployment automation maps those names to secret-manager values; credentials must never be supplied in HTTP requests.
- Request IDs are constrained to 128 safe characters and returned in `X-Request-ID`. They are correlation metadata, not authorization credentials.
- Provider endpoints must use HTTPS. The committed `.example.invalid` endpoints cannot reach production services.

## Threat boundaries

The execution endpoint can invoke providers only after all configured gates pass, but this repository does not deploy the service or contain real credentials. Durable audit storage, a live billing service, rate limiting, content-safety enforcement, asymmetric workload identity, provider-private networking, and production incident controls remain required before serving external traffic. These gaps are tracked in the roadmap.

Report vulnerabilities privately to the repository owners. Do not open public issues containing secrets or exploitable details.
