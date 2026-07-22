"""Fast, explicitly non-publishable capacity exploration."""

from __future__ import annotations

import math
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from link_metrics.progress import ExecutionBudget
from link_metrics.scenarios import P99_BUDGETS_MS
from link_metrics.trial import TRIAL_MODES


DEFAULT_LITE_SCENARIOS = ("short-link-creation", "uniform-resolution")
LITE_INITIAL_RATE = 64.0
LITE_GROWTH_FACTOR = 4.0
LITE_REFINEMENTS = 3


class LiteError(Exception):
    """The exploratory scheduler could not produce a useful result."""


class _LiteBudgetExhausted(Exception):
    """No new exploratory Trial may start within the current budget."""


def _trial_passes(bundle: dict[str, Any]) -> bool:
    try:
        offered = float(bundle["results"]["offeredIterations"])
        achieved = int(bundle["results"]["achievedIterations"])
        errors = bundle["results"]["errors"]
        error_count = int(errors["unexpectedResponses"]) + int(
            errors["transportFailures"]
        )
        p99_ms = float(bundle["results"]["latency"]["p99Ms"])
        p99_budget_ms = P99_BUDGETS_MS[str(bundle["scenario"])]
        valid = bool(bundle["validity"]["valid"])
        return (
            valid
            and achieved / offered >= 0.99
            and error_count / offered < 0.001
            and p99_ms <= p99_budget_ms
        )
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
        raise LiteError("lite Trial bundle is missing valid result fields") from error


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
        "sampleCount": len(samples),
        "samples": samples,
    }


def _result_row(
    contender: str,
    scenario: str,
    calibration: dict[str, Any],
    confirmation: dict[str, Any],
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
        confirmed = _trial_passes(confirmation)
        if not confirmed:
            warnings.append("capacity_confirmation_failed")
        return {
            "contender": contender,
            "scenario": scenario,
            "estimatedCapacity": float(calibration["passingRate"]),
            "confirmed": confirmed,
            "p50Ms": float(latency["medMs"]),
            "p95Ms": float(latency["p95Ms"]),
            "p99Ms": float(latency["p99Ms"]),
            "errorRate": error_count / offered,
            "peakRamBytes": int(resources.get("peakResidentMemoryBytes", 0)),
            "calibrationSamples": int(calibration["sampleCount"]),
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
    if budget is None:
        budget = ExecutionBudget.for_hours(max_hours)

    if work_directory is None:
        with tempfile.TemporaryDirectory(prefix="link-metrics-lite-") as temporary:
            return run_lite_exploration(
                root,
                contenders,
                scenarios=scenarios,
                max_hours=max_hours,
                trial_runner=trial_runner,
                budget=budget,
                work_directory=Path(temporary),
            )

    work_directory.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    conformed: set[str] = set()
    sequence = 0

    def run_trial(contender: str, scenario: str, mode: str, rate: float) -> dict[str, Any]:
        nonlocal sequence
        if not budget.can_start():
            raise _LiteBudgetExhausted
        sequence += 1
        bundle = trial_runner(
            root.resolve(),
            contender,
            scenario=scenario,
            output=work_directory / f"trial-{sequence:03d}.json",
            mode=mode,
            repetition=1,
            offered_rate=rate,
            pause_database_after=True,
            verify_conformance=contender not in conformed,
        )
        conformed.add(contender)
        budget.record_completed()
        return bundle

    try:
        for scenario in scenarios:
            for contender in contenders:
                calibration = calibrate_lite_capacity(
                    lambda rate, contender=contender, scenario=scenario: run_trial(
                        contender, scenario, "lite-calibration", rate
                    )
                )
                confirmation = run_trial(
                    contender,
                    scenario,
                    "lite-measurement",
                    float(calibration["passingRate"]),
                )
                results.append(
                    _result_row(contender, scenario, calibration, confirmation)
                )
    except _LiteBudgetExhausted:
        status = "budget-exhausted"
    else:
        status = "complete"

    return {
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
    }


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
        "Est. capacity",
        "Confirmed",
        "p50",
        "p95",
        "p99",
        "Errors",
        "Peak RAM",
    )
    rows = []
    for result in document["results"]:
        rows.append(
            (
                str(result["contender"]),
                str(result["scenario"]),
                f"{_format_number(float(result['estimatedCapacity']))} req/s",
                "yes" if result["confirmed"] else "no",
                f"{_format_number(float(result['p50Ms']))} ms",
                f"{_format_number(float(result['p95Ms']))} ms",
                f"{_format_number(float(result['p99Ms']))} ms",
                f"{float(result['errorRate']) * 100:.2f}%",
                f"{round(int(result['peakRamBytes']) / (1024 * 1024))} MiB",
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
    return "\n".join(lines)
