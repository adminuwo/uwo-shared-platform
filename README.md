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

Authenticated requests use `Authorization: Bearer <signed-edge-assertion>` and may supply `X-Request-ID`; otherwise the gateway generates a request ID. `POST /v1/execute` accepts the routing fields plus `prompt`, performs authorization, billing, and pre-execution content-safety checks, then invokes a configured provider adapter with bounded timeout, retry, fallback, and circuit-breaker controls. Provider output must pass a second content-safety gate before it can be returned. A successful provider response must contain explicit, complete, non-negative token usage whose total equals input plus output; missing, null, partial, malformed, or inconsistent usage fails closed before output is returned and cannot create a fabricated zero-usage charge.

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

The ledger is append-only and derives non-negative available and reserved balances. Every entry type has exact canonical available/reserved deltas, account and tenant binding, a unique ID, and a sequential version; repositories revalidate these invariants on append and derivation. A reservation is created before provider execution, captures redacted token usage only after output safety succeeds, atomically releases unused credit, and releases fully on provider or safety failure. Gateway receipts carry only reservation identity. Gateway-specific methods load the current reservation and balance inside one UnitOfWork, so adapter recreation and unrelated ledger activity cannot create stale lifecycle versions. Retries replay original reservation/capture/release results without duplicate charges, usage events, ledger entries, or successful audit events.

Rate-card versions are immutable and selected with `active_at(usage_occurred_at)`: the newest version whose UTC effective time is not later than the usage time wins, with rate-card ID and version as deterministic tie-breakers across families. Multiple versions in one family at the same effective time are rejected. Future cards never activate early, historical usage keeps its bound version, and all committed prices are illustrative test data, not live commercial pricing.

Billing authorization composes with Phase 3A: platform administrators manage accounts and credits; tenant members need `billing.read`; and only explicitly configured, directory-revalidated internal executors may reserve, capture, or release. Known control-plane denials and missing resources map to stable `403` and `404` responses; unexpected directory or repository faults propagate to one redacted `500 internal_error` audit/response rather than being mislabeled as authorization denial. Unknown and suspended tenants, cross-tenant access, insufficient balances, and closed accounts fail closed. Usage and audit contracts exclude prompts, outputs, bearer tokens, API keys, secrets, request bodies, and payment credentials.

Provider and output-safety failures use an idempotent release operation. If release itself fails, the original failure remains chained for internal recovery, one `billing-compensation-failed` audit event is emitted, callers receive the stable non-sensitive `billing_compensation_failed` code, and the reservation remains discoverable. Provider execution uses an injected outcome UnitOfWork containing a durable outcome repository and transactional outbox. A provider result is stored before capture; after capture, the captured marker and mandatory `provider.execution.succeeded` event commit atomically. If that transaction fails after billing committed, retry reuses the stored result, captures idempotently, and repairs the event without another provider call or charge. Captured replay verifies or recovers its event before returning. Provider-failure and compensation-failure outcomes/events use the same restart-safe boundary; optional telemetry remains failure-isolated. The committed outcome UnitOfWork and outbox are in-memory test implementations; production requires durable workflow/outbox storage and reconciliation workers.

## Platform Data and Event Services

Phase 3C adds four separate internal services. `platform_storage` owns metadata-only object lifecycle and immutable version records behind a provider-neutral `BlobStore`; callers never select storage keys and the repository never stores raw bytes. Malware scans append immutable scan-result history and current state is derived deterministically without rewriting an object version. Scanner source IDs are idempotency keys bound across tenant, object version, status, and verified executor: exact replay returns the original result, conflicting reuse returns `409`, and an infected result plus its single outbox event commit atomically. Upload expiry and retention are compared as parsed UTC instants. Restricted/regulated retention must remain future-dated; only a platform administrator can shorten it with an explicit reason and audit event. SHA-2 integrity, size, regional policy, classification, retention, legal hold, deletion, scan state, and bounded download TTL all fail closed before opaque download authorization is issued.

`platform_notifications` owns immutable template versions, preferences, delivery lifecycle, deterministic retry, cancellation, suppression, and dead letters. Notification creation and its immutable outbox record share one transaction. Dispatch uses claim/lease, commits the claim, calls the provider outside the UnitOfWork with a stable delivery key, then finalizes acceptance or retry in a second transaction. Generic outbox claim never terminalizes the notification independently. If a worker dies after its final claim, the next worker atomically records the final attempt, creates one dead letter, and terminalizes both notification and outbox without calling the provider again. Reconciliation is idempotent and forbids an enqueued notification with a terminal outbox or a terminal notification with active outbox work. Provider adapters return redacted acceptance metadata only; this repository includes deterministic fakes, not vendor connections. Webhook destinations are opaque references checked by an injected allowlist.

`platform_analytics` accepts only canonical operational dimensions—tenant, product, region, allowlisted event type/outcome, bounded buckets, error code, and approved pseudonymous identifiers. The service owns `recorded_at`, rejects events outside lateness/future-skew bounds, and aggregates parsed UTC windows as half-open `[start, end)` intervals. Minimum-count suppression applies to tenant and platform-admin cross-tenant exports; cross-tenant results contain aggregate points only. Prompts, outputs, bodies, file content, credentials, payment data, direct identity, and arbitrary metadata have no contract field.

`platform_audit` allocates monotonic tenant-scoped sequences and links canonical immutable events with SHA-256 previous/current hashes. Checkpoints verify the complete contiguous chain from genesis. Export verification checks tenant binding, contiguous sequences, exact first/last boundaries, every previous/current hash link, and the manifest’s canonical event digest. Source `PlatformEvent` ingestion is idempotent by source event ID and conflicting content fails closed. Durable `actor_subject` is always the verified service executor; `pseudonymous_subject_id` remains a separate allowlisted attribute and can never spoof the actor. The allowlisted scalar schema is redacted by construction.

All Phase 3C mutations keep business state and an outbox record in one rollback boundary where downstream delivery applies; control-plane status changes and AI Gateway mandatory outcome events use the same atomic pattern. Outbox records are immutable and optimistic-versioned. Dispatchers claim leases, exclude concurrent processing, obey due times, recover expired leases, acknowledge only after idempotent downstream acceptance, and ask the owning business service to reconcile exhausted work. `PlatformEvent` validates stable IDs, UTC/schema version, deeply immutable allowlisted scalar attributes, and an event ID fingerprint covering type, tenant, request, and safe attributes. Delivery infrastructure failure never changes an already committed business result. Committed repositories, blob storage, providers, subject directories, outcome stores, and publishers are thread-safe test integrations only. Every executable entry point refuses to choose them for production.

## Validation

```bash
python tooling/validate_architecture.py
python tooling/validate_security.py
python -m unittest discover -s tests -v
```

See [ARCHITECTURE.md](ARCHITECTURE.md), [security baseline](docs/SECURITY.md), [control-plane decision](docs/adr/0003-identity-tenancy-control-plane.md), [billing decision](docs/adr/0004-billing-credits-usage-ledger.md), [data-services decision](docs/adr/0005-platform-data-and-event-services.md), and [roadmap](docs/ROADMAP.md).
