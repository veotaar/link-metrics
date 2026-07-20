"""Registration Trial orchestration and immutable result bundles."""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from link_metrics.conformance import run_conformance_checks
from link_metrics.dataset import (
    DatasetError,
    ReferenceTokenCorpus,
    build_reference_token_corpus,
    describe_dataset,
    sample_workload,
)
from link_metrics.dataset_runtime import inspect_template, reset_from_template
from link_metrics.evidence import write_immutable_json
from link_metrics.scenarios import (
    PROTECTED_SCENARIOS,
    SCENARIO_CONFIGURATIONS,
    SCENARIOS,
)
from link_metrics.runtime import (
    CONTROL_ROLE,
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
SCENARIO_SCRIPT = Path("benchmark/protocol/k6/scenario.js")
BENCHMARK_PASSWORD = "link-metrics-benchmark-only"

SMOKE_WARM_SECONDS = 2
SMOKE_MEASURE_SECONDS = 5
SMOKE_OFFERED_RATE = 2
TRIAL_WARM_SECONDS = 30
TRIAL_MEASURE_SECONDS = 60


@dataclass(frozen=True)
class TrialMode:
    warm_seconds: int
    measure_seconds: int
    official: bool
    uses_official_resource_profile: bool
    fixed_rate: float | None = None


TRIAL_MODES = {
    "smoke": TrialMode(
        warm_seconds=SMOKE_WARM_SECONDS,
        measure_seconds=SMOKE_MEASURE_SECONDS,
        official=False,
        uses_official_resource_profile=False,
        fixed_rate=SMOKE_OFFERED_RATE,
    ),
    "calibration": TrialMode(
        warm_seconds=TRIAL_WARM_SECONDS,
        measure_seconds=TRIAL_MEASURE_SECONDS,
        official=False,
        uses_official_resource_profile=True,
    ),
    "trial": TrialMode(
        warm_seconds=TRIAL_WARM_SECONDS,
        measure_seconds=TRIAL_MEASURE_SECONDS,
        official=True,
        uses_official_resource_profile=True,
    ),
}

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

LOCAL_RESOURCE_PROFILE = {
    "contender": {"cpusetCpus": "0-3", "memoryBytes": 8 * 1024**3},
    "postgres": {"cpusetCpus": "4-5", "memoryBytes": 8 * 1024**3},
    "k6": {"cpusetCpus": "6-7", "memoryBytes": 4 * 1024**3},
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
    if not bool(metrics.get("k6CpuObserved", True)):
        reasons.append("k6_cpu_observation_failed")
    return {"valid": not reasons, "reasons": reasons}


def write_result_bundle(output: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist an immutable machine-readable Trial bundle."""
    official = bool(payload.get("official"))
    mode = payload.get("mode")
    configuration = TRIAL_MODES.get(str(mode))
    if configuration is None:
        raise TrialError("mode must be smoke, calibration, or trial")
    if official and not configuration.official:
        raise TrialError(f"{mode} Trial bundles must be nonofficial")

    bundle = {"schemaVersion": 1, **payload}
    try:
        return write_immutable_json(output, bundle)
    except FileExistsError as error:
        raise TrialError(f"result bundle already exists: {output}") from error


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


def _container_limits(container: str) -> dict[str, Any]:
    document = _container_document(container)
    host_config = document["HostConfig"]
    return {
        "cpusetCpus": host_config.get("CpusetCpus") or None,
        "memoryBytes": int(host_config.get("Memory") or 0),
        "memorySwapBytes": int(host_config.get("MemorySwap") or 0),
        "nanoCpus": int(host_config.get("NanoCpus") or 0),
    }


def _environment_fingerprint(
    root: Path, contender: dict[str, Any], *, k6_limits: dict[str, Any]
) -> dict[str, Any]:
    memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    names = _resource_names(root, contender["id"])
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
        "containers": {
            "contender": _container_limits(names.contender),
            "postgres": _container_limits(names.database),
            "k6": k6_limits,
        },
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


_DOCKER_SIZE_UNITS = {
    "B": 1,
    "kB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
}


def _docker_bytes(value: str) -> int:
    match = re.fullmatch(r"\s*([0-9.]+)\s*([kMGT]?i?B)\s*", value)
    if not match or match.group(2) not in _DOCKER_SIZE_UNITS:
        raise ValueError(f"unrecognized Docker size: {value}")
    return round(float(match.group(1)) * _DOCKER_SIZE_UNITS[match.group(2)])


def _resource_sample(containers: list[str]) -> dict[str, dict[str, Any]]:
    raw = _docker(
        "stats",
        "--no-stream",
        "--format",
        "{{json .}}",
        *containers,
        check=False,
    )
    if raw.returncode != 0:
        return {}
    samples: dict[str, dict[str, Any]] = {}
    try:
        for line in raw.stdout.splitlines():
            item = json.loads(line)
            memory_usage, _ = item["MemUsage"].split("/", 1)
            network_received, network_sent = item["NetIO"].split("/", 1)
            block_read, block_written = item["BlockIO"].split("/", 1)
            samples[item["Name"]] = {
                "cpuPercent": float(item["CPUPerc"].strip().rstrip("%")),
                "residentMemoryBytes": _docker_bytes(memory_usage),
                "networkReceivedBytes": _docker_bytes(network_received),
                "networkSentBytes": _docker_bytes(network_sent),
                "blockReadBytes": _docker_bytes(block_read),
                "blockWrittenBytes": _docker_bytes(block_written),
            }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return samples


def _postgres_activity_snapshot(container: str) -> dict[str, Any]:
    query = """
SELECT json_build_object(
  'transactions', json_build_object(
    'committed', xact_commit,
    'rolledBack', xact_rollback,
    'deadlocks', deadlocks
  ),
  'databaseBlocks', json_build_object('read', blks_read, 'cacheHits', blks_hit),
  'locks', (
    SELECT json_build_object(
      'granted', count(*) FILTER (WHERE granted),
      'waiting', count(*) FILTER (WHERE NOT granted)
    ) FROM pg_catalog.pg_locks
  ),
  'ioOperations', (
    SELECT json_build_object(
      'reads', coalesce(sum(reads), 0),
      'writes', coalesce(sum(writes), 0),
      'writebacks', coalesce(sum(writebacks), 0),
      'extends', coalesce(sum(extends), 0),
      'fsyncs', coalesce(sum(fsyncs), 0)
    ) FROM pg_catalog.pg_stat_io
  )
)
FROM pg_catalog.pg_stat_database
WHERE datname = current_database();
"""
    result = _docker(
        "exec",
        container,
        "psql",
        "--username",
        CONTROL_ROLE,
        "--dbname",
        DATABASE_NAME,
        "--tuples-only",
        "--no-align",
        "--command",
        query,
        check=False,
    )
    if result.returncode != 0:
        return {"observed": False, "error": result.stderr.strip() or "psql failed"}
    try:
        return {"observed": True, "values": json.loads(result.stdout.strip())}
    except json.JSONDecodeError:
        return {"observed": False, "error": "PostgreSQL returned invalid telemetry JSON"}


def _summarize_resource_samples(
    samples: dict[str, list[tuple[float, dict[str, Any]]]]
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for container, observations in samples.items():
        if not observations:
            summary[container] = {"observed": False}
            continue
        cpu_seconds = 0.0
        for (previous_at, _), (sampled_at, sample) in zip(observations, observations[1:]):
            cpu_seconds += sample["cpuPercent"] / 100 * (sampled_at - previous_at)
        memories = [sample["residentMemoryBytes"] for _, sample in observations]
        first = observations[0][1]
        last = observations[-1][1]
        summary[container] = {
            "observed": True,
            "sampleCount": len(observations),
            "cpuTimeSecondsEstimate": cpu_seconds,
            "averageResidentMemoryBytes": sum(memories) / len(memories),
            "peakResidentMemoryBytes": max(memories),
            "networkBytes": {
                "received": max(0, last["networkReceivedBytes"] - first["networkReceivedBytes"]),
                "sent": max(0, last["networkSentBytes"] - first["networkSentBytes"]),
            },
            "blockIoBytes": {
                "read": max(0, last["blockReadBytes"] - first["blockReadBytes"]),
                "written": max(0, last["blockWrittenBytes"] - first["blockWrittenBytes"]),
            },
        }
    return summary


def _apply_official_resource_profile(root: Path, contender_id: str) -> None:
    """Apply the versioned local profile before an official measurement."""
    names = _resource_names(root, contender_id)
    for container, profile in (
        (names.contender, LOCAL_RESOURCE_PROFILE["contender"]),
        (names.database, LOCAL_RESOURCE_PROFILE["postgres"]),
    ):
        memory = str(profile["memoryBytes"])
        _docker(
            "update",
            "--cpuset-cpus",
            str(profile["cpusetCpus"]),
            "--memory",
            memory,
            "--memory-swap",
            memory,
            container,
        )
        actual = _container_limits(container)
        expected = {
            "cpusetCpus": profile["cpusetCpus"],
            "memoryBytes": profile["memoryBytes"],
            "memorySwapBytes": profile["memoryBytes"],
            "nanoCpus": 0,
        }
        if actual != expected:
            raise TrialError(
                f"container {container} does not match the official resource profile: "
                f"expected {expected}, got {actual}"
            )


def run_k6(
    root: Path,
    *,
    scenario: str,
    contender_id: str,
    base_url: str,
    offered_rate: float,
    duration_seconds: int,
    repetition: int,
    workload: dict[str, Any],
    reference_tokens: ReferenceTokenCorpus | None,
    work_dir: Path,
    official: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """Run the pinned Scenario script against a Contender URL."""
    root = root.resolve()
    if scenario not in SCENARIOS:
        raise TrialError(f"unknown Scenario: {scenario}")
    if scenario in PROTECTED_SCENARIOS and reference_tokens is None:
        raise TrialError(f"{scenario} requires a reference-token corpus")
    if scenario not in PROTECTED_SCENARIOS and reference_tokens is not None:
        raise TrialError(f"{scenario} does not use reference tokens")
    script = (root / SCENARIO_SCRIPT).resolve()
    if not script.is_file():
        raise TrialError(f"missing k6 script at {script}")

    work_dir.mkdir(parents=True, exist_ok=True)
    work_dir.chmod(0o777)
    summary_path = work_dir / "summary.json"
    if summary_path.exists():
        summary_path.unlink()
    workload_path = work_dir / "workload.json"
    workload_path.write_text(
        json.dumps(workload, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    workload_path.chmod(0o644)
    tokens_path = work_dir / "reference-tokens.json"
    if reference_tokens is not None:
        tokens_path.write_bytes(reference_tokens.serialized)
        tokens_path.chmod(0o644)

    names = _resource_names(root, contender_id)
    network = names.network
    container = _k6_container_name(root, contender_id)
    _remove_k6(root, contender_id)

    contender = _find_manifest(root, contender_id)
    private_url = f"http://{names.contender}:{contender['port']}"
    target_url = private_url if base_url.startswith("http://127.0.0.1") else base_url

    pre_allocated = 256
    max_vus = 256
    assigned_cpus = 2.0

    try:
        _docker("pull", K6_IMAGE, check=False)
        docker_arguments = [
            "run",
            "--detach",
            "--name",
            container,
            "--network",
            network,
            "--cpus",
            "2",
        ]
        if official:
            docker_arguments.extend(
                ["--cpuset-cpus", str(LOCAL_RESOURCE_PROFILE["k6"]["cpusetCpus"])]
            )
        docker_arguments.extend(
            [
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
                f"SCENARIO={scenario}",
                "--env",
                f"PASSWORD={BENCHMARK_PASSWORD}",
                "--env",
                f"PRE_ALLOCATED_VUS={pre_allocated}",
                "--env",
                f"MAX_VUS={max_vus}",
                "--env",
                "WORKLOAD_PATH=/work/workload.json",
                "--env",
                "SUMMARY_PATH=/work/summary.json",
            ]
        )
        if reference_tokens is not None:
            docker_arguments.extend(["--env", "TOKENS_PATH=/work/reference-tokens.json"])
        docker_arguments.extend([K6_IMAGE, "run", script.name])
        _docker(*docker_arguments)
        actual_k6_limits = _container_limits(container)
        if official:
            expected_k6_limits = {
                "cpusetCpus": LOCAL_RESOURCE_PROFILE["k6"]["cpusetCpus"],
                "memoryBytes": LOCAL_RESOURCE_PROFILE["k6"]["memoryBytes"],
                "memorySwapBytes": LOCAL_RESOURCE_PROFILE["k6"]["memoryBytes"],
                "nanoCpus": 2_000_000_000,
            }
            if actual_k6_limits != expected_k6_limits:
                raise TrialError(
                    "k6 does not match the official resource profile: "
                    f"expected {expected_k6_limits}, got {actual_k6_limits}"
                )
        peak_cpu = 0.0
        cpu_samples = 0
        cpu_sample_failures = 0
        telemetry_containers = {
            "contender": names.contender,
            "postgres": names.database,
            "k6": container,
        }
        measured_containers = list(telemetry_containers.values())
        resource_samples: dict[str, list[tuple[float, dict[str, Any]]]] = {
            name: [] for name in measured_containers
        }
        deadline = time.monotonic() + duration_seconds + 120
        while time.monotonic() < deadline:
            document = _container_document(container)
            sampled_at = time.monotonic()
            sample = _resource_sample(measured_containers)
            for name, values in sample.items():
                resource_samples[name].append((sampled_at, values))
            k6_sample = sample.get(container)
            if k6_sample is None:
                cpu_sample_failures += 1
            else:
                cpu_samples += 1
                peak_cpu = max(peak_cpu, k6_sample["cpuPercent"])
            if document["State"]["Status"] != "running":
                break
            time.sleep(0.5)
        else:
            _docker("kill", container, check=False)
            raise TrialError("k6 did not finish within the Trial watchdog window")

        logs = _docker("logs", container, check=False)
        exit_code = int(document["State"].get("ExitCode", 1))
        if exit_code != 0:
            raise TrialError(
                f"k6 exited with status {exit_code}: "
                + (logs.stderr.strip() or logs.stdout.strip() or "no container logs")
            )
        saturation_threshold = (assigned_cpus * 100.0) * 0.95
        saturated = peak_cpu >= saturation_threshold
        if not summary_path.is_file():
            raise TrialError(
                "k6 did not emit summary.json: "
                + (logs.stderr.strip() or logs.stdout.strip() or "no container logs")
            )
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summarized_resources = _summarize_resource_samples(resource_samples)
        return summary, {
            "assignedCpus": assigned_cpus,
            "peakCpuPercent": peak_cpu,
            "saturationThresholdPercent": saturation_threshold,
            "sampleCount": cpu_samples,
            "sampleFailures": cpu_sample_failures,
            "observed": cpu_samples > 0,
            "saturated": saturated,
            "containerLimits": actual_k6_limits,
        }, {
            role: summarized_resources[container_name]
            for role, container_name in telemetry_containers.items()
        }, reference_tokens.evidence if reference_tokens is not None else None
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


def run_scenario_trial(
    root: Path,
    contender_id: str,
    *,
    scenario: str,
    output: Path,
    mode: str,
    repetition: int = 1,
    offered_rate: float | None = None,
    reference_tokens: ReferenceTokenCorpus | None = None,
) -> dict[str, Any]:
    """Execute one Scenario Trial lifecycle and write its raw bundle."""
    root = root.resolve()
    if output.exists():
        raise TrialError(f"result bundle already exists: {output}")
    if scenario not in SCENARIOS:
        raise TrialError(f"unknown Scenario: {scenario}")
    configuration = TRIAL_MODES.get(mode)
    if configuration is None:
        raise TrialError("mode must be smoke, calibration, or trial")
    if configuration.fixed_rate is None:
        if offered_rate is None or offered_rate <= 0:
            raise TrialError(f"{mode} mode requires a positive offered rate")
        rate = offered_rate
    else:
        rate = configuration.fixed_rate
    warm_seconds = configuration.warm_seconds
    measure_seconds = configuration.measure_seconds
    official = configuration.official
    uses_official_resource_profile = configuration.uses_official_resource_profile

    contender = _find_manifest(root, contender_id)
    manifest = describe_dataset(root)
    if not 1 <= repetition <= len(manifest["repetitionSeeds"]):
        raise TrialError(
            f"repetition must be between 1 and {len(manifest['repetitionSeeds'])}"
        )
    seed = int(manifest["repetitionSeeds"][repetition - 1])

    work_dir = output.parent / f".trial-work-{contender_id}-{mode}"
    if scenario in PROTECTED_SCENARIOS:
        reference_tokens = reference_tokens or build_reference_token_corpus(
            root, repetition
        )
    elif reference_tokens is not None:
        raise TrialError(f"{scenario} does not use reference tokens")
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
        conformance = None
        if uses_official_resource_profile:
            _apply_official_resource_profile(root, contender_id)
        if official:
            conformance = run_conformance_checks(root, contender_id, base_url)
        expected_warm = max(rate * warm_seconds * 2, 100)
        warm_workload = sample_workload(root, repetition, int(expected_warm))
        run_k6(
            root,
            scenario=scenario,
            contender_id=contender_id,
            base_url=base_url,
            offered_rate=rate,
            duration_seconds=warm_seconds,
            repetition=repetition,
            workload=warm_workload,
            reference_tokens=reference_tokens,
            work_dir=work_dir / "warm",
            official=uses_official_resource_profile,
        )

        reset = reset_from_template(root, contender_id, template["templateChecksum"])
        inspect_contender(root, contender_id)

        expected_measure = max(rate * measure_seconds * 2, 100)
        measure_workload = sample_workload(root, repetition, int(expected_measure))
        postgres_before = _postgres_activity_snapshot(
            _resource_names(root, contender_id).database
        )
        summary, k6_health, resource_telemetry, token_evidence = run_k6(
            root,
            scenario=scenario,
            contender_id=contender_id,
            base_url=base_url,
            offered_rate=rate,
            duration_seconds=measure_seconds,
            repetition=repetition,
            workload=measure_workload,
            reference_tokens=reference_tokens,
            work_dir=work_dir / "measure",
            official=uses_official_resource_profile,
        )
        postgres_after = _postgres_activity_snapshot(
            _resource_names(root, contender_id).database
        )
        parsed = parse_k6_summary(summary)
        offered_iterations = rate * measure_seconds
        validity = evaluate_validity(
            {
                "droppedIterations": parsed["droppedIterations"],
                "k6CpuSaturated": k6_health["saturated"],
                "k6CpuObserved": k6_health["observed"],
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
                "repetitionSeeds": manifest["repetitionSeeds"],
                "migrationVersion": _migration_version(root),
                "scenario": scenario,
                "repetition": repetition,
                "workloadSeed": seed,
                "contender": {
                    "id": contender_id,
                    "imageDigest": image_digest,
                    "manifest": contender,
                },
                "environment": {
                    "fingerprint": _environment_fingerprint(
                        root,
                        contender,
                        k6_limits=k6_health["containerLimits"],
                    ),
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
                    "scenarioConfiguration": SCENARIO_CONFIGURATIONS[scenario],
                    "referenceTokens": token_evidence,
                },
                "results": {
                    "offeredIterations": offered_iterations,
                    "achievedIterations": parsed["httpReqs"],
                    "droppedIterations": parsed["droppedIterations"],
                    "latency": parsed["latency"],
                    "errors": parsed["errors"],
                    "checksPassed": parsed["checksPassed"],
                    "checksFailed": parsed["checksFailed"],
                    "resourceTelemetry": resource_telemetry,
                    "postgresTelemetry": {
                        "before": postgres_before,
                        "after": postgres_after,
                    },
                },
                "validity": {
                    **validity,
                    "k6SchedulingHealthy": parsed["droppedIterations"] == 0,
                    "k6CpuSaturated": k6_health["saturated"],
                    "k6CpuEvidence": k6_health,
                },
                "conformance": conformance,
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
