"""Validate manifest structure and consistency with code contracts and the filesystem."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packages.contracts import CAPABILITIES, PRODUCTS  # noqa: E402


def validate(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    path = root / "architecture/manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"cannot load architecture manifest: {exc}"]

    required = {"version", "products", "capabilities", "components"}
    missing = required - manifest.keys()
    if missing:
        errors.append(f"missing required keys: {sorted(missing)}")
        return errors
    if manifest["version"] != 1:
        errors.append("manifest version must be 1")
    if manifest["products"] != [item.value for item in PRODUCTS]:
        errors.append("manifest products must exactly match packages.contracts.PRODUCTS")
    if manifest["capabilities"] != [item.value for item in CAPABILITIES]:
        errors.append("manifest capabilities must exactly match packages.contracts.CAPABILITIES")

    ids: set[str] = set()
    capability_ids = set(manifest["capabilities"])
    for component in manifest["components"]:
        component_id = component.get("id")
        if not component_id or component_id in ids:
            errors.append(f"component id must be non-empty and unique: {component_id!r}")
        ids.add(component_id)
        component_path = root / component.get("path", "")
        if not component_path.is_dir() or root not in component_path.resolve().parents:
            errors.append(f"component {component_id!r} has invalid path")
        unknown = set(component.get("capabilities", [])) - capability_ids
        if unknown:
            errors.append(f"component {component_id!r} references unknown capabilities: {sorted(unknown)}")
    return errors


def main() -> int:
    errors = validate()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("architecture manifest is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
