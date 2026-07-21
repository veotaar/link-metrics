"""Trial lifecycle and CLI seam for registration."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from link_metrics.dataset import sample_workload
from link_metrics.trial import (
    CONTENDER_CONSTRAINTS,
    SMOKE_MEASURE_SECONDS,
    SMOKE_OFFERED_RATE,
    SMOKE_WARM_SECONDS,
    SCENARIO_CONFIGURATIONS,
    TRIAL_MEASURE_SECONDS,
    TRIAL_WARM_SECONDS,
    build_validation_flags,
    parse_k6_summary,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TRIAL_E2E_ENABLED = os.environ.get("LINK_METRICS_TEST_TRIAL") == "1"


def run_trial(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "link_metrics", "trial", *arguments, "--root", str(REPOSITORY_ROOT)],
        check=False,
        capture_output=True,
        text=True,
    )


def run_control_plane(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "link_metrics", *arguments, "--root", str(REPOSITORY_ROOT)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_validation_flags_match_dataset_validation_stream() -> None:
    expected = sample_workload(REPOSITORY_ROOT, 1, 500)["samples"]["validation"]
    assert build_validation_flags(REPOSITORY_ROOT, repetition=1, count=500) == expected


def test_smoke_and_trial_durations_are_protocol_constants() -> None:
    assert SMOKE_WARM_SECONDS == 2
    assert SMOKE_MEASURE_SECONDS == 5
    assert SMOKE_OFFERED_RATE == 2
    assert TRIAL_WARM_SECONDS == 30
    assert TRIAL_MEASURE_SECONDS == 60


def test_contender_constraints_record_pool_timeouts_and_no_access_logging() -> None:
    assert CONTENDER_CONSTRAINTS == {
        "maxPoolConnections": 20,
        "connectionTimeoutMillis": 2000,
        "statementTimeoutMillis": 2000,
        "successPathAccessLogging": False,
    }
    server = (REPOSITORY_ROOT / "backends/express-node/src/server.ts").read_text(encoding="utf-8")
    assert "max: 20" in server
    assert "connectionTimeoutMillis: 2_000" in server
    assert "statement_timeout=2000" in server
    assert "morgan" not in server
    assert "Express Contender listening" in server


def test_scenario_configurations_publish_distinct_deterministic_workloads() -> None:
    assert SCENARIO_CONFIGURATIONS == {
        "registration": {
            "authentication": "none",
            "selection": "unique-seeded-registration-identities",
            "bodyValidation": "seeded-one-percent",
            "p99BudgetMs": 1_000,
        },
        "login": {
            "authentication": "seeded-credentials",
            "selection": "seeded-user-stream",
            "bodyValidation": "seeded-one-percent",
            "p99BudgetMs": 1_000,
        },
        "short-link-creation": {
            "authentication": "reference-token-corpus",
            "selection": "all-reference-users-evenly",
            "destinations": "byte-stable-per-iteration",
            "shortCodes": "database-generated",
            "bodyValidation": "seeded-one-percent",
            "p99BudgetMs": 250,
        },
        "statistics": {
            "authentication": "reference-token-corpus",
            "selection": "owned-short-links-evenly-null-and-nonnull",
            "bodyValidation": "seeded-one-percent",
            "p99BudgetMs": 250,
        },
        "uniform-resolution": {
            "authentication": "none",
            "selection": "all-seeded-short-links-evenly",
            "locationValidation": "every-response",
            "p99BudgetMs": 250,
        },
        "viral-resolution": {
            "authentication": "none",
            "selection": "ninety-percent-viral-ten-percent-uniform",
            "locationValidation": "every-response",
            "p99BudgetMs": 250,
        },
    }


def test_parse_k6_summary_captures_errors_and_latency() -> None:
    summary = parse_k6_summary(
        {
            "droppedIterations": 1,
            "httpReqs": 99,
            "checksPassed": 90,
            "checksFailed": 9,
            "unexpectedResponses": 2,
            "transportFailures": 3,
            "latency": {"avgMs": 10.0, "p99Ms": 40.0, "maxMs": 50.0},
        }
    )
    assert summary["droppedIterations"] == 1
    assert summary["errors"]["transportFailures"] == 3
    assert summary["errors"]["unexpectedResponses"] == 2
    assert summary["latency"]["p99Ms"] == 40.0


def test_trial_smoke_requires_an_output_path() -> None:
    result = run_trial("smoke", "express-node")
    assert result.returncode == 2
    assert "output" in result.stderr.lower() or "required" in result.stderr.lower()


def test_trial_run_requires_rate_and_output() -> None:
    result = run_trial("run", "express-node", "--scenario", "registration")
    assert result.returncode == 2


def test_startup_run_requires_an_output_path() -> None:
    result = run_control_plane("startup", "run", "express-node")

    assert result.returncode == 2
    assert "output" in result.stderr.lower() or "required" in result.stderr.lower()


@pytest.mark.parametrize(
    "scenario",
    [
        "registration",
        "login",
        "short-link-creation",
        "uniform-resolution",
        "viral-resolution",
        "statistics",
    ],
)
def test_trial_cli_accepts_every_success_path_scenario(scenario: str) -> None:
    result = run_trial("run", "express-node", "--scenario", scenario)

    assert result.returncode == 2
    assert "invalid choice" not in result.stderr


@pytest.mark.skipif(
    not TRIAL_E2E_ENABLED,
    reason="set LINK_METRICS_TEST_TRIAL=1 for end-to-end registration smoke",
)
def test_registration_smoke_trial_writes_a_nonofficial_bundle(tmp_path: Path) -> None:
    output = tmp_path / "smoke-bundle.json"
    run_control_plane("contenders", "stop", "express-node")

    try:
        started = run_control_plane("contenders", "start", "express-node")
        assert started.returncode == 0, started.stderr
        built = run_control_plane("dataset", "build", "express-node")
        assert built.returncode == 0, built.stderr

        smoke = run_trial("smoke", "express-node", "--output", str(output), "--repetition", "1")
        assert smoke.returncode == 0, smoke.stderr
        bundle = json.loads(smoke.stdout)
        assert output.is_file()
        assert bundle["official"] is False
        assert bundle["mode"] == "smoke"
        assert bundle["environment"]["fingerprint"]["resourceProfile"] is None
        assert bundle["environment"]["fingerprint"]["resourceProfileDefinition"] is None
        assert bundle["scenario"] == "registration"
        assert bundle["lifecycle"]["warmSeconds"] == SMOKE_WARM_SECONDS
        assert bundle["lifecycle"]["measureSeconds"] == SMOKE_MEASURE_SECONDS
        assert bundle["workload"]["offeredRate"] == SMOKE_OFFERED_RATE
        assert bundle["workload"]["password"] == "link-metrics-benchmark-only"
        assert bundle["workload"]["transport"]["protocol"] == "HTTP/1.1"
        assert bundle["workload"]["transport"]["keepAlive"] is True
        assert "droppedIterations" in bundle["results"]
        assert "k6SchedulingHealthy" in bundle["validity"]
        assert bundle["contenderConstraints"]["maxPoolConnections"] == 20
        assert bundle["contenderConstraints"]["successPathAccessLogging"] is False
    finally:
        run_control_plane("contenders", "stop", "express-node")
