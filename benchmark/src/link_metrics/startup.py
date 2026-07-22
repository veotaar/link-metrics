"""Cold Contender startup measurement and immutable evidence."""

from __future__ import annotations

import json
import statistics
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from link_metrics.dataset import DatasetError, describe_dataset
from link_metrics.dataset_runtime import inspect_template, reset_from_template
from link_metrics.evidence import write_immutable_json
from link_metrics.environment import (
    LOCAL_RESOURCE_PROFILE,
    assess_host_preflight,
    capture_host_observation,
    summarize_host_execution,
)
from link_metrics.progress import ExecutionBudget
from link_metrics.runtime import (
    POSTGRES_IMAGE,
    ContenderRuntimeError,
    _container_document,
    _container_exists,
    _contender_url,
    _docker,
    _find_manifest,
    _migration_version,
    _resource_names,
    _wait_for_postgres,
    image_digest,
    pause_database_container,
    remove_contender_container,
)
from link_metrics.results import percentile
from link_metrics.trial import (
    BENCHMARK_PASSWORD,
    TrialError,
    _api_contract_version,
    _apply_container_profile,
    _container_limits,
    _contender_container_arguments,
    _environment_fingerprint,
    _git_commit,
    _protocol_version,
)


COLD_START_REPETITIONS = 20


class StartupError(Exception):
    """The control plane could not measure cold startup."""


def _cold_start_repetition_directory(output: Path) -> Path:
    return output.parent / f"{output.stem}.repetitions"


def _load_cold_start_repetition(
    path: Path,
    *,
    contender_id: str,
    template_checksum: str,
    repetition: int,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        matches = (
            isinstance(document, dict)
            and document.get("kind") == "cold-start-repetition"
            and document.get("contender") == contender_id
            and document.get("templateChecksum") == template_checksum
            and document.get("provenance") == provenance
            and int(document.get("sample", {}).get("repetition")) == repetition
            and len(document.get("hostObservations", [])) == 2
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        matches = False
        document = None
    if not matches or not isinstance(document, dict):
        raise StartupError(
            f"existing cold-start repetition does not match its schedule: {path}"
        )
    return document


def resume_cold_start_repetitions(
    output: Path,
    *,
    contender_id: str,
    template_checksum: str,
    measure: Callable[[int], dict[str, Any]],
    budget: ExecutionBudget,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run or reuse the 20 immutable repetitions behind one cold-start series."""
    documents: list[dict[str, Any]] = []
    if provenance is None:
        provenance = {}
    directory = _cold_start_repetition_directory(output)
    for repetition in range(1, COLD_START_REPETITIONS + 1):
        path = directory / f"repetition-{repetition:02d}.json"
        if path.exists():
            document = _load_cold_start_repetition(
                path,
                contender_id=contender_id,
                template_checksum=template_checksum,
                repetition=repetition,
                provenance=provenance,
            )
        else:
            if not budget.can_start():
                return {
                    "schemaVersion": 1,
                    "kind": "cold-start-progress",
                    "status": "paused",
                    "contender": contender_id,
                    "completedRepetitions": len(documents),
                    "newRepetitionsThisSession": budget.completed_units,
                    "output": str(output),
                }
            evidence = measure(repetition)
            document = write_immutable_json(
                path,
                {
                    "schemaVersion": 1,
                    "kind": "cold-start-repetition",
                    "contender": contender_id,
                    "templateChecksum": template_checksum,
                    "provenance": provenance,
                    **evidence,
                },
            )
            budget.record_completed()
        documents.append(document)
    return {
        "schemaVersion": 1,
        "kind": "cold-start-progress",
        "status": "complete",
        "contender": contender_id,
        "completedRepetitions": len(documents),
        "newRepetitionsThisSession": budget.completed_units,
        "output": str(output),
        "repetitions": documents,
    }


def docker_started_at_ns(value: str) -> int:
    """Parse Docker's RFC3339Nano process-start timestamp without losing precision."""
    if not value.endswith("Z") or "." not in value:
        raise StartupError(f"invalid Docker StartedAt timestamp: {value}")
    whole, fractional = value[:-1].split(".", 1)
    if not fractional.isdigit() or len(fractional) > 9:
        raise StartupError(f"invalid Docker StartedAt timestamp: {value}")
    instant = datetime.fromisoformat(whole).replace(tzinfo=timezone.utc)
    return int(instant.timestamp()) * 1_000_000_000 + int(fractional.ljust(9, "0"))


def summarize_cold_startup(samples: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Summarize the mandatory 20 cold-start repetitions as median and p95."""
    if len(samples) != COLD_START_REPETITIONS:
        raise StartupError("cold startup requires exactly 20 repetitions")
    try:
        repetitions = [int(sample["repetition"]) for sample in samples]
        readiness = [float(sample["readinessMs"]) for sample in samples]
        first_request = [float(sample["firstRequestMs"]) for sample in samples]
        _statuses = [
            (int(sample["readinessStatus"]), int(sample["firstRequestStatus"]))
            for sample in samples
        ]
    except (KeyError, TypeError, ValueError) as error:
        raise StartupError("cold-start repetition evidence is incomplete") from error
    if sorted(repetitions) != list(range(1, COLD_START_REPETITIONS + 1)):
        raise StartupError("cold startup repetitions must be numbered 1 through 20")
    return {
        "repetitions": COLD_START_REPETITIONS,
        "readinessMs": {
            "median": statistics.median(readiness),
            "p95": percentile(readiness, 0.95),
        },
        "firstRequestMs": {
            "median": statistics.median(first_request),
            "p95": percentile(first_request, 0.95),
        },
    }


def validate_cold_start_bundle(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate official cold-start evidence and return its derived summary."""
    if payload.get("kind") != "cold-start-series" or not payload.get("official"):
        raise StartupError("cold-start bundles must be official cold-start-series evidence")
    try:
        provenance_complete = all(
            (
                payload["gitCommit"],
                payload["protocolVersion"],
                payload["apiContractVersion"],
                payload["datasetVersion"],
                payload["migrationVersion"],
                payload["contender"]["id"],
                payload["contender"]["imageDigest"],
                payload["contender"]["manifest"],
                payload["environment"]["fingerprint"],
                payload["environment"]["preflight"],
                payload["environment"]["execution"],
                payload["lifecycle"],
                payload["validity"],
            )
        )
    except (KeyError, TypeError):
        provenance_complete = False
    if not provenance_complete:
        raise StartupError("official cold-start bundle provenance is incomplete")
    try:
        expected = summarize_cold_startup(payload["repetitions"])
    except KeyError as error:
        raise StartupError("cold-start bundle has no repetitions") from error
    if payload.get("summary") != expected:
        raise StartupError("cold-start summary does not match its repetitions")
    return expected


def write_cold_start_bundle(output: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist one immutable cold-start Result Series."""
    validate_cold_start_bundle(payload)
    bundle = {"schemaVersion": 1, **payload}
    try:
        return write_immutable_json(output, bundle)
    except FileExistsError as error:
        raise StartupError(f"cold-start bundle already exists: {output}") from error


def _create_stopped_contender(root: Path, contender_id: str) -> tuple[str, str]:
    names = _resource_names(root, contender_id)
    contender = _find_manifest(root, contender_id)
    if _container_exists(names.contender):
        _docker("container", "rm", "--force", names.contender)
    profile = LOCAL_RESOURCE_PROFILE["contender"]
    _docker(
        "create",
        *_contender_container_arguments(
            root,
            contender_id,
            profile=profile,
        ),
    )
    actual = _container_limits(names.contender)
    expected = {
        "cpusetCpus": profile["cpusetCpus"],
        "memoryBytes": profile["memoryBytes"],
        "memorySwapBytes": profile["memorySwapBytes"],
        "nanoCpus": 0,
    }
    if actual != expected:
        raise StartupError(
            "cold Contender does not match the official resource profile: "
            f"expected {expected}, got {actual}"
        )
    document = _container_document(names.contender)
    health_url = _contender_url(document, contender["port"])
    return names.contender, health_url


def _wait_for_first_readiness(health_url: str, deadline: float) -> tuple[int, int]:
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=1) as response:
                response.read()
                if response.status == 204:
                    return 204, time.time_ns()
        except (OSError, urllib.error.HTTPError):
            pass
        time.sleep(0.01)
    raise StartupError("Contender did not become ready within 60 seconds")


def _first_registration(base_url: str, repetition: int) -> tuple[int, float]:
    body = json.dumps(
        {
            "email": f"cold-start-{repetition:02d}@trial.invalid",
            "password": BENCHMARK_PASSWORD,
        },
        separators=(",", ":"),
    ).encode()
    request = urllib.request.Request(
        f"{base_url}/api/auth/register",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    started = time.perf_counter_ns()
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        error.read()
        status = error.code
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    return status, elapsed_ms


def run_cold_startup(
    root: Path,
    contender_id: str,
    *,
    output: Path,
    budget: ExecutionBudget | None = None,
) -> dict[str, Any]:
    """Measure 20 process starts against an already-ready Benchmark Dataset."""
    root = root.resolve()
    if budget is None:
        budget = ExecutionBudget()
    contender = _find_manifest(root, contender_id)
    names = _resource_names(root, contender_id)
    if not _container_exists(names.database):
        raise StartupError(
            "PostgreSQL and the Benchmark Dataset must already be ready; run "
            f"`link-metrics contenders start {contender_id}` and "
            f"`link-metrics dataset build {contender_id}` first"
        )
    if not bool(_container_document(names.database)["State"]["Running"]):
        _docker("start", names.database)
        _wait_for_postgres(names.database)
    try:
        template = inspect_template(root, contender_id)
    except DatasetError as error:
        raise StartupError(
            f"{error}; run `link-metrics dataset build {contender_id}` first"
        ) from error

    remove_contender_container(root, contender_id)
    try:
        _apply_container_profile(names.database, LOCAL_RESOURCE_PROFILE["postgres"])
        preflight = assess_host_preflight(
            LOCAL_RESOURCE_PROFILE,
            capture_host_observation(LOCAL_RESOURCE_PROFILE),
        )
        if not preflight["valid"]:
            raise StartupError(
                "host preflight failed: " + ", ".join(preflight["reasons"])
            )

        _create_stopped_contender(root, contender_id)
        try:
            fingerprint = _environment_fingerprint(
                root,
                contender,
                k6_limits={
                    "cpusetCpus": LOCAL_RESOURCE_PROFILE["k6"]["cpusetCpus"],
                    "memoryBytes": LOCAL_RESOURCE_PROFILE["k6"]["memoryBytes"],
                    "memorySwapBytes": LOCAL_RESOURCE_PROFILE["k6"]["memorySwapBytes"],
                    "nanoCpus": 2_000_000_000,
                },
                host_observation=preflight["observation"],
                resource_profile=LOCAL_RESOURCE_PROFILE,
            )
        finally:
            remove_contender_container(root, contender_id)
        contender_image_digest = image_digest(names.image)
        repetition_provenance = {
            "gitCommit": _git_commit(root),
            "protocolVersion": _protocol_version(root),
            "datasetVersion": describe_dataset(root)["version"],
            "environmentFingerprint": fingerprint,
            "contenderImageDigest": contender_image_digest,
            "contenderManifest": contender,
        }
        if output.exists():
            try:
                completed = json.loads(output.read_text(encoding="utf-8"))
                validate_cold_start_bundle(completed)
                matches = (
                    completed.get("gitCommit") == repetition_provenance["gitCommit"]
                    and completed.get("protocolVersion")
                    == repetition_provenance["protocolVersion"]
                    and completed.get("datasetVersion")
                    == repetition_provenance["datasetVersion"]
                    and completed.get("environment", {}).get("fingerprint") == fingerprint
                    and completed.get("contender")
                    == {
                        "id": contender_id,
                        "imageDigest": contender_image_digest,
                        "manifest": contender,
                    }
                    and all(
                        sample.get("templateChecksum") == template["templateChecksum"]
                        for sample in completed.get("repetitions", [])
                    )
                )
            except (OSError, json.JSONDecodeError, TypeError, StartupError):
                matches = False
                completed = None
            if not matches or not isinstance(completed, dict):
                raise StartupError(
                    f"cold-start bundle already exists with incompatible evidence: {output}"
                )
            return completed

        def measure(repetition: int) -> dict[str, Any]:
            reset = reset_from_template(root, contender_id, template["templateChecksum"])
            container, health_url = _create_stopped_contender(root, contender_id)
            observations = [capture_host_observation(LOCAL_RESOURCE_PROFILE)]
            try:
                _docker("start", container)
                process_started = docker_started_at_ns(
                    str(_container_document(container)["State"]["StartedAt"])
                )
                readiness_status, readiness_observed_at = _wait_for_first_readiness(
                    health_url, time.monotonic() + 60
                )
                readiness_ms = (readiness_observed_at - process_started) / 1_000_000
                if readiness_ms < 0:
                    raise StartupError(
                        "Docker process-start timestamp is ahead of the host clock"
                    )
                first_status, first_request_ms = _first_registration(
                    health_url.removesuffix("/health"), repetition
                )
                observations.append(capture_host_observation(LOCAL_RESOURCE_PROFILE))
                return {
                    "sample": {
                        "repetition": repetition,
                        "readinessMs": readiness_ms,
                        "firstRequestMs": first_request_ms,
                        "readinessStatus": readiness_status,
                        "firstRequestStatus": first_status,
                        "templateChecksum": reset["templateChecksum"],
                    },
                    "hostObservations": observations,
                }
            finally:
                remove_contender_container(root, contender_id)

        progress = resume_cold_start_repetitions(
            output,
            contender_id=contender_id,
            template_checksum=str(template["templateChecksum"]),
            measure=measure,
            budget=budget,
            provenance=repetition_provenance,
        )
        if progress["status"] == "paused":
            return progress
        repetition_evidence = progress["repetitions"]
        samples = [item["sample"] for item in repetition_evidence]
        host_observations = [
            observation
            for item in repetition_evidence
            for observation in item["hostObservations"]
        ]

        summary = summarize_cold_startup(samples)
        host_execution = summarize_host_execution(
            LOCAL_RESOURCE_PROFILE, host_observations
        )
        validity_reasons = list(host_execution["reasons"])
        if any(sample["readinessStatus"] != 204 for sample in samples):
            validity_reasons.append("readiness_failed")
        if any(sample["firstRequestStatus"] != 201 for sample in samples):
            validity_reasons.append("first_real_request_failed")
        validity = {
            "valid": not validity_reasons,
            "reasons": validity_reasons,
        }
        return write_cold_start_bundle(
            output,
            {
                "kind": "cold-start-series",
                "official": True,
                "gitCommit": _git_commit(root),
                "protocolVersion": _protocol_version(root),
                "apiContractVersion": _api_contract_version(root),
                "datasetVersion": describe_dataset(root)["version"],
                "migrationVersion": _migration_version(root),
                "contender": {
                    "id": contender_id,
                    "imageDigest": contender_image_digest,
                    "manifest": contender,
                },
                "environment": {
                    "fingerprint": fingerprint,
                    "postgresImage": POSTGRES_IMAGE,
                    "preflight": preflight,
                    "execution": host_execution,
                },
                "lifecycle": {
                    "databaseReadyBeforeProcessStart": True,
                    "excluded": [
                        "imageBuild",
                        "imagePull",
                        "migrations",
                        "seeding",
                        "loadGeneration",
                    ],
                    "processStartBoundary": "Docker State.StartedAt",
                    "readiness": "first /health 204",
                    "firstRequest": "POST /api/auth/register",
                },
                "repetitions": samples,
                "summary": summary,
                "validity": validity,
            },
        )
    except (ContenderRuntimeError, DatasetError, TrialError) as error:
        raise StartupError(str(error)) from error
    finally:
        remove_contender_container(root, contender_id)
        pause_database_container(root, contender_id)
