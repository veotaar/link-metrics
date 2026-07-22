"""Fast, explicitly non-publishable capacity exploration."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from link_metrics.evidence import write_immutable_json
from link_metrics.progress import ExecutionBudget
from link_metrics.scenarios import P99_BUDGETS_MS
from link_metrics.trial import TRIAL_MODES


DEFAULT_LITE_SCENARIOS = ("short-link-creation", "uniform-resolution")
LITE_INITIAL_RATE = 64.0
LITE_GROWTH_FACTOR = 4.0
LITE_REFINEMENTS = 5


class LiteError(Exception):
    """The exploratory scheduler could not produce a useful result."""


class _LiteBudgetExhausted(Exception):
    """No new exploratory Trial may start within the current budget."""


def default_lite_output_directory(root: Path) -> Path:
    """Return a unique ignored directory for locally inspectable lite evidence."""
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return root.resolve() / "benchmark" / "results" / "lite" / run_id


def _trial_failures(bundle: dict[str, Any]) -> list[str]:
    try:
        offered = float(bundle["results"]["offeredIterations"])
        achieved = int(bundle["results"]["achievedIterations"])
        errors = bundle["results"]["errors"]
        error_count = int(errors["unexpectedResponses"]) + int(
            errors["transportFailures"]
        )
        p99_ms = float(bundle["results"]["latency"]["p99Ms"])
        p99_budget_ms = P99_BUDGETS_MS[str(bundle["scenario"])]
        validity = bundle["validity"]
        failures = [str(reason) for reason in validity.get("reasons", [])]
        if not bool(validity["valid"]) and not failures:
            failures.append("trial_invalid_without_reason")
        if achieved / offered < 0.99:
            failures.append("confirmation_completion_below_99_percent")
        if error_count / offered >= 0.001:
            failures.append("confirmation_error_rate_at_or_above_0_1_percent")
        if p99_ms > p99_budget_ms:
            failures.append("confirmation_p99_budget_exceeded")
        return sorted(set(failures))
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
        raise LiteError("lite Trial bundle is missing valid result fields") from error


def _trial_passes(bundle: dict[str, Any]) -> bool:
    return not _trial_failures(bundle)


def calibrate_lite_capacity(
    measure: Callable[[float], dict[str, Any]],
    *,
    initial_rate: float = LITE_INITIAL_RATE,
) -> dict[str, Any]:
    """Find a coarse passing/failing bracket with a bounded adaptive search."""
    if initial_rate < 1:
        raise LiteError("initial lite calibration rate must be at least one")

    samples: list[dict[str, Any]] = []

    def attempt(rate: float) -> bool:
        normalized_rate = round(rate, 2)
        bundle = measure(normalized_rate)
        passed = _trial_passes(bundle)
        samples.append(
            {"offeredRate": normalized_rate, "passed": passed, "trial": bundle}
        )
        return passed

    candidate = float(initial_rate)
    if attempt(candidate):
        passing_rate = candidate
        failing_rate = candidate * LITE_GROWTH_FACTOR
        while attempt(failing_rate):
            passing_rate = failing_rate
            failing_rate *= LITE_GROWTH_FACTOR
    else:
        failing_rate = candidate
        candidate = max(1.0, candidate / LITE_GROWTH_FACTOR)
        while not attempt(candidate):
            failing_rate = candidate
            if candidate == 1.0:
                raise LiteError("lite calibration failed at one request per second")
            candidate = max(1.0, candidate / LITE_GROWTH_FACTOR)
        passing_rate = candidate

    for _ in range(LITE_REFINEMENTS):
        candidate = round(math.sqrt(passing_rate * failing_rate), 2)
        if candidate in {passing_rate, failing_rate}:
            break
        if attempt(candidate):
            passing_rate = candidate
        else:
            failing_rate = candidate

    return {
        "passingRate": round(passing_rate, 2),
        "failingRate": round(failing_rate, 2),
        "relativeBracketWidth": failing_rate / passing_rate - 1,
        "sampleCount": len(samples),
        "samples": samples,
    }


def _result_row(
    contender: str,
    scenario: str,
    calibration: dict[str, Any],
    confirmation: dict[str, Any],
    trial_artifacts: Sequence[str],
) -> dict[str, Any]:
    try:
        results = confirmation["results"]
        latency = results["latency"]
        errors = results["errors"]
        offered = float(results["offeredIterations"])
        error_count = int(errors["unexpectedResponses"]) + int(
            errors["transportFailures"]
        )
        resources = results.get("resourceTelemetry", {}).get("contender", {})
        warnings = list(confirmation.get("validity", {}).get("warnings", []))
        failures = _trial_failures(confirmation)
        confirmed = not failures
        if not confirmed:
            warnings.append("capacity_confirmation_failed")
            warnings.extend(failures)
        validity = confirmation.get("validity", {})
        k6_cpu = validity.get("k6CpuEvidence", {}).get("peakCpuPercent")
        completion_rate = int(results["achievedIterations"]) / offered
        calibration_samples = calibration["samples"]
        if len(trial_artifacts) != len(calibration_samples) + 1:
            raise LiteError("lite Trial artifact index does not match completed samples")
        return {
            "contender": contender,
            "scenario": scenario,
            "estimatedCapacity": float(calibration["passingRate"]),
            "passingRate": float(calibration["passingRate"]),
            "failingRate": float(calibration["failingRate"]),
            "relativeBracketWidth": float(calibration["relativeBracketWidth"]),
            "confirmed": confirmed,
            "completionRate": completion_rate,
            "droppedIterations": int(results.get("droppedIterations", 0)),
            "validityReasons": [str(reason) for reason in validity.get("reasons", [])],
            "k6PeakCpuPercent": float(k6_cpu) if k6_cpu is not None else None,
            "p50Ms": float(latency["medMs"]),
            "p95Ms": float(latency["p95Ms"]),
            "p99Ms": float(latency["p99Ms"]),
            "errorRate": error_count / offered,
            "peakRamBytes": int(resources.get("peakResidentMemoryBytes", 0)),
            "calibrationSamples": int(calibration["sampleCount"]),
            "calibration": {
                "passingRate": float(calibration["passingRate"]),
                "failingRate": float(calibration["failingRate"]),
                "relativeBracketWidth": float(calibration["relativeBracketWidth"]),
                "samples": [
                    {
                        "offeredRate": float(sample["offeredRate"]),
                        "passed": bool(sample["passed"]),
                        "artifact": trial_artifacts[index],
                    }
                    for index, sample in enumerate(calibration_samples)
                ],
            },
            "confirmationArtifact": trial_artifacts[-1],
            "trialArtifacts": list(trial_artifacts),
            "warnings": sorted(set(str(warning) for warning in warnings)),
        }
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
        raise LiteError("lite confirmation bundle is missing valid result fields") from error


def run_lite_exploration(
    root: Path,
    contenders: Sequence[str],
    *,
    scenarios: Sequence[str] = DEFAULT_LITE_SCENARIOS,
    max_hours: float = 2,
    trial_runner: Callable[..., dict[str, Any]] | None = None,
    template_preparer: Callable[[Path, str], object] | None = None,
    progress: Callable[[str], None] | None = None,
    budget: ExecutionBudget | None = None,
    work_directory: Path | None = None,
) -> dict[str, Any]:
    """Run short calibration and one confirmation per Contender and Scenario."""
    if not contenders:
        raise LiteError("lite mode requires at least one Contender")
    if len(set(contenders)) != len(contenders):
        raise LiteError("Contender identities must be unique")
    if not scenarios:
        raise LiteError("lite mode requires at least one Scenario")
    if len(set(scenarios)) != len(scenarios):
        raise LiteError("Scenarios must be unique")
    unknown = sorted(set(scenarios) - set(P99_BUDGETS_MS))
    if unknown:
        raise LiteError("unknown Scenario: " + ", ".join(unknown))
    if not math.isfinite(max_hours) or max_hours <= 0 or max_hours > 2:
        raise LiteError("lite mode max-hours must be greater than zero and at most two")

    if trial_runner is None:
        from link_metrics.trial import run_scenario_trial

        trial_runner = run_scenario_trial
    if template_preparer is None:
        from link_metrics.dataset_runtime import prepare_template_runtime

        template_preparer = prepare_template_runtime
    if budget is None:
        budget = ExecutionBudget.for_hours(max_hours)
    report_progress = progress or (lambda message: None)

    work_directory = (
        default_lite_output_directory(root)
        if work_directory is None
        else work_directory.resolve()
    )
    summary_path = work_directory / "lite-results.json"
    if summary_path.exists():
        raise LiteError(f"lite result summary already exists: {summary_path}")
    trials_directory = work_directory / "trials"
    trials_directory.mkdir(parents=True, exist_ok=True)
    report_progress(f"Saving lite artifacts to {work_directory}")
    results: list[dict[str, Any]] = []
    completed_trial_artifacts: list[str] = []
    conformed: set[str] = set()
    prepared: set[str] = set()
    sequence = 0

    def run_trial(
        contender: str,
        scenario: str,
        mode: str,
        rate: float,
        *,
        job_index: int,
        total_jobs: int,
        job_trial_artifacts: list[str],
    ) -> dict[str, Any]:
        nonlocal sequence
        progress_prefix = f"({job_index}/{total_jobs})"
        if not budget.can_start():
            raise _LiteBudgetExhausted
        if contender not in prepared:
            report_progress(f"{progress_prefix} Preparing {contender} dataset template")
            template_preparer(root.resolve(), contender)
            prepared.add(contender)
            report_progress(f"{progress_prefix} Prepared {contender} dataset template")
            if not budget.can_start():
                raise _LiteBudgetExhausted
        sequence += 1
        phase = "calibration" if mode == "lite-calibration" else "confirmation"
        rate_label = f"{rate:,.2f}".rstrip("0").rstrip(".")
        trial_label = f"{contender} / {scenario}: {phase} at {rate_label} req/s"
        report_progress(f"{progress_prefix} Running {trial_label}")
        trial_path = trials_directory / f"trial-{sequence:03d}.json"
        bundle = trial_runner(
            root.resolve(),
            contender,
            scenario=scenario,
            output=trial_path,
            mode=mode,
            repetition=1,
            offered_rate=rate,
            pause_database_after=True,
            verify_conformance=contender not in conformed,
        )
        relative_trial_path = str(trial_path.relative_to(work_directory))
        completed_trial_artifacts.append(relative_trial_path)
        job_trial_artifacts.append(relative_trial_path)
        report_progress(f"{progress_prefix} Finished {trial_label}")
        conformed.add(contender)
        budget.record_completed()
        return bundle

    jobs = [(scenario, contender) for scenario in scenarios for contender in contenders]
    total_jobs = len(jobs)
    try:
        for job_index, (scenario, contender) in enumerate(jobs, start=1):
            job_trial_artifacts: list[str] = []
            calibration = calibrate_lite_capacity(
                lambda rate, contender=contender, scenario=scenario: run_trial(
                    contender,
                    scenario,
                    "lite-calibration",
                    rate,
                    job_index=job_index,
                    total_jobs=total_jobs,
                    job_trial_artifacts=job_trial_artifacts,
                )
            )
            confirmation = run_trial(
                contender,
                scenario,
                "lite-measurement",
                float(calibration["passingRate"]),
                job_index=job_index,
                total_jobs=total_jobs,
                job_trial_artifacts=job_trial_artifacts,
            )
            results.append(
                _result_row(
                    contender,
                    scenario,
                    calibration,
                    confirmation,
                    job_trial_artifacts,
                )
            )
    except _LiteBudgetExhausted:
        status = "budget-exhausted"
    except Exception as error:
        failure_path = work_directory / "failure.json"
        failure_document = {
            "schemaVersion": 1,
            "kind": "lite-failure",
            "status": "failed",
            "error": {
                "type": type(error).__name__,
                "message": str(error),
            },
            "completedTrials": budget.completed_units,
            "artifacts": {
                "directory": str(work_directory),
                "failure": str(failure_path),
                "trials": completed_trial_artifacts,
            },
        }
        try:
            write_immutable_json(failure_path, failure_document)
        except (FileExistsError, OSError):
            pass
        raise
    else:
        status = "complete"

    document = {
        "schemaVersion": 1,
        "kind": "lite-results",
        "status": status,
        "publishable": False,
        "configuration": {
            "calibrationWarmSeconds": TRIAL_MODES["lite-calibration"].warm_seconds,
            "calibrationMeasureSeconds": TRIAL_MODES["lite-calibration"].measure_seconds,
            "confirmationWarmSeconds": TRIAL_MODES["lite-measurement"].warm_seconds,
            "confirmationMeasureSeconds": TRIAL_MODES["lite-measurement"].measure_seconds,
            "maximumHours": max_hours,
            "scenarios": list(scenarios),
        },
        "completedTrials": budget.completed_units,
        "results": results,
        "artifacts": {
            "directory": str(work_directory),
            "summary": str(summary_path),
            "trials": completed_trial_artifacts,
        },
    }
    try:
        return write_immutable_json(summary_path, document)
    except FileExistsError as error:
        raise LiteError(f"lite result summary already exists: {summary_path}") from error


def _format_number(value: float) -> str:
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def format_lite_results(document: dict[str, Any]) -> str:
    """Render exploratory results as a compact terminal table."""
    configuration = document["configuration"]
    lines = [
        "Exploratory results — not suitable for publication",
        (
            f"{configuration['calibrationMeasureSeconds']} s calibration samples; "
            f"one {configuration['confirmationMeasureSeconds']} s confirmation; "
            "no repetitions or confidence intervals"
        ),
        "",
    ]
    headers = (
        "Contender",
        "Scenario",
        "Capacity bracket",
        "Confirmed",
        "Completion",
        "Dropped",
        "p50",
        "p95",
        "p99",
        "Errors",
        "Peak RAM",
        "K6 CPU",
    )
    rows = []
    for result in document["results"]:
        rows.append(
            (
                str(result["contender"]),
                str(result["scenario"]),
                (
                    f"{_format_number(float(result.get('passingRate', result['estimatedCapacity'])))}"
                    "–"
                    f"{_format_number(float(result.get('failingRate', result['estimatedCapacity'])))} req/s"
                ),
                "yes" if result["confirmed"] else "no",
                f"{float(result.get('completionRate', 0)) * 100:.2f}%",
                str(int(result.get("droppedIterations", 0))),
                f"{_format_number(float(result['p50Ms']))} ms",
                f"{_format_number(float(result['p95Ms']))} ms",
                f"{_format_number(float(result['p99Ms']))} ms",
                f"{float(result['errorRate']) * 100:.2f}%",
                f"{round(int(result['peakRamBytes']) / (1024 * 1024))} MiB",
                (
                    f"{_format_number(float(result['k6PeakCpuPercent']))}%"
                    if result.get("k6PeakCpuPercent") is not None
                    else "n/a"
                ),
            )
        )
    widths = [
        max([len(headers[index]), *(len(row[index]) for row in rows)])
        for index in range(len(headers))
    ]
    lines.append(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(headers))
    )
    lines.extend(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        for row in rows
    )
    warnings = [
        f"{result['contender']} / {result['scenario']}: {', '.join(result['warnings'])}"
        for result in document["results"]
        if result["warnings"]
    ]
    if warnings:
        lines.extend(["", "Warnings:", *(f"- {warning}" for warning in warnings)])
    if document["status"] != "complete":
        lines.extend(["", "Time budget exhausted; no additional Trial was started."])
    artifact_directory = document.get("artifacts", {}).get("directory")
    if artifact_directory:
        lines.extend(["", f"Artifacts: {artifact_directory}"])
    return "\n".join(lines)
