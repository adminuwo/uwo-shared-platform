# UWO Shared Platform

Canonical shared platform for UWO products: identity, tenancy, entitlements, billing, AI gateway, storage, notifications, analytics, audit, security, connectors, knowledge, and shared UI.

## Bootstrap

The first executable foundation provides shared product/capability contracts and a tenant-aware AI model router. Routing is deterministic and fails closed when a tenant, provider, model, or region is not explicitly allowed.

Run the service with Python 3.9 or newer:

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

The response contains the selected provider and an ordered fallback list. The bootstrap does not invoke model providers.

## Validation

```bash
python tooling/validate_architecture.py
python -m unittest discover -s tests -v
```

See [ARCHITECTURE.md](ARCHITECTURE.md), [security baseline](docs/SECURITY.md), [routing decision](docs/adr/0001-tenant-aware-ai-routing.md), and [roadmap](docs/ROADMAP.md).
