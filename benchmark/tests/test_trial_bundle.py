"""Result-bundle seam for registration Trials."""

from __future__ import annotations

import json
from pathlib import Path

from link_metrics.trial import (
    TrialError,
    evaluate_validity,
    registration_email,
    write_result_bundle,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_registration_emails_are_deterministic_unique_and_non_colliding() -> None:
    first = registration_email(repetition=1, iteration=0)
    second = registration_email(repetition=1, iteration=1)
    third = registration_email(repetition=2, iteration=0)

    assert first == "reg-01-000000000000@trial.invalid"
    assert second == "reg-01-000000000001@trial.invalid"
    assert third == "reg-02-000000000000@trial.invalid"
    assert first != second
    assert not first.startswith("benchmark-user-")


def test_trial_is_invalid_when_k6_cannot_schedule_or_is_cpu_saturated() -> None:
    healthy = evaluate_validity(
        {
            "droppedIterations": 0,
            "unexpectedResponses": 0,
            "transportFailures": 0,
            "k6CpuSaturated": False,
            "offeredIterations": 100,
            "completedIterations": 100,
        }
    )
    assert healthy == {"valid": True, "reasons": []}

    dropped = evaluate_validity(
        {
            "droppedIterations": 3,
            "unexpectedResponses": 0,
            "transportFailures": 0,
            "k6CpuSaturated": False,
            "offeredIterations": 100,
            "completedIterations": 97,
        }
    )
    assert dropped["valid"] is False
    assert "k6_could_not_schedule_offered_rate" in dropped["reasons"]

    saturated = evaluate_validity(
        {
            "droppedIterations": 0,
            "unexpectedResponses": 0,
            "transportFailures": 0,
            "k6CpuSaturated": True,
            "offeredIterations": 100,
            "completedIterations": 100,
        }
    )
    assert saturated["valid"] is False
    assert "k6_cpu_saturated" in saturated["reasons"]


def test_smoke_bundle_is_explicitly_nonofficial_and_auditable(tmp_path: Path) -> None:
    output = tmp_path / "smoke.json"
    bundle = write_result_bundle(
        output,
        {
            "official": False,
            "mode": "smoke",
            "gitCommit": "abc123",
            "protocolVersion": "1.0.0",
            "apiContractVersion": "1.0.1",
            "datasetVersion": "1.2.0",
            "migrationVersion": "20260101000000",
            "scenario": "registration",
            "repetition": 1,
            "workloadSeed": 1_350_403_001_542_084_573,
            "contender": {
                "id": "express-node",
                "imageDigest": "sha256:deadbeef",
                "manifest": {"id": "express-node", "resourceProfile": "local-7800x3d"},
            },
            "environment": {
                "fingerprint": {"resourceProfile": "local-7800x3d", "kernel": "linux"},
                "k6Image": "grafana/k6:1.4.2@sha256:3656673de3f30424e8ebcfa46acd9558d83b6a43612d0f668ffeac953950c6c7",
                "postgresImage": "postgres:18.4-bookworm@sha256:16fa100a3a6e92c0556632870455e7f8c6f3df5cefddd67d6b95292732bd7ff0",
            },
            "lifecycle": {
                "warmSeconds": 2,
                "measureSeconds": 5,
                "templateChecksum": "a" * 64,
            },
            "workload": {
                "offeredRate": 2,
                "password": "link-metrics-benchmark-only",
                "transport": {
                    "protocol": "HTTP/1.1",
                    "tls": False,
                    "keepAlive": True,
                    "maxConnections": 256,
                    "httpTimeoutSeconds": 5,
                },
            },
            "results": {
                "offeredIterations": 10,
                "achievedIterations": 10,
                "droppedIterations": 0,
                "latency": {"avgMs": 12.5, "p99Ms": 40.0},
                "errors": {
                    "transportFailures": 0,
                    "unexpectedResponses": 0,
                    "checksFailed": 0,
                },
            },
            "validity": {"valid": True, "reasons": [], "k6SchedulingHealthy": True, "k6CpuSaturated": False},
            "contenderConstraints": {
                "maxPoolConnections": 20,
                "connectionTimeoutMillis": 2000,
                "statementTimeoutMillis": 2000,
                "successPathAccessLogging": False,
            },
        },
    )

    assert output.is_file()
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded == bundle
    assert loaded["official"] is False
    assert loaded["mode"] == "smoke"
    assert loaded["schemaVersion"] == 1
    assert loaded["scenario"] == "registration"
    assert loaded["workload"]["password"] == "link-metrics-benchmark-only"
    assert set(loaded) >= {
        "schemaVersion",
        "official",
        "gitCommit",
        "protocolVersion",
        "apiContractVersion",
        "datasetVersion",
        "migrationVersion",
        "scenario",
        "repetition",
        "workloadSeed",
        "contender",
        "environment",
        "lifecycle",
        "workload",
        "results",
        "validity",
        "contenderConstraints",
    }


def test_write_result_bundle_rejects_official_smoke_label(tmp_path: Path) -> None:
    try:
        write_result_bundle(
            tmp_path / "bad.json",
            {
                "official": True,
                "mode": "smoke",
                "gitCommit": "abc",
                "protocolVersion": "1.0.0",
                "apiContractVersion": "1.0.1",
                "datasetVersion": "1.2.0",
                "migrationVersion": "1",
                "scenario": "registration",
                "repetition": 1,
                "workloadSeed": 1,
                "contender": {"id": "express-node", "imageDigest": "sha256:x", "manifest": {}},
                "environment": {"fingerprint": {}, "k6Image": "x", "postgresImage": "y"},
                "lifecycle": {"warmSeconds": 2, "measureSeconds": 5, "templateChecksum": "a" * 64},
                "workload": {
                    "offeredRate": 1,
                    "password": "link-metrics-benchmark-only",
                    "transport": {
                        "protocol": "HTTP/1.1",
                        "tls": False,
                        "keepAlive": True,
                        "maxConnections": 256,
                        "httpTimeoutSeconds": 5,
                    },
                },
                "results": {
                    "offeredIterations": 1,
                    "achievedIterations": 1,
                    "droppedIterations": 0,
                    "latency": {},
                    "errors": {
                        "transportFailures": 0,
                        "unexpectedResponses": 0,
                        "checksFailed": 0,
                    },
                },
                "validity": {
                    "valid": True,
                    "reasons": [],
                    "k6SchedulingHealthy": True,
                    "k6CpuSaturated": False,
                },
                "contenderConstraints": {
                    "maxPoolConnections": 20,
                    "connectionTimeoutMillis": 2000,
                    "statementTimeoutMillis": 2000,
                    "successPathAccessLogging": False,
                },
            },
        )
    except TrialError as error:
        assert "smoke" in str(error)
    else:
        raise AssertionError("expected TrialError")
