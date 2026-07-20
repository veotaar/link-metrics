"""Registration Trial orchestration and immutable result bundles."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Any

from link_metrics.dataset import DatasetError, describe_dataset, sample_workload
from link_metrics.dataset_runtime import inspect_template, reset_from_template
from link_metrics.runtime import (
    CONTENDER_PASSWORD,
    CONTENDER_ROLE,
    DATABASE_NAME,
    POSTGRES_IMAGE,
    ContenderRuntimeError,
    _container_document,
    _container_exists,
    _contender_url,
    _docker,
    _find_manifest,
    _migration_version,
    _published_port,
    _resource_names,
    _wait_for_readiness,
    inspect_contender,
    start_contender,
    stop_contender,
)


K6_IMAGE = (
    "grafana/k6:1.4.2@"
    "sha256:3656673de3f30424e8ebcfa46acd9558d83b6a43612d0f668ffeac953950c6c7"
)
REGISTRATION_SCRIPT = Path("benchmark/protocol/k6/registration.js")
BENCHMARK_PASSWORD = "link-metrics-benchmark-only"

SMOKE_WARM_SECONDS = 2
SMOKE_MEASURE_SECONDS = 5
SMOKE_OFFERED_RATE = 2
TRIAL_WARM_SECONDS = 30
TRIAL_MEASURE_SECONDS = 60

CONTENDER_CONSTRAINTS = {
    "maxPoolConnections": 20,
    "connectionTimeoutMillis": 2000,
    "statementTimeoutMillis": 2000,
    "successPathAccessLogging": False,
}

TRANSPORT = {
    "protocol": "HTTP/1.1",
    "tls": False,
    "keepAlive": True,
    "maxConnections": 256,
    "httpTimeoutSeconds": 5,
}


class TrialError(Exception):
    """The control plane could not run or record a Trial."""


def registration_email(*, repetition: int, iteration: int) -> str:
    """Return a deterministic unique valid registration identity."""
    if repetition < 1:
        raise TrialError("repetition must be at least 1")
    if iteration < 0:
        raise TrialError("iteration must be nonnegative")
    return f"reg-{repetition:02d}-{iteration:012d}@trial.invalid"


def evaluate_validity(metrics: dict[str, Any]) -> dict[str, Any]:
    """Decide whether a Trial remains valid under load-generator health rules."""
    reasons: list[str] = []
    if int(metrics.get("droppedIterations", 0)) > 0:
        reasons.append("k6_could_not_schedule_offered_rate")
    if bool(metrics.get("k6CpuSaturated", False)):
        reasons.append("k6_cpu_saturated")
    return {"valid": not reasons, "reasons": reasons}


def write_result_bundle(output: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist an immutable machine-readable Trial bundle."""
    official = bool(payload.get("official"))
    mode = payload.get("mode")
    if mode == "smoke" and official:
        raise TrialError("smoke Trial bundles must be nonofficial")
    if mode not in {"smoke", "trial"}:
        raise TrialError("mode must be smoke or trial")

    bundle = {"schemaVersion": 1, **payload}
    serialized = json.dumps(bundle, indent=2, sort_keys=True) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(serialized, encoding="utf-8")
    return json.loads(serialized)


def build_validation_flags(root: Path, *, repetition: int, count: int) -> list[bool]:
    """Return the Dataset validation stream for the first `count` iterations."""
    return list(sample_workload(root, repetition, count)["samples"]["validation"])


def parse_k6_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Normalize the k6 handleSummary payload into Trial result fields."""
    latency = summary.get("latency") or {}
    return {
        "droppedIterations": int(summary.get("droppedIterations") or 0),
        "httpReqs": int(summary.get("httpReqs") or 0),
        "checksPassed": int(summary.get("checksPassed") or 0),
        "checksFailed": int(summary.get("checksFailed") or 0),
        "latency": {
            "avgMs": float(latency.get("avgMs") or 0),
            "medMs": float(latency.get("medMs") or 0),
            "p90Ms": float(latency.get("p90Ms") or 0),
            "p95Ms": float(latency.get("p95Ms") or 0),
            "p99Ms": float(latency.get("p99Ms") or 0),
            "maxMs": float(latency.get("maxMs") or 0),
        },
        "errors": {
            "transportFailures": int(summary.get("transportFailures") or 0),
            "unexpectedResponses": int(summary.get("unexpectedResponses") or 0),
            "checksFailed": int(summary.get("checksFailed") or 0),
        },
    }


def _git_commit(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise TrialError(f"cannot resolve Git commit: {result.stderr.strip()}")
    return result.stdout.strip()


def _protocol_version(root: Path) -> str:
    return (root / "benchmark" / "protocol" / "VERSION").read_text(encoding="utf-8").strip()


def _api_contract_version(root: Path) -> str:
    for line in (root / "contracts" / "http" / "openapi.yaml").read_text(encoding="utf-8").splitlines():
        if line.startswith("  version:"):
            return line.split(":", 1)[1].strip()
    raise TrialError("API Contract version is missing from openapi.yaml")


def _image_digest(image: str) -> str:
    result = _docker(
        "image",
        "inspect",
        "--format",
        "{{json .RepoDigests}}",
        image,
        check=False,
    )
    if result.returncode != 0:
        # Fall back to image ID when RepoDigests are empty for locally tagged builds.
        identity = _docker(
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            image,
            check=False,
        )
        if identity.returncode != 0:
            raise TrialError(f"cannot inspect image digest for {image}")
        return identity.stdout.strip()
    digests = json.loads(result.stdout)
    if digests:
        return digests[0].split("@", 1)[-1] if "@" in digests[0] else digests[0]
    identity = _docker("image", "inspect", "--format", "{{.Id}}", image)
    return identity.stdout.strip()


def _environment_fingerprint(contender: dict[str, Any]) -> dict[str, Any]:
    memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    return {
        "resourceProfile": contender["resourceProfile"],
        "hostname": platform.node(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpus": os.cpu_count(),
        "memoryBytes": memory,
        "k6Image": K6_IMAGE,
        "postgresImage": POSTGRES_IMAGE,
        "k6Cpus": 2,
        "k6MemoryGiB": 4,
        "contenderCpus": 4,
        "contenderMemoryGiB": 8,
        "postgresCpus": 2,
        "postgresMemoryGiB": 8,
    }


def _contender_base_url(root: Path, contender_id: str) -> str:
    contender = _find_manifest(root, contender_id)
    names = _resource_names(root, contender_id)
    document = _container_document(names.contender)
    port = _published_port(document, contender["port"])
    return f"http://127.0.0.1:{port}"


def _k6_container_name(root: Path, contender_id: str) -> str:
    return f"{_resource_names(root, contender_id).contender}-k6"


def _remove_k6(root: Path, contender_id: str) -> None:
    name = _k6_container_name(root, contender_id)
    if _container_exists(name):
        _docker("rm", "--force", name, check=False)


def _k6_cpu_percent(container: str) -> float:
    raw = _docker(
        "stats",
        "--no-stream",
        "--format",
        "{{.CPUPerc}}",
        container,
        check=False,
    )
    if raw.returncode != 0:
        return 0.0
    text = raw.stdout.strip().rstrip("%")
    try:
        return float(text)
    except ValueError:
        return 0.0


def run_k6(
    root: Path,
    *,
    contender_id: str,
    base_url: str,
    offered_rate: int,
    duration_seconds: int,
    repetition: int,
    validation_flags: list[bool],
    work_dir: Path,
) -> tuple[dict[str, Any], bool]:
    """Run the pinned registration script against a Contender URL."""
    root = root.resolve()
    script = (root / REGISTRATION_SCRIPT).resolve()
    if not script.is_file():
        raise TrialError(f"missing k6 script at {script}")

    work_dir.mkdir(parents=True, exist_ok=True)
    work_dir.chmod(0o777)
    summary_path = work_dir / "summary.json"
    if summary_path.exists():
        summary_path.unlink()
    flags_path = work_dir / "validation-flags.json"
    flags_path.write_text(json.dumps(validation_flags, separators=(",", ":")), encoding="utf-8")
    flags_path.chmod(0o644)

    names = _resource_names(root, contender_id)
    network = names.network
    container = _k6_container_name(root, contender_id)
    _remove_k6(root, contender_id)

    contender = _find_manifest(root, contender_id)
    private_url = f"http://{names.contender}:{contender['port']}"
    target_url = private_url if base_url.startswith("http://127.0.0.1") else base_url

    pre_allocated = max(20, min(256, offered_rate * 4))
    max_vus = max(pre_allocated, min(256, offered_rate * 8))
    assigned_cpus = 2.0

    try:
        _docker("pull", K6_IMAGE, check=False)
        _docker(
            "run",
            "--detach",
            "--name",
            container,
            "--network",
            network,
            "--cpus",
            "2",
            "--memory",
            "4g",
            "--memory-swap",
            "4g",
            "--label",
            "dev.link-metrics.control-plane=true",
            "--volume",
            f"{script.parent}:/scripts:ro",
            "--volume",
            f"{work_dir.resolve()}:/work",
            "--workdir",
            "/scripts",
            "--env",
            f"BASE_URL={target_url}",
            "--env",
            f"OFFERED_RATE={offered_rate}",
            "--env",
            f"DURATION={duration_seconds}s",
            "--env",
            f"REPETITION={repetition}",
            "--env",
            f"PASSWORD={BENCHMARK_PASSWORD}",
            "--env",
            f"PRE_ALLOCATED_VUS={pre_allocated}",
            "--env",
            f"MAX_VUS={max_vus}",
            "--env",
            "VALIDATION_FLAGS_PATH=/work/validation-flags.json",
            "--env",
            "SUMMARY_PATH=/work/summary.json",
            K6_IMAGE,
            "run",
            "registration.js",
        )
        peak_cpu = 0.0
        deadline = time.monotonic() + duration_seconds + 120
        while time.monotonic() < deadline:
            document = _container_document(container)
            peak_cpu = max(peak_cpu, _k6_cpu_percent(container))
            if document["State"]["Status"] != "running":
                break
            time.sleep(0.5)
        else:
            _docker("kill", container, check=False)
            raise TrialError("k6 did not finish within the Trial watchdog window")

        logs = _docker("logs", container, check=False)
        saturated = peak_cpu >= (assigned_cpus * 100.0) * 0.95
        if not summary_path.is_file():
            raise TrialError(
                "k6 did not emit summary.json: "
                + (logs.stderr.strip() or logs.stdout.strip() or "no container logs")
            )
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        return summary, saturated
    finally:
        _remove_k6(root, contender_id)


def _start_contender_container(root: Path, contender_id: str) -> None:
    """Create a fresh Contender container against an already-running PostgreSQL."""
    names = _resource_names(root, contender_id)
    contender = _find_manifest(root, contender_id)
    if _container_exists(names.contender):
        _docker("rm", "--force", names.contender)
    migration_version = _migration_version(root)
    database_url = (
        f"postgresql://{CONTENDER_ROLE}:{CONTENDER_PASSWORD}@postgres:5432/{DATABASE_NAME}"
    )
    _docker(
        "run",
        "--detach",
        "--name",
        names.contender,
        "--network",
        names.network,
        "--label",
        "dev.link-metrics.control-plane=true",
        "--env",
        f"DATABASE_URL={database_url}",
        "--env",
        f"EXPECTED_MIGRATION_VERSION={migration_version}",
        "--env",
        f"PORT={contender['port']}",
        "--publish",
        f"127.0.0.1:0:{contender['port']}",
        names.image,
    )
    document = _container_document(names.contender)
    _wait_for_readiness(_contender_url(document, contender["port"]))


def _stop_contender_container(root: Path, contender_id: str) -> None:
    names = _resource_names(root, contender_id)
    if _container_exists(names.contender):
        _docker("stop", "--time", "5", names.contender, check=False)
        _docker("container", "rm", "--force", names.contender)


def _ensure_fresh_contender(root: Path, contender_id: str) -> bool:
    """Ensure PostgreSQL is up and start a brand-new Contender container.

    Returns True when this call created the PostgreSQL container so the caller
    may tear down the full stack afterward. The Contender container is always
    fresh for the Trial; PostgreSQL is preserved so the Dataset template survives.
    """
    names = _resource_names(root, contender_id)
    if not _container_exists(names.database):
        start_contender(root, contender_id)
        return True
    _start_contender_container(root, contender_id)
    return False


def run_registration_trial(
    root: Path,
    contender_id: str,
    *,
    output: Path,
    mode: str,
    repetition: int = 1,
    offered_rate: int | None = None,
) -> dict[str, Any]:
    """Execute one registration Trial lifecycle and write its raw bundle."""
    root = root.resolve()
    if mode not in {"smoke", "trial"}:
        raise TrialError("mode must be smoke or trial")
    if mode == "smoke":
        warm_seconds = SMOKE_WARM_SECONDS
        measure_seconds = SMOKE_MEASURE_SECONDS
        rate = SMOKE_OFFERED_RATE
        official = False
    else:
        warm_seconds = TRIAL_WARM_SECONDS
        measure_seconds = TRIAL_MEASURE_SECONDS
        if offered_rate is None or offered_rate < 1:
            raise TrialError("trial mode requires a positive --rate")
        rate = offered_rate
        official = True

    contender = _find_manifest(root, contender_id)
    manifest = describe_dataset(root)
    if not 1 <= repetition <= len(manifest["repetitionSeeds"]):
        raise TrialError(
            f"repetition must be between 1 and {len(manifest['repetitionSeeds'])}"
        )
    seed = int(manifest["repetitionSeeds"][repetition - 1])

    work_dir = output.parent / f".trial-work-{contender_id}-{mode}"
    owns_stack = False
    try:
        owns_stack = _ensure_fresh_contender(root, contender_id)
        try:
            template = inspect_template(root, contender_id)
        except DatasetError as error:
            raise TrialError(
                f"{error}; run `link-metrics dataset build {contender_id}` first"
            ) from error

        base_url = _contender_base_url(root, contender_id)
        expected_warm = max(rate * warm_seconds * 2, 100)
        warm_flags = build_validation_flags(root, repetition=repetition, count=int(expected_warm))
        run_k6(
            root,
            contender_id=contender_id,
            base_url=base_url,
            offered_rate=rate,
            duration_seconds=warm_seconds,
            repetition=repetition,
            validation_flags=warm_flags,
            work_dir=work_dir / "warm",
        )

        reset = reset_from_template(root, contender_id, template["templateChecksum"])
        inspect_contender(root, contender_id)

        expected_measure = max(rate * measure_seconds * 2, 100)
        measure_flags = build_validation_flags(
            root, repetition=repetition, count=int(expected_measure)
        )
        summary, saturated = run_k6(
            root,
            contender_id=contender_id,
            base_url=base_url,
            offered_rate=rate,
            duration_seconds=measure_seconds,
            repetition=repetition,
            validation_flags=measure_flags,
            work_dir=work_dir / "measure",
        )
        parsed = parse_k6_summary(summary)
        offered_iterations = rate * measure_seconds
        validity = evaluate_validity(
            {
                "droppedIterations": parsed["droppedIterations"],
                "k6CpuSaturated": saturated,
            }
        )
        names = _resource_names(root, contender_id)
        image_digest = _image_digest(names.image)
        return write_result_bundle(
            output,
            {
                "official": official,
                "mode": mode,
                "gitCommit": _git_commit(root),
                "protocolVersion": _protocol_version(root),
                "apiContractVersion": _api_contract_version(root),
                "datasetVersion": manifest["version"],
                "migrationVersion": _migration_version(root),
                "scenario": "registration",
                "repetition": repetition,
                "workloadSeed": seed,
                "contender": {
                    "id": contender_id,
                    "imageDigest": image_digest,
                    "manifest": contender,
                },
                "environment": {
                    "fingerprint": _environment_fingerprint(contender),
                    "k6Image": K6_IMAGE,
                    "postgresImage": POSTGRES_IMAGE,
                },
                "lifecycle": {
                    "warmSeconds": warm_seconds,
                    "measureSeconds": measure_seconds,
                    "templateChecksum": reset["templateChecksum"],
                },
                "workload": {
                    "offeredRate": rate,
                    "password": BENCHMARK_PASSWORD,
                    "transport": TRANSPORT,
                },
                "results": {
                    "offeredIterations": offered_iterations,
                    "achievedIterations": parsed["httpReqs"],
                    "droppedIterations": parsed["droppedIterations"],
                    "latency": parsed["latency"],
                    "errors": parsed["errors"],
                    "checksPassed": parsed["checksPassed"],
                    "checksFailed": parsed["checksFailed"],
                },
                "validity": {
                    **validity,
                    "k6SchedulingHealthy": parsed["droppedIterations"] == 0,
                    "k6CpuSaturated": saturated,
                },
                "contenderConstraints": CONTENDER_CONSTRAINTS,
            },
        )
    except (ContenderRuntimeError, DatasetError) as error:
        raise TrialError(str(error)) from error
    finally:
        _remove_k6(root, contender_id)
        if owns_stack:
            stop_contender(root, contender_id)
        else:
            _stop_contender_container(root, contender_id)
