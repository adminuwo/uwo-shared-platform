# Security Baseline

This bootstrap is deny-by-default: a tenant without an explicit policy cannot route, and a request must satisfy tenant provider, model, and regional constraints before a provider is returned.

## Required controls

- Authenticate clients at the deployment edge and pass a verified tenant identity; request-body tenant IDs are a bootstrap interface, not a trusted production identity source.
- Authorize product and model entitlements before inference. Never use a caller-supplied provider credential.
- Store provider credentials in the deployment secret manager, rotate them, and keep them out of configuration, logs, and source control.
- Encrypt transport with TLS 1.2 or newer and use private provider endpoints where available.
- Treat prompts and outputs as tenant-confidential data. Do not log them. Apply tenant-scoped retention, deletion, and data-residency controls.
- Emit immutable audit events for policy decisions and provider invocations, with request IDs but without prompt content or secrets.
- Apply request size limits, rate limits, timeouts, bounded retries, and circuit breakers at the deployment edge and provider adapters.
- Pin CI actions to reviewed revisions before environments require SLSA provenance; enable dependency, secret, and code scanning.
- Separate production tenants and credentials from development fixtures. The included tenant configuration is illustrative only.

## Threat boundaries

The route endpoint returns a policy decision; it does not yet invoke providers. Authentication, durable audit storage, provider adapters, content safety, billing enforcement, and cryptographic workload identity are required before serving external production traffic. These gaps are tracked in the roadmap.

Report vulnerabilities privately to the repository owners. Do not open public issues containing secrets or exploitable details.
