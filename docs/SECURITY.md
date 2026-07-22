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
- Require optimistic aggregate versions for mutations and reject stale writes. Scope bounded idempotency keys by operation, tenant, and actor. Ledger records retain an immutable original response snapshot, so replay cannot follow a changed or deleted live resource; conflicting fingerprints are rejected.
- Provision a tenant transactionally: tenant write, entitlement initialization, initial policy, and idempotency persistence either all commit or all roll back. Emit one redacted rollback event without repository details.
- Revalidate both membership-derived administrators and target members through the subject directory before every membership update, role mutation, or effective-permission decision. Authentication alone does not preserve authority after deprovisioning.
- Accept JSON-compatible policy configuration only. Deep-freeze nested objects and arrays, reject non-string keys, unsupported objects, NaN, and infinity, and use canonical deterministic serialization. `Tenant.status` is authoritative and must never be duplicated in policy configuration.
- Keep mutable state behind repository interfaces. In-memory repositories and the static subject directory are test-only; the executable entry point fails startup until trusted authentication and durable repositories are injected.
- Control-plane audit events use an allowlisted schema containing correlation and resource identifiers only. They exclude bearer tokens, authentication material, secrets, and request bodies. The HTTP boundary owns denial auditing so each denied request produces exactly one event; the service owns successful mutation and rollback events.
- Return stable `403`, `404`, and `409` contracts for authorization, unknown resources, stale versions, and idempotency conflicts. Unexpected persistence failures return a generic `500 internal_error`; exception text and repository internals never cross the boundary.
- Limit request bodies to 65,536 bytes, constrain request IDs and idempotency keys, bound list pages to 100 records, return `no-store` responses, and preserve tenant isolation at the service layer as well as the HTTP boundary.

Phase 3A does not add production persistence, credentials, or deployment infrastructure. Before production use, integrate workload identity, a durable identity directory and repositories, encrypted regional storage, immutable audit ingestion, rate limiting, policy promotion approval, and recovery controls.

## Billing, credit, and usage controls

- Keep billing lifecycle in `services/platform_billing`; AI Gateway and tenant-control handlers consume application contracts and never reach billing repositories directly.
- Represent credit, rates, charges, token counts, and usage quantities as integers. One credit is 1,000,000 microunits. Reject floating-point financial input, negative usage, unknown price dimensions, stale versions, and any mutation that would make available or reserved balances negative.
- Treat the ledger as immutable and append-only. Enforce exact entry-type deltas, account/tenant binding, unique IDs, and sequential versions in both contracts and repositories; there are no update or delete operations. Captured plus released amounts cannot exceed the original reservation.
- Reserve before provider transport. Gateway receipts contain identity only; reserve/capture/release load current lifecycle and balance state inside one UnitOfWork. Capture and unused release finalize atomically, while provider, missing-usage, or output-safety failure uses an idempotent full release. Expired capture requires an explicit, audited platform-administrator override.
- Require explicit provider usage. Missing, null, partial, negative, or inconsistent token counts fail closed before output and create no usage event or zero-charge record; explicit internally consistent zero counts remain valid input to the configured fixed request price.
- Bind usage to the immutable rate-card ID and version selected at `occurred_at`. Future cards cannot activate early, exact effective time is inclusive, and ambiguous same-family activation is rejected. Example repository prices are not commercial prices. Production rate cards require approval, signing/promotion, effective-time controls, and reconciliation.
- Scope idempotency records by operation, tenant, actor, and key. Store immutable original operation results. Exact retries must not add ledger entries, usage events, or successful audit events; conflicting reuse returns `409`.
- Execute each grant, reservation, capture, release, refund, and its associated ledger/usage/idempotency records in one transaction. Roll back every participant and emit one redacted transaction-failure event on repository failure.
- Grant account and credit mutation only to explicitly configured, directory-revalidated platform administrators. Tenant members require `billing.read` and remain tenant-bound. Reserve/capture/release authority belongs only to explicit trusted internal executor subjects that are revalidated against the subject directory. Translate only known control-plane denial/not-found errors; unexpected authorization dependency faults must produce one redacted `500`, not a false denial.
- Unknown tenants, suspended tenants, insufficient balances, cross-tenant access, suspended billing accounts, and closed-account debit attempts fail closed. Caller-supplied tenant IDs are resource selectors, never authority.
- Billing usage and audit schemas may retain tenant, product, shared model alias, provider/model identifiers where allowed, region, request/provider correlation IDs, integer token counts, rate-card version, and calculated charge. They must never retain prompts, model output, bearer tokens, API keys, provider secrets, request bodies, authentication material, or payment credentials.
- The HTTP boundary enforces 65,536-byte bodies, bounded request and idempotency identifiers, explicit status codes, pagination limits, `no-store`, correlation headers, one denial event per denied request, and generic `500 internal_error` responses without exception or repository detail.
- Compensation must not hide the initiating failure. A release failure emits one redacted `billing-compensation-failed` event and stable response while retaining the reservation for recovery. A capture failure after provider success must be retryable without a second provider call; production requires durable workflow state and reconciliation rather than relying on process memory.
- Emit transaction-rollback audit events only when persistence fails after a mutation has begun. Pre-validation, authorization, not-found, stale-version, and idempotency conflicts are not transaction rollbacks.
- Never start production with the committed in-memory repositories, in-process billing adapter, example price data, or JSON stdout audit sink. Production requires regional durable transactions, encrypted storage, workload identity, immutable audit ingestion, reconciliation, expiration processing, monitoring, backup/recovery, and an approved rate-card promotion process.

Phase 3B adds no credentials, payment gateway, production database, or deployment infrastructure.

## Platform data and event controls

- Keep storage metadata, notification lifecycle, analytics, and audit in their separate service boundaries. No HTTP handler accesses global mutable dictionaries, raw blob bytes, provider credentials, or another service's repository.
- Authorize through Phase 3A permissions. Tenant context selects a resource but never grants authority. Deprovisioned subjects fail immediately; suspended tenants cannot create objects or notifications; tenant reads remain isolated; cross-tenant analytics or audit export is platform-admin only; internal ingestion/delivery requires an explicit executor allowlist.
- Reject storage objects outside allowed size and region policy. Generate provider keys internally. Require SHA-256 or stronger integrity metadata, append immutable versions, enforce retention and active legal holds, and deny download for deletion, checksum failure, incomplete scanning, or malware. Restricted/regulated content requires explicit region, retention, and access controls.
- Return only opaque expiring download authorization metadata. Never place raw cloud credentials, signed provider URLs, storage keys, file contents, or authorization tokens in audit/event logs.
- Keep notification templates immutable by version. Check channel preference and opt-out before enqueue, validate webhook destination references through a tenant allowlist, redact provider responses, cap deterministic retries, and preserve terminal cancellation/suppression/dead-letter states.
- Commit a business mutation and its outbox record atomically. Use deterministic event IDs, immutable records, optimistic transition versions, acceptance-before-acknowledgement, retry-safe downstream deduplication, and visible poison-event dead letters. Never silently drop events.
- Analytics accepts fixed operational dimensions only. It excludes prompts, model outputs, message bodies, file content, credentials, payment data, direct personal data, and arbitrary user metadata. Use approved pseudonymous IDs, half-open UTC windows, immutable snapshots, and minimum-count suppression before export.
- Durable audit accepts explicit allowlisted scalar attributes only and always marks events redacted. Exclude authorization headers, tokens, secrets, bodies, prompts, outputs, notification content, payment data, stack traces, and arbitrary exception text. Hash canonical tenant-scoped events into monotonic chains; verify chains before checkpoints and exports; report integrity failure with stable codes only.
- HTTP boundaries cap bodies at 65,536 bytes, constrain request/correlation identifiers, return `no-store`, paginate reads, and expose stable redacted status contracts. The HTTP layer owns exactly one denial event; unexpected exceptions become one generic `internal_error` event and response.
- Committed in-memory repositories, fake blob and notification providers, static allowlists, and collecting publishers are test-only. Executable entry points fail until deployments inject durable regional repositories, managed storage, authenticated provider adapters, and transactional dispatchers.

Phase 3C adds no credentials, cloud storage integration, messaging vendor, broker, production database, or deployment infrastructure. Production requires encryption and regional isolation, workload identity, managed key rotation, malware tooling, approved retention/legal-hold enforcement, durable outbox dispatch and reconciliation, backup/recovery, integrity monitoring, and Phase 3D observability and runbooks.

Report vulnerabilities privately to the repository owners. Do not open public issues containing secrets or exploitable details.
