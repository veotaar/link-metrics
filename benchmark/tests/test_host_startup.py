"""Host-validity and cold-start evidence at the control-plane seam."""

from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from link_metrics.environment import (
    LOCAL_RESOURCE_PROFILE,
    assess_host_preflight,
    summarize_host_execution,
)
from link_metrics.reporting import write_reports
from link_metrics.progress import ExecutionBudget
from link_metrics.results import ResultError
from link_metrics.startup import (
    resume_cold_start_repetitions,
    summarize_cold_startup,
    write_cold_start_bundle,
)
from link_metrics.startup import docker_started_at_ns


def host_observation() -> dict[str, object]:
    return {
        "cpuModel": "AMD Ryzen 7 7800X3D 8-Core Processor",
        "logicalCpuCount": 16,
        "load1": 0.25,
        "boost": {"observed": True, "enabled": False},
        "governors": {str(cpu): "performance" for cpu in range(8)},
        "threadSiblings": {str(cpu): [cpu, cpu + 8] for cpu in range(8)},
        "frequenciesKHz": {str(cpu): 4_200_000 for cpu in range(8)},
        "temperatureMilliCelsius": 55_000,
        "temperatureObserved": True,
        "thermalThrottlingActive": False,
        "thermalThrottlingObserved": False,
        "thermalThrottleCounts": {},
    }


def test_local_profile_assigns_isolated_physical_cores_memory_and_no_swap() -> None:
    assert LOCAL_RESOURCE_PROFILE["id"] == "local-7800x3d"
    assert LOCAL_RESOURCE_PROFILE["contender"] == {
        "cpusetCpus": "0-3",
        "memoryBytes": 8 * 1024**3,
        "memorySwapBytes": 8 * 1024**3,
    }
    assert LOCAL_RESOURCE_PROFILE["postgres"] == {
        "cpusetCpus": "4-5",
        "memoryBytes": 8 * 1024**3,
        "memorySwapBytes": 8 * 1024**3,
    }
    assert LOCAL_RESOURCE_PROFILE["k6"] == {
        "cpusetCpus": "6-7",
        "memoryBytes": 4 * 1024**3,
        "memorySwapBytes": 4 * 1024**3,
    }

    assessment = assess_host_preflight(LOCAL_RESOURCE_PROFILE, host_observation())

    assert assessment["valid"] is True
    assert assessment["reasons"] == []
    assert assessment["checks"]["physicalCoreIsolation"] is True


def test_preflight_rejects_smt_siblings_and_an_unstable_host() -> None:
    profile = deepcopy(LOCAL_RESOURCE_PROFILE)
    profile["contender"]["cpusetCpus"] = "0,8,1,2"
    observation = host_observation()
    observation.update(
        {
            "load1": 1.5,
            "boost": {"observed": True, "enabled": True},
            "governors": {**observation["governors"], "0": "powersave"},
            "frequenciesKHz": {**observation["frequenciesKHz"], "0": 3_500_000},
            "temperatureMilliCelsius": 85_000,
            "thermalThrottlingActive": True,
        }
    )

    assessment = assess_host_preflight(profile, observation)

    assert assessment["valid"] is False
    assert set(assessment["reasons"]) == {
        "cpu_profile_uses_smt_siblings",
        "host_not_quiescent",
        "performance_governor_required",
        "dynamic_boost_enabled",
        "cpu_frequency_out_of_tolerance",
        "cpu_temperature_out_of_tolerance",
        "thermal_throttling_active",
    }


def test_trial_host_evidence_invalidates_frequency_temperature_and_throttling() -> None:
    first = host_observation()
    second = host_observation()
    first["thermalThrottlingObserved"] = True
    first["thermalThrottleCounts"] = {str(cpu): 0 for cpu in range(8)}
    second["thermalThrottlingObserved"] = True
    second["thermalThrottleCounts"] = {str(cpu): 0 for cpu in range(8)}
    second["frequenciesKHz"] = {str(cpu): 3_800_000 for cpu in range(8)}
    second["temperatureMilliCelsius"] = 82_000
    second["thermalThrottleCounts"] = {**second["thermalThrottleCounts"], "3": 1}

    evidence = summarize_host_execution(LOCAL_RESOURCE_PROFILE, [first, second])

    assert evidence["observed"] is True
    assert evidence["sampleCount"] == 2
    assert evidence["valid"] is False
    assert set(evidence["reasons"]) == {
        "cpu_frequency_out_of_tolerance",
        "cpu_temperature_out_of_tolerance",
        "thermal_throttling_observed",
    }
    assert evidence["frequencyKHz"]["minimum"] == 3_800_000
    assert evidence["temperatureMilliCelsius"]["peak"] == 82_000
    assert evidence["thermalThrottleIncrements"] == {"3": 1}


def test_amd_temperature_and_frequency_are_valid_without_intel_throttle_counters() -> None:
    observation = host_observation()

    preflight = assess_host_preflight(LOCAL_RESOURCE_PROFILE, observation)
    execution = summarize_host_execution(LOCAL_RESOURCE_PROFILE, [observation])

    assert preflight["valid"] is True
    assert preflight["checks"]["temperatureSignal"] is True
    assert execution["observed"] is True
    assert execution["valid"] is True
    assert execution["thermalThrottleCountersObserved"] is False


def test_missing_cpu_temperature_fails_preflight_and_execution_evidence() -> None:
    observation = host_observation()
    observation["temperatureObserved"] = False
    observation["temperatureMilliCelsius"] = None

    preflight = assess_host_preflight(LOCAL_RESOURCE_PROFILE, observation)
    execution = summarize_host_execution(LOCAL_RESOURCE_PROFILE, [observation])

    assert preflight["valid"] is False
    assert "cpu_temperature_observation_missing" in preflight["reasons"]
    assert execution["observed"] is False
    assert execution["valid"] is False
    assert "host_execution_evidence_missing" in execution["reasons"]


def test_host_preflight_command_reports_current_machine_without_mutating_it() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "link_metrics", "host", "preflight"],
        check=False,
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)
    assert result.returncode in {0, 2}
    assert payload["profile"] == "local-7800x3d"
    assert payload["valid"] is (result.returncode == 0)
    assert "observation" in payload


def test_docker_started_at_uses_the_process_timestamp_not_client_invocation() -> None:
    assert docker_started_at_ns("2026-07-21T10:20:30.123456789Z") == 1_784_629_230_123_456_789


def test_cold_start_bundle_requires_twenty_repetitions_and_reports_median_p95(
    tmp_path: Path,
) -> None:
    samples = [
        {
            "repetition": repetition,
            "readinessMs": float(repetition * 10),
            "firstRequestMs": float(repetition),
            "readinessStatus": 204,
            "firstRequestStatus": 201,
        }
        for repetition in range(1, 21)
    ]

    summary = summarize_cold_startup(samples)
    bundle = write_cold_start_bundle(
        tmp_path / "startup.json",
        {
            "official": True,
            "kind": "cold-start-series",
            "gitCommit": "abc123",
            "apiContractVersion": "1.0.1",
            "protocolVersion": "3.0.0",
            "datasetVersion": "1.2.0",
            "migrationVersion": "20260101000000",
            "environment": {
                "fingerprint": {"resourceProfile": "local-7800x3d"},
                "preflight": {"valid": True},
                "execution": {"valid": True},
            },
            "contender": {
                "id": "express-node",
                "imageDigest": "sha256:abc",
                "manifest": {"id": "express-node"},
            },
            "lifecycle": {
                "databaseReadyBeforeProcessStart": True,
                "excluded": ["imageBuild", "imagePull", "migrations", "seeding"],
                "readiness": "first /health 204",
                "firstRequest": "POST /api/auth/register",
            },
            "repetitions": samples,
            "summary": summary,
            "validity": {"valid": True, "reasons": []},
        },
    )

    assert bundle["summary"]["readinessMs"] == {"median": 105.0, "p95": 190.5}
    assert bundle["summary"]["firstRequestMs"] == {"median": 10.5, "p95": 19.05}
    assert len(bundle["repetitions"]) == 20


def test_failed_first_request_remains_auditable_invalid_startup_evidence(
    tmp_path: Path,
) -> None:
    samples = [
        {
            "repetition": repetition,
            "readinessMs": 100.0,
            "firstRequestMs": 5.0,
            "readinessStatus": 204,
            "firstRequestStatus": 500 if repetition == 20 else 201,
        }
        for repetition in range(1, 21)
    ]

    summary = summarize_cold_startup(samples)
    bundle = write_cold_start_bundle(
        tmp_path / "invalid-startup.json",
        {
            "official": True,
            "kind": "cold-start-series",
            "gitCommit": "abc123",
            "apiContractVersion": "1.0.1",
            "protocolVersion": "3.0.0",
            "datasetVersion": "1.2.0",
            "migrationVersion": "20260101000000",
            "environment": {
                "fingerprint": {"resourceProfile": "local-7800x3d"},
                "preflight": {"valid": True},
                "execution": {"valid": False},
            },
            "contender": {
                "id": "express-node",
                "imageDigest": "sha256:abc",
                "manifest": {"id": "express-node"},
            },
            "lifecycle": {"processStartBoundary": "Docker State.StartedAt"},
            "repetitions": samples,
            "summary": summary,
            "validity": {
                "valid": False,
                "reasons": ["first_real_request_failed"],
            },
        },
    )

    assert bundle["summary"]["firstRequestMs"]["median"] == 5.0
    assert bundle["repetitions"][-1]["firstRequestStatus"] == 500
    assert bundle["validity"]["valid"] is False


def test_official_cold_start_bundle_requires_full_provenance(tmp_path: Path) -> None:
    samples = [
        {
            "repetition": repetition,
            "readinessMs": 100.0,
            "firstRequestMs": 5.0,
            "readinessStatus": 204,
            "firstRequestStatus": 201,
        }
        for repetition in range(1, 21)
    ]

    try:
        write_cold_start_bundle(
            tmp_path / "incomplete.json",
            {
                "official": True,
                "kind": "cold-start-series",
                "repetitions": samples,
                "summary": summarize_cold_startup(samples),
            },
        )
    except Exception as error:
        assert "provenance" in str(error)
    else:
        raise AssertionError("expected incomplete official evidence to be rejected")


def test_cold_start_repetitions_resume_without_remeasuring_completed_evidence(
    tmp_path: Path,
) -> None:
    output = tmp_path / "express-node.json"
    measured: list[int] = []

    def measure(repetition: int) -> dict[str, object]:
        assert repetition not in measured
        measured.append(repetition)
        return {
            "sample": {
                "repetition": repetition,
                "readinessMs": float(repetition),
                "firstRequestMs": 5.0,
                "readinessStatus": 204,
                "firstRequestStatus": 201,
                "templateChecksum": "sha256:template",
            },
            "hostObservations": [host_observation(), host_observation()],
        }

    paused = resume_cold_start_repetitions(
        output,
        contender_id="express-node",
        template_checksum="sha256:template",
        measure=measure,
        budget=ExecutionBudget(maximum_units=3),
    )

    assert paused["status"] == "paused"
    assert paused["completedRepetitions"] == 3
    assert measured == [1, 2, 3]

    completed = resume_cold_start_repetitions(
        output,
        contender_id="express-node",
        template_checksum="sha256:template",
        measure=measure,
        budget=ExecutionBudget(),
    )

    assert completed["status"] == "complete"
    assert completed["completedRepetitions"] == 20
    assert measured == list(range(1, 21))
    assert [
        item["sample"]["repetition"] for item in completed["repetitions"]
    ] == list(range(1, 21))


def test_reports_keep_cold_start_separate_from_warm_capacity(tmp_path: Path) -> None:
    raw = tmp_path / "startup.json"
    samples = [
        {
            "repetition": repetition,
            "readinessMs": 100.0,
            "firstRequestMs": 5.0,
            "readinessStatus": 204,
            "firstRequestStatus": 201,
        }
        for repetition in range(1, 21)
    ]
    raw.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "kind": "cold-start-series",
                "official": True,
                "gitCommit": "abc123",
                "apiContractVersion": "1.0.1",
                "protocolVersion": "3.0.0",
                "datasetVersion": "1.2.0",
                "migrationVersion": "20260101000000",
                "environment": {
                    "fingerprint": {"resourceProfile": "local-7800x3d"},
                    "preflight": {"valid": True},
                    "execution": {"valid": True},
                },
                "contender": {
                    "id": "express-node",
                    "imageDigest": "sha256:abc",
                    "manifest": {"id": "express-node"},
                },
                "lifecycle": {"processStartBoundary": "Docker State.StartedAt"},
                "repetitions": samples,
                "summary": summarize_cold_startup(samples),
                "validity": {"valid": True, "reasons": []},
            }
        ),
        encoding="utf-8",
    )

    write_reports([raw], tmp_path / "report")

    compact = json.loads((tmp_path / "report" / "summary.json").read_text())
    markdown = (tmp_path / "report" / "report.md").read_text(encoding="utf-8")
    assert compact["scenarios"] == []
    assert compact["coldStartup"][0]["contender"] == "express-node"
    assert compact["coldStartup"][0]["summary"]["readinessMs"]["median"] == 100.0
    assert compact["coldStartup"][0]["comparabilityKey"] == compact["comparabilityKey"]
    assert compact["coldStartup"][0]["hostExecution"] == {"valid": True}
    assert "## Cold startup" in markdown
    assert "Cold-start host execution evidence" in markdown
    assert "warm capacity" not in markdown.lower()
    assert "composite" not in markdown.lower()


def test_reports_reject_cold_start_series_with_unlike_comparability_keys(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    samples = [
        {
            "repetition": repetition,
            "readinessMs": 100.0,
            "firstRequestMs": 5.0,
            "readinessStatus": 204,
            "firstRequestStatus": 201,
        }
        for repetition in range(1, 21)
    ]
    bundle = {
        "schemaVersion": 1,
        "kind": "cold-start-series",
        "official": True,
        "gitCommit": "abc123",
        "apiContractVersion": "1.0.1",
        "protocolVersion": "4.0.0",
        "datasetVersion": "1.2.0",
        "migrationVersion": "20260101000000",
        "environment": {
            "fingerprint": {"resourceProfile": "local-7800x3d", "kernel": "one"},
            "preflight": {"valid": True},
            "execution": {"valid": True},
        },
        "contender": {
            "id": "express-node",
            "imageDigest": "sha256:abc",
            "manifest": {"id": "express-node"},
        },
        "lifecycle": {"processStartBoundary": "Docker State.StartedAt"},
        "repetitions": samples,
        "summary": summarize_cold_startup(samples),
        "validity": {"valid": True, "reasons": []},
    }
    first.write_text(json.dumps(bundle), encoding="utf-8")
    changed = json.loads(json.dumps(bundle))
    changed["contender"]["id"] = "hono-bun"
    changed["environment"]["fingerprint"]["kernel"] = "two"
    second.write_text(json.dumps(changed), encoding="utf-8")

    with pytest.raises(ResultError, match="unlike comparability keys"):
        write_reports([first, second], tmp_path / "report")
