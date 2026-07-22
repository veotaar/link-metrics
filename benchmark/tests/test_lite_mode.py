"""Exploratory lite-mode behavior at its command seam."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from link_metrics.lite import (
    DEFAULT_LITE_SCENARIOS,
    calibrate_lite_capacity,
    format_lite_results,
    run_lite_exploration,
)
from link_metrics.progress import ExecutionBudget
from link_metrics.trial import TRIAL_MODES


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def lite_bundle(
    contender: str,
    scenario: str,
    rate: float,
    *,
    passed: bool,
    mode: str = "lite-calibration",
) -> dict[str, Any]:
    measure_seconds = TRIAL_MODES[mode].measure_seconds
    offered_iterations = rate * measure_seconds
    achieved_iterations = round(offered_iterations if passed else offered_iterations * 0.9)
    return {
        "official": False,
        "publishable": False,
        "mode": mode,
        "scenario": scenario,
        "contender": {"id": contender},
        "lifecycle": {"measureSeconds": measure_seconds},
        "workload": {"offeredRate": rate},
        "results": {
            "offeredIterations": offered_iterations,
            "achievedIterations": achieved_iterations,
            "latency": {
                "medMs": 4.0,
                "p95Ms": 10.0,
                "p99Ms": 20.0 if passed else 300.0,
            },
            "errors": {"unexpectedResponses": 0, "transportFailures": 0},
            "resourceTelemetry": {
                "contender": {"peakResidentMemoryBytes": 256 * 1024 * 1024}
            },
        },
        "validity": {"valid": True, "reasons": [], "warnings": []},
    }


def test_lite_trial_modes_are_short_nonofficial_and_resource_constrained() -> None:
    calibration = TRIAL_MODES["lite-calibration"]
    measurement = TRIAL_MODES["lite-measurement"]

    assert (calibration.warm_seconds, calibration.measure_seconds) == (5, 15)
    assert (measurement.warm_seconds, measurement.measure_seconds) == (10, 45)
    assert calibration.official is measurement.official is False
    assert calibration.uses_official_resource_profile is True
    assert measurement.uses_official_resource_profile is True
    assert calibration.requires_stable_host is False
    assert measurement.requires_stable_host is False


def test_lite_calibration_uses_aggressive_probes_and_three_refinements() -> None:
    attempted: list[float] = []

    def measure(rate: float) -> dict[str, Any]:
        attempted.append(rate)
        return lite_bundle("express-node", "short-link-creation", rate, passed=rate <= 2_000)

    calibration = calibrate_lite_capacity(measure)

    assert attempted[:4] == [64.0, 256.0, 1_024.0, 4_096.0]
    assert len(attempted) == 7
    assert calibration["passingRate"] <= 2_000
    assert calibration["failingRate"] > 2_000
    assert calibration["sampleCount"] == 7


def test_lite_exploration_conforms_each_contender_once_and_confirms_each_scenario(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str, str, bool]] = []

    def run_trial(
        root: Path,
        contender_id: str,
        *,
        scenario: str,
        output: Path,
        mode: str,
        offered_rate: float,
        repetition: int,
        reference_tokens: object | None = None,
        pause_database_after: bool = False,
        verify_conformance: bool = False,
    ) -> dict[str, Any]:
        del root, output, repetition, reference_tokens
        assert pause_database_after is True
        calls.append((contender_id, scenario, mode, verify_conformance))
        return lite_bundle(
            contender_id,
            scenario,
            offered_rate,
            passed=offered_rate <= 100,
            mode=mode,
        )

    result = run_lite_exploration(
        REPOSITORY_ROOT,
        ["express-node", "hono-bun"],
        scenarios=DEFAULT_LITE_SCENARIOS,
        trial_runner=run_trial,
        work_directory=tmp_path,
    )

    assert result["kind"] == "lite-results"
    assert result["status"] == "complete"
    assert result["publishable"] is False
    assert len(result["results"]) == 4
    assert sum(call[3] for call in calls if call[0] == "express-node") == 1
    assert sum(call[3] for call in calls if call[0] == "hono-bun") == 1
    assert all(row["confirmed"] for row in result["results"])
    assert all(row["peakRamBytes"] == 256 * 1024 * 1024 for row in result["results"])
    assert all(row["calibrationSamples"] >= 1 for row in result["results"])


def test_lite_exploration_stops_starting_trials_when_budget_expires(tmp_path: Path) -> None:
    calls = 0

    def run_trial(*args: object, **kwargs: object) -> dict[str, Any]:
        nonlocal calls
        del args
        calls += 1
        return lite_bundle(
            str(kwargs.get("contender_id", "express-node")),
            str(kwargs["scenario"]),
            float(kwargs["offered_rate"]),
            passed=True,
            mode=str(kwargs["mode"]),
        )

    result = run_lite_exploration(
        REPOSITORY_ROOT,
        ["express-node"],
        scenarios=["short-link-creation"],
        trial_runner=run_trial,
        budget=ExecutionBudget(maximum_units=2),
        work_directory=tmp_path,
    )

    assert result["status"] == "budget-exhausted"
    assert result["results"] == []
    assert calls == 2


def test_lite_terminal_output_is_prominently_nonpublishable() -> None:
    output = format_lite_results(
        {
            "kind": "lite-results",
            "status": "complete",
            "publishable": False,
            "configuration": {
                "calibrationMeasureSeconds": 15,
                "confirmationMeasureSeconds": 45,
            },
            "results": [
                {
                    "contender": "express-node",
                    "scenario": "short-link-creation",
                    "estimatedCapacity": 100.0,
                    "confirmed": True,
                    "p50Ms": 4.0,
                    "p95Ms": 10.0,
                    "p99Ms": 20.0,
                    "errorRate": 0.0,
                    "peakRamBytes": 256 * 1024 * 1024,
                    "warnings": [],
                }
            ],
        }
    )

    assert output.startswith("Exploratory results — not suitable for publication")
    assert "express-node" in output
    assert "short-link-creation" in output
    assert "100 req/s" in output
    assert "256 MiB" in output


def test_lite_terminal_output_handles_a_budget_expiring_before_any_result() -> None:
    output = format_lite_results(
        {
            "kind": "lite-results",
            "status": "budget-exhausted",
            "publishable": False,
            "configuration": {
                "calibrationMeasureSeconds": 15,
                "confirmationMeasureSeconds": 45,
            },
            "results": [],
        }
    )

    assert "Time budget exhausted" in output
    assert "Contender" in output


def test_lite_cli_exposes_repeatable_scenarios_and_two_hour_default() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "link_metrics", "lite", "run", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "--scenario" in result.stdout
    assert "--max-hours" in result.stdout
    assert "short-link-creation" in result.stdout
    assert "uniform-resolution" in result.stdout
