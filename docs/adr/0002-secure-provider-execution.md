# ADR 0002: Secure provider execution boundary

- Status: Accepted
- Date: 2026-07-20

## Context

Phase 1 returned deterministic routing plans but intentionally did not authenticate callers or invoke providers. Controlled internal execution requires a verifiable tenant identity, authorization gates, credential isolation, bounded provider failure behavior, and audit records that do not leak tenant content.

## Decision

Require short-lived, issuer- and audience-bound HMAC-signed bearer assertions at every non-health HTTP boundary and bind the verified tenant claim to the request tenant. Check product/model entitlements, billing authorization, and provider-neutral input content safety before deterministic routing. Preserve stable UWO model aliases at the public boundary and require every provider to map each declared alias exactly once to a provider-specific model ID or Azure deployment. Resolve this mapping before secret access or transport. Invoke providers only through a provider-neutral adapter contract that obtains credentials from a secret-manager abstraction. Parse raw Responses API output through one fail-closed contract and require output content-safety authorization before release.

Wrap adapters with explicit per-attempt timeouts, bounded retry, ordered provider fallback, and per-provider circuit breakers. Correlate HTTP responses, provider requests, and structured audit events with a constrained request ID. Audit events use an allowlist schema that cannot contain prompts, model outputs, bearer tokens, or secret values.

## Consequences

Authorization and credential handling are testable without live providers, failures remain bounded, and provider-specific code is isolated. Runtime startup fails if the authentication secret is absent, and production mode also fails while only the internal/test content-safety authorizer exists. HMAC assertions, environment-backed secret resolution, configuration-backed billing and safety, and stdout JSON audit are internal bootstrap implementations; production environments must replace or integrate them with workload identity, managed secrets, billing and content-safety services, and durable audit ingestion.
