"""Exploratory lite-mode behavior at its command seam."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from link_metrics import cli
from link_metrics.lite import (
    DEFAULT_LITE_SCENARIOS,
    calibrate_lite_capacity,
    default_lite_output_directory,
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
            "droppedIterations": 0,
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
        "validity": {
            "valid": True,
            "reasons": [],
            "warnings": [],
            "k6CpuEvidence": {"peakCpuPercent": 42.0},
        },
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


def test_lite_calibration_uses_aggressive_probes_and_reaches_five_percent_precision() -> None:
    attempted: list[float] = []

    def measure(rate: float) -> dict[str, Any]:
        attempted.append(rate)
        return lite_bundle("express-node", "short-link-creation", rate, passed=rate <= 2_000)

    calibration = calibrate_lite_capacity(measure)

    assert attempted[:4] == [64.0, 256.0, 1_024.0, 4_096.0]
    assert len(attempted) == 9
    assert calibration["passingRate"] <= 2_000
    assert calibration["failingRate"] > 2_000
    assert calibration["relativeBracketWidth"] <= 0.05
    assert calibration["sampleCount"] == 9


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
        template_preparer=lambda root, contender_id: None,
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
    assert all(row["relativeBracketWidth"] <= 0.05 for row in result["results"])
    assert all(row["completionRate"] == pytest.approx(1.0, rel=0.001) for row in result["results"])
    assert all(row["droppedIterations"] == 0 for row in result["results"])
    assert (tmp_path / "lite-results.json").is_file()


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
        template_preparer=lambda root, contender_id: None,
        budget=ExecutionBudget(maximum_units=2),
        work_directory=tmp_path,
    )

    assert result["status"] == "budget-exhausted"
    assert result["results"] == []
    assert calls == 2


def test_lite_prepares_each_contenders_cached_template_once_before_its_first_trial(
    tmp_path: Path,
) -> None:
    events: list[tuple[str, str]] = []

    def prepare(root: Path, contender_id: str) -> None:
        assert root == REPOSITORY_ROOT
        events.append(("prepare", contender_id))

    def run_trial(
        root: Path,
        contender_id: str,
        *,
        scenario: str,
        mode: str,
        offered_rate: float,
        **kwargs: object,
    ) -> dict[str, Any]:
        del root, kwargs
        events.append(("trial", contender_id))
        return lite_bundle(
            contender_id,
            scenario,
            offered_rate,
            passed=offered_rate <= 100,
            mode=mode,
        )

    run_lite_exploration(
        REPOSITORY_ROOT,
        ["express-node", "elysia-bun"],
        scenarios=["short-link-creation"],
        trial_runner=run_trial,
        template_preparer=prepare,
        work_directory=tmp_path,
    )

    assert [event for event in events if event[0] == "prepare"] == [
        ("prepare", "express-node"),
        ("prepare", "elysia-bun"),
    ]
    for contender_id in ("express-node", "elysia-bun"):
        assert events.index(("prepare", contender_id)) < events.index(("trial", contender_id))


def test_lite_reports_template_and_trial_progress(tmp_path: Path) -> None:
    progress: list[str] = []

    def run_trial(
        root: Path,
        contender_id: str,
        *,
        scenario: str,
        mode: str,
        offered_rate: float,
        **kwargs: object,
    ) -> dict[str, Any]:
        del root, kwargs
        return lite_bundle(
            contender_id,
            scenario,
            offered_rate,
            passed=offered_rate <= 64,
            mode=mode,
        )

    run_lite_exploration(
        REPOSITORY_ROOT,
        ["express-node"],
        scenarios=["short-link-creation"],
        trial_runner=run_trial,
        template_preparer=lambda root, contender_id: None,
        progress=progress.append,
        work_directory=tmp_path,
    )

    assert progress[0] == f"Saving lite artifacts to {tmp_path.resolve()}"
    assert progress[1] == "(1/1) Preparing express-node dataset template"
    assert "(1/1) Running express-node / short-link-creation: calibration at 64 req/s" in progress
    assert "(1/1) Finished express-node / short-link-creation: calibration at 64 req/s" in progress
    assert "(1/1) Running express-node / short-link-creation: confirmation at 64 req/s" in progress
    assert progress[-1] == "(1/1) Finished express-node / short-link-creation: confirmation at 64 req/s"


def test_lite_keeps_raw_trials_and_summary_in_an_ignored_run_directory(
    tmp_path: Path,
) -> None:
    output_directory = tmp_path / "lite-run"

    def run_trial(
        root: Path,
        contender_id: str,
        *,
        scenario: str,
        mode: str,
        offered_rate: float,
        output: Path,
        **kwargs: object,
    ) -> dict[str, Any]:
        del root, kwargs
        bundle = lite_bundle(
            contender_id,
            scenario,
            offered_rate,
            passed=offered_rate <= 64,
            mode=mode,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("persisted trial evidence\n", encoding="utf-8")
        return bundle

    result = run_lite_exploration(
        REPOSITORY_ROOT,
        ["express-node"],
        scenarios=["short-link-creation"],
        trial_runner=run_trial,
        template_preparer=lambda root, contender_id: None,
        work_directory=output_directory,
    )

    assert result["artifacts"]["directory"] == str(output_directory.resolve())
    assert result["artifacts"]["summary"] == str(
        (output_directory / "lite-results.json").resolve()
    )
    assert (output_directory / "lite-results.json").is_file()
    assert sorted((output_directory / "trials").glob("trial-*.json"))
    assert result["results"][0]["trialArtifacts"]
    assert result["results"][0]["calibration"]["samples"]
    assert result["results"][0]["confirmationArtifact"].startswith("trials/trial-")

    default_parent = default_lite_output_directory(REPOSITORY_ROOT).parent
    assert default_parent == REPOSITORY_ROOT / "benchmark" / "results" / "lite"
    gitignore = (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "benchmark/results/lite/" in gitignore


def test_lite_result_explains_why_confirmation_failed(tmp_path: Path) -> None:
    def run_trial(
        root: Path,
        contender_id: str,
        *,
        scenario: str,
        mode: str,
        offered_rate: float,
        **kwargs: object,
    ) -> dict[str, Any]:
        del root, kwargs
        bundle = lite_bundle(
            contender_id,
            scenario,
            offered_rate,
            passed=offered_rate <= 64,
            mode=mode,
        )
        if mode == "lite-measurement":
            bundle["results"]["achievedIterations"] = offered_rate * 45 * 0.98
            bundle["results"]["droppedIterations"] = 12
            bundle["validity"]["valid"] = False
            bundle["validity"]["reasons"] = ["k6_could_not_schedule_offered_rate"]
        return bundle

    result = run_lite_exploration(
        REPOSITORY_ROOT,
        ["express-node"],
        scenarios=["short-link-creation"],
        trial_runner=run_trial,
        template_preparer=lambda root, contender_id: None,
        work_directory=tmp_path,
    )

    row = result["results"][0]
    assert row["confirmed"] is False
    assert row["completionRate"] == pytest.approx(0.98, rel=0.001)
    assert row["droppedIterations"] == 12
    assert row["validityReasons"] == ["k6_could_not_schedule_offered_rate"]
    assert "confirmation_completion_below_99_percent" in row["warnings"]
    assert "k6_could_not_schedule_offered_rate" in row["warnings"]


def test_lite_retains_failure_diagnostics_when_a_trial_aborts(tmp_path: Path) -> None:
    calls = 0

    def run_trial(
        root: Path,
        contender_id: str,
        *,
        scenario: str,
        mode: str,
        offered_rate: float,
        output: Path,
        **kwargs: object,
    ) -> dict[str, Any]:
        nonlocal calls
        del root, kwargs
        calls += 1
        if calls == 2:
            raise RuntimeError("k6 exited with status 137")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("completed trial\n", encoding="utf-8")
        return lite_bundle(
            contender_id,
            scenario,
            offered_rate,
            passed=True,
            mode=mode,
        )

    with pytest.raises(RuntimeError, match="status 137"):
        run_lite_exploration(
            REPOSITORY_ROOT,
            ["express-node"],
            scenarios=["short-link-creation"],
            trial_runner=run_trial,
            template_preparer=lambda root, contender_id: None,
            work_directory=tmp_path,
        )

    failure = json.loads((tmp_path / "failure.json").read_text(encoding="utf-8"))
    assert failure["status"] == "failed"
    assert failure["error"] == {
        "message": "k6 exited with status 137",
        "type": "RuntimeError",
    }
    assert failure["artifacts"]["trials"] == ["trials/trial-001.json"]


def test_lite_cli_streams_progress_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    def run_lite(*args: object, **kwargs: object) -> dict[str, Any]:
        del args
        assert kwargs["work_directory"] == tmp_path.resolve()
        kwargs["progress"]("Running hono-bun / short-link-creation: calibration at 64 req/s")
        return {
            "kind": "lite-results",
            "status": "budget-exhausted",
            "publishable": False,
            "configuration": {
                "calibrationMeasureSeconds": 15,
                "confirmationMeasureSeconds": 45,
            },
            "results": [],
        }

    monkeypatch.setattr(cli, "run_lite_exploration", run_lite)

    assert cli.main(
        [
            "lite",
            "run",
            "hono-bun",
            "--scenario",
            "short-link-creation",
            "--root",
            str(REPOSITORY_ROOT),
            "--output",
            str(tmp_path),
        ]
    ) == 0

    captured = capsys.readouterr()
    assert "Running hono-bun / short-link-creation" in captured.err
    assert "Exploratory results" in captured.out


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
                    "passingRate": 100.0,
                    "failingRate": 104.0,
                    "relativeBracketWidth": 0.04,
                    "confirmed": True,
                    "completionRate": 1.0,
                    "droppedIterations": 0,
                    "k6PeakCpuPercent": 42.0,
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
    assert "100–104 req/s" in output
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
            "artifacts": {"directory": "/tmp/lite-results"},
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
    assert "--output" in result.stdout
    assert "short-link-creation" in result.stdout
    assert "uniform-resolution" in result.stdout
