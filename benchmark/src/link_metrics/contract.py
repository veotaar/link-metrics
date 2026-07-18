"""Linting for the versioned Link Metrics API Contract."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from openapi_spec_validator import validate_spec
from openapi_spec_validator.validation.exceptions import OpenAPIValidationError


EXPECTED_OPERATIONS = {
    ("/health", "get"): ("health", {"204", "503"}),
    ("/api/auth/register", "post"): (
        "registerUser",
        {"201", "400", "409", "413", "415", "503"},
    ),
    ("/api/auth/login", "post"): (
        "loginUser",
        {"200", "400", "401", "413", "415", "503"},
    ),
    ("/api/links", "post"): (
        "createShortLink",
        {"201", "400", "401", "413", "415", "503"},
    ),
    ("/{shortCode}", "get"): ("resolveShortLink", {"302", "404", "503"}),
    ("/api/links/{shortCode}/stats", "get"): (
        "getShortLinkStats",
        {"200", "401", "404", "503"},
    ),
}
SEMANTIC_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class ContractLintError(Exception):
    """The API Contract is invalid or has drifted from the minimal surface."""


def lint_contract(document_path: Path) -> dict[str, Any]:
    """Validate the OpenAPI document and return its stable identity summary."""
    try:
        document = yaml.safe_load(document_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ContractLintError(f"{document_path}: cannot read OpenAPI document: {error}") from error

    try:
        validate_spec(document)
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

    actual_operations: dict[tuple[str, str], dict[str, Any]] = {}
    for path, path_item in document.get("paths", {}).items():
        for method in ("get", "post", "put", "patch", "delete", "options", "head", "trace"):
            if method in path_item:
                actual_operations[(path, method)] = path_item[method]

    if set(actual_operations) != set(EXPECTED_OPERATIONS):
        missing = sorted(set(EXPECTED_OPERATIONS) - set(actual_operations))
        unexpected = sorted(set(actual_operations) - set(EXPECTED_OPERATIONS))
        raise ContractLintError(
            f"{document_path}: operation surface drift; missing={missing}, unexpected={unexpected}"
        )

    operation_ids: list[str] = []
    for operation_key, (expected_id, expected_statuses) in EXPECTED_OPERATIONS.items():
        operation = actual_operations[operation_key]
        actual_id = operation.get("operationId")
        if actual_id != expected_id:
            raise ContractLintError(
                f"{document_path}: {operation_key}: operationId must be '{expected_id}', got {actual_id!r}"
            )
        actual_statuses = set(operation.get("responses", {}))
        if actual_statuses != expected_statuses:
            raise ContractLintError(
                f"{document_path}: {operation_key}: responses must be {sorted(expected_statuses)}, "
                f"got {sorted(actual_statuses)}"
            )
        operation_ids.append(expected_id)

    return {
        "openapi": document["openapi"],
        "operations": sorted(operation_ids),
        "version": contract_version,
    }
