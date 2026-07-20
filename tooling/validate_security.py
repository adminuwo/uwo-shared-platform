"""Fail CI when gateway configuration contains unsafe provider credential material."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
