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

## Validation

```bash
python tooling/validate_architecture.py
python tooling/validate_security.py
python -m unittest discover -s tests -v
```

See [ARCHITECTURE.md](ARCHITECTURE.md), [security baseline](docs/SECURITY.md), [routing decision](docs/adr/0001-tenant-aware-ai-routing.md), and [roadmap](docs/ROADMAP.md).
