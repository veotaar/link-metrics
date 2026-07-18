"""Discovery and validation for local Contender manifests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator


class ContenderDiscoveryError(Exception):
    """A local Contender manifest could not be discovered safely."""


def discover_contenders(root: Path) -> list[dict[str, Any]]:
    """Return validated Contender manifests in stable identity order."""
    schema_path = Path(__file__).resolve().parents[2] / "schemas" / "contender.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    contenders: list[dict[str, Any]] = []

    for manifest_path in sorted((root / "backends").glob("*/contender.yaml")):
        relative_path = manifest_path.relative_to(root)
        try:
            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise ContenderDiscoveryError(f"{relative_path}: invalid YAML: {error}") from error

        errors = sorted(validator.iter_errors(manifest), key=lambda error: list(error.path))
        if errors:
            error = errors[0]
            field = ".".join(str(part) for part in error.absolute_path) or "manifest"
            raise ContenderDiscoveryError(f"{relative_path}: {field}: {error.message}")

        contenders.append({**manifest, "manifest": relative_path.as_posix()})

    by_id: dict[str, list[str]] = {}
    for contender in contenders:
        by_id.setdefault(contender["id"], []).append(contender["manifest"])
    for contender_id, manifests in by_id.items():
        if len(manifests) > 1:
            raise ContenderDiscoveryError(
                f"duplicate Contender id '{contender_id}' in {', '.join(manifests)}"
            )

    for contender in contenders:
        directory_id = Path(contender["manifest"]).parent.name
        if contender["id"] != directory_id:
            raise ContenderDiscoveryError(
                f"{contender['manifest']}: id: expected '{directory_id}' to match its directory, "
                f"got '{contender['id']}'"
            )

    return sorted(contenders, key=lambda contender: contender["id"])
