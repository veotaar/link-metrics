"""Linting for the versioned Link Metrics API Contract."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from openapi_spec_validator import validate
from openapi_spec_validator.validation.exceptions import OpenAPIValidationError


HTTP_METHODS = ("get", "post", "put", "patch", "delete", "options", "head", "trace")
SEMANTIC_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class ContractLintError(Exception):
    """The API Contract is invalid or violates a contract-tooling invariant."""


def lint_contract(document_path: Path) -> dict[str, Any]:
    """Validate the OpenAPI document and return its stable identity summary."""
    try:
        document = yaml.safe_load(document_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ContractLintError(f"{document_path}: cannot read OpenAPI document: {error}") from error

    try:
        validate(document)
    except OpenAPIValidationError as error:
        raise ContractLintError(f"{document_path}: invalid OpenAPI document: {error}") from error

    if document.get("openapi") != "3.1.2":
        raise ContractLintError(
            f"{document_path}: openapi must be exactly '3.1.2', got {document.get('openapi')!r}"
        )

    contract_version = document.get("info", {}).get("version")
    if not isinstance(contract_version, str) or not SEMANTIC_VERSION.fullmatch(contract_version):
        raise ContractLintError(
            f"{document_path}: info.version must be a stable semantic version, got {contract_version!r}"
        )

    operation_ids: list[str] = []
    for path, path_item in document.get("paths", {}).items():
        for method in HTTP_METHODS:
            if method in path_item:
                operation_id = path_item[method].get("operationId")
                if not isinstance(operation_id, str) or not operation_id:
                    raise ContractLintError(
                        f"{document_path}: {method.upper()} {path} must declare an operationId"
                    )
                operation_ids.append(operation_id)

    duplicate_ids = sorted(
        operation_id for operation_id in set(operation_ids) if operation_ids.count(operation_id) > 1
    )
    if duplicate_ids:
        raise ContractLintError(f"{document_path}: duplicate operationIds: {duplicate_ids}")

    return {
        "openapi": document["openapi"],
        "operations": sorted(operation_ids),
        "version": contract_version,
    }
