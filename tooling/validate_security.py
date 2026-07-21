"""Fail CI when gateway configuration contains unsafe provider credential material."""

from __future__ import annotations

import json
import sys
from dataclasses import fields
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def validate(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    try:
        config = json.loads((root / "infrastructure/config/ai-gateway.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"cannot load AI gateway configuration: {exc}"]
    forbidden_fields = {"api_key", "access_token", "password", "credential", "secret_value"}
    for index, provider in enumerate(config.get("providers", [])):
        present = forbidden_fields & provider.keys()
        if present:
            errors.append(f"provider {index} contains credential fields: {sorted(present)}")
        if not str(provider.get("secret_ref", "")).startswith("env://"):
            errors.append(f"provider {index} must use an env:// secret reference")
        if not str(provider.get("endpoint", "")).startswith("https://"):
            errors.append(f"provider {index} endpoint must use HTTPS")
        models = provider.get("models")
        model_map = provider.get("model_map")
        if not isinstance(models, list) or not models or not all(isinstance(alias, str) and alias.strip() for alias in models) or len(set(models)) != len(models) or not isinstance(model_map, dict):
            errors.append(f"provider {index} must define models and model_map")
        elif set(models) != set(model_map):
            errors.append(f"provider {index} model_map must exactly match declared models")
        elif not all(isinstance(alias, str) and alias.strip() and isinstance(value, str) and value.strip() for alias, value in model_map.items()):
            errors.append(f"provider {index} model_map values must be non-empty strings")
        if provider.get("adapter") == "azure-openai":
            if "api_version" in provider:
                errors.append(f"Azure provider {index} must use the v1 contract without api_version")
    for tenant_id, policy in config.get("tenant_policies", {}).items():
        content_safety = policy.get("content_safety") if isinstance(policy, dict) else None
        if not isinstance(content_safety, dict) or content_safety.get("enabled") is not True:
            errors.append(f"tenant {tenant_id!r} must enable the content-safety boundary")
    try:
        gitignore = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        errors.append(f"cannot verify .gitignore: {exc}")
    else:
        if ".env" not in gitignore or ".env.*" not in gitignore:
            errors.append(".gitignore must exclude .env credential files")
    control_plane = root / "services/platform_control_plane"
    if control_plane.is_dir():
        app_source = (control_plane / "app.py").read_text(encoding="utf-8")
        if "from .in_memory" in app_source or "InMemoryTenantRepository(" in app_source:
            errors.append("control-plane HTTP startup must not instantiate test-only in-memory repositories")
        if root == ROOT:
            from services.platform_control_plane.audit import ControlPlaneAuditEvent

            audit_fields = {item.name for item in fields(ControlPlaneAuditEvent)}
            forbidden_audit_fields = {"authorization", "bearer_token", "prompt", "output", "request_body", "secret", "secret_value"}
            present = audit_fields & forbidden_audit_fields
            if present:
                errors.append(f"control-plane audit schema contains sensitive fields: {sorted(present)}")
    billing = root / "services/platform_billing"
    if billing.is_dir():
        app_source = (billing / "app.py").read_text(encoding="utf-8")
        if "from .in_memory" in app_source or "InMemoryBilling" in app_source:
            errors.append("billing HTTP startup must not instantiate test-only in-memory repositories")
        contracts_source = (root / "packages/contracts/billing.py").read_text(encoding="utf-8")
        forbidden_usage_fields = {"prompt", "model_output", "bearer_token", "api_key", "provider_secret", "request_body"}
        if any(f"    {name}:" in contracts_source for name in forbidden_usage_fields):
            errors.append("billing contracts contain a forbidden sensitive-data field")
        if root == ROOT:
            from services.platform_billing.audit import BillingAuditEvent

            audit_fields = {item.name for item in fields(BillingAuditEvent)}
            forbidden_audit_fields = {"authorization", "bearer_token", "prompt", "output", "request_body", "secret", "secret_value"}
            present = audit_fields & forbidden_audit_fields
            if present:
                errors.append(f"billing audit schema contains sensitive fields: {sorted(present)}")
    return errors


def main() -> int:
    errors = validate()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("security configuration is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
