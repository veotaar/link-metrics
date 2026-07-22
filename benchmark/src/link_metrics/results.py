"""Capacity discovery, qualification, and deterministic result reporting."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import platform
import random
import statistics
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from link_metrics.dataset import (
    REFERENCE_TOKEN_MIN_VALIDITY_SECONDS,
    ReferenceTokenCorpus,
    build_reference_token_corpus,
    describe_dataset,
)
from link_metrics.evidence import write_immutable_json
from link_metrics.progress import ExecutionBudget
from link_metrics.scenarios import P99_BUDGETS_MS, PROTECTED_SCENARIOS


TARGET_PERCENTAGES = (25, 50, 75, 90, 100, 110)


class ResultError(Exception):
    """Raw results cannot be calibrated, combined, qualified, or reported."""


class _CapacityPaused(Exception):
    """No new Trial may start within the current execution budget."""


def _load_resumable_trial(
    path: Path,
    *,
    contender: str,
    scenario: str,
    mode: str,
    repetition: int,
    offered_rate: float,
    expected_provenance: dict[str, Any],
) -> dict[str, Any]:
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
        matches = (
            isinstance(bundle, dict)
            and bundle.get("contender", {}).get("id") == contender
            and bundle.get("scenario") == scenario
            and bundle.get("mode") == mode
            and int(bundle.get("repetition")) == repetition
            and float(bundle.get("workload", {}).get("offeredRate")) == offered_rate
            and bool(bundle.get("official")) is (mode == "trial")
            and all(bundle.get(key) == value for key, value in expected_provenance.items())
            and all(
                bundle.get("environment", {}).get("fingerprint", {}).get(key) == value
                for key, value in _current_host_identity().items()
            )
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        matches = False
        bundle = None
    if not matches or not isinstance(bundle, dict):
        raise ResultError(f"existing resumable Trial does not match its schedule: {path}")
    return bundle


def _repository_commit(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ResultError("cannot determine the repository commit for resumption")
    return result.stdout.strip()


def _current_host_identity() -> dict[str, Any]:
    return {
        "hostname": platform.node(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpus": os.cpu_count(),
        "memoryBytes": os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"),
    }


def _load_completed_series(
    output: Path, contenders: Sequence[str], scenario: str
) -> dict[str, Any]:
    try:
        document = json.loads(output.read_text(encoding="utf-8"))
        measured_contenders = {
            str(bundle["contender"]["id"]) for bundle in document["measurements"]
        }
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        document = None
        measured_contenders = set()
    if (
        not isinstance(document, dict)
        or document.get("kind") != "result-series"
        or document.get("scenario") != scenario
        or measured_contenders != set(contenders)
    ):
        raise ResultError(f"result bundle already exists with incompatible evidence: {output}")
    return document


def _trial_sample(bundle: dict[str, Any]) -> dict[str, Any]:
    try:
        offered_iterations = float(bundle["results"]["offeredIterations"])
        achieved_iterations = int(bundle["results"]["achievedIterations"])
        measure_seconds = int(bundle["lifecycle"]["measureSeconds"])
        errors = bundle["results"]["errors"]
        error_count = int(errors["unexpectedResponses"]) + int(errors["transportFailures"])
        sample = {
            "repetition": int(bundle["repetition"]),
            "workloadSeed": int(bundle["workloadSeed"]),
            "offeredRate": float(bundle["workload"]["offeredRate"]),
            "achievedThroughput": achieved_iterations / measure_seconds,
            "completionRate": achieved_iterations / offered_iterations,
            "errorRate": error_count / offered_iterations,
            "p99Ms": float(bundle["results"]["latency"]["p99Ms"]),
            "valid": bool(bundle["validity"]["valid"]),
            "validityReasons": list(bundle["validity"].get("reasons", [])),
        }
        for optional_evidence in (
            "resourceTelemetry",
            "postgresTelemetry",
            "runtimeDiagnostics",
        ):
            if optional_evidence in bundle["results"]:
                sample[optional_evidence] = bundle["results"][optional_evidence]
        if "execution" in bundle.get("environment", {}):
            sample["hostExecution"] = bundle["environment"]["execution"]
        return sample
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
        raise ResultError("raw Trial bundle is missing valid result fields") from error


def _trial_passes(bundle: dict[str, Any]) -> bool:
    sample = _trial_sample(bundle)
    scenario = bundle.get("scenario")
    try:
        budget = P99_BUDGETS_MS[str(scenario)]
    except KeyError as error:
        raise ResultError(f"unknown Scenario: {scenario}") from error
    return (
        sample["valid"]
        and sample["completionRate"] >= 0.99
        and sample["errorRate"] < 0.001
        and sample["p99Ms"] <= budget
    )


def calibrate_boundary(
    measure: Callable[[float], dict[str, Any]],
    *,
    initial_rate: float = 1,
) -> dict[str, Any]:
    """Discover the highest passing rate behind a five-percent bracket."""
    if initial_rate < 1:
        raise ResultError("initial calibration rate must be positive")

    samples: list[dict[str, Any]] = []

    def attempt(rate: float) -> bool:
        bundle = measure(rate)
        samples.append(
            {
                "offeredRate": rate,
                "passed": _trial_passes(bundle),
                "trial": bundle,
            }
        )
        return bool(samples[-1]["passed"])

    passing_rate = 0
    failing_rate = initial_rate
    while attempt(failing_rate):
        passing_rate = failing_rate
        failing_rate *= 2

    if passing_rate == 0:
        raise ResultError(f"calibration failed at the minimum offered rate {initial_rate}")

    while (failing_rate - passing_rate) / passing_rate > 0.05:
        candidate = round((passing_rate + failing_rate) / 2, 2)
        if candidate in {passing_rate, failing_rate}:
            raise ResultError("calibration rate precision cannot close the five-percent bracket")
        if attempt(candidate):
            passing_rate = candidate
        else:
            failing_rate = candidate

    return {
        "passingRate": passing_rate,
        "failingRate": failing_rate,
        "relativeBracketWidth": (failing_rate - passing_rate) / passing_rate,
        "samples": samples,
    }


def _target_rate(boundary: float, percentage: int) -> float:
    return max(0.01, round(boundary * percentage / 100, 2))


def measurement_plan(
    contenders: Sequence[str],
    boundaries: dict[str, float],
    repetition_seeds: Sequence[int],
    *,
    scenario: str,
) -> list[dict[str, Any]]:
    """Return the deterministic official schedule with randomized Contender order."""
    if len(repetition_seeds) != 5:
        raise ResultError("official measurements require exactly five repetition seeds")
    if not contenders:
        raise ResultError("at least one Contender is required")
    if set(contenders) != set(boundaries):
        raise ResultError("every Contender requires one calibrated boundary")

    plan: list[dict[str, Any]] = []
    for target_percentage in TARGET_PERCENTAGES:
        for repetition, workload_seed in enumerate(repetition_seeds, start=1):
            order = list(contenders)
            seed_material = f"{scenario}:{target_percentage}:{workload_seed}".encode()
            shuffle_seed = int.from_bytes(hashlib.sha256(seed_material).digest())
            random.Random(shuffle_seed).shuffle(order)
            plan.append(
                {
                    "targetPercent": target_percentage,
                    "repetition": repetition,
                    "workloadSeed": int(workload_seed),
                    "contenders": order,
                    "offeredRates": {
                        contender: _target_rate(boundaries[contender], target_percentage)
                        for contender in order
                    },
                }
            )
    return plan


def run_capacity_sweep(
    root: Path,
    contenders: Sequence[str],
    *,
    scenario: str,
    output: Path,
    trial_runner: Callable[..., dict[str, Any]] | None = None,
    budget: ExecutionBudget | None = None,
) -> dict[str, Any]:
    """Calibrate Contenders, run the official schedule, and write raw evidence."""
    if output.exists():
        return _load_completed_series(output, contenders, scenario)
    if scenario not in P99_BUDGETS_MS:
        raise ResultError(f"unknown Scenario: {scenario}")
    if len(set(contenders)) != len(contenders):
        raise ResultError("Contender identities must be unique")
    if trial_runner is None:
        from link_metrics.trial import run_scenario_trial

        trial_runner = run_scenario_trial
    if budget is None:
        budget = ExecutionBudget()

    root = root.resolve()
    manifest = describe_dataset(root)
    expected_provenance = {
        "gitCommit": _repository_commit(root),
        "protocolVersion": (root / "benchmark" / "protocol" / "VERSION")
        .read_text(encoding="utf-8")
        .strip(),
        "datasetVersion": str(manifest["version"]),
    }
    repetition_seeds = [int(seed) for seed in manifest["repetitionSeeds"]]
    trial_directory = output.parent / f"{output.stem}.trials"
    reference_tokens: ReferenceTokenCorpus | None = None

    def fresh_reference_tokens(repetition: int) -> ReferenceTokenCorpus | None:
        nonlocal reference_tokens
        if scenario not in PROTECTED_SCENARIOS:
            return None
        now = int(time.time())
        if (
            reference_tokens is None
            or reference_tokens.evidence["repetition"] != repetition
            or reference_tokens.evidence["expiresAt"]
            <= now + REFERENCE_TOKEN_MIN_VALIDITY_SECONDS
        ):
            reference_tokens = build_reference_token_corpus(root, repetition, now)
        return reference_tokens

    calibrations: dict[str, Any] = {}
    boundaries: dict[str, float] = {}
    all_trials: list[dict[str, Any]] = []

    def pause_progress() -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "kind": "capacity-progress",
            "status": "paused",
            "scenario": scenario,
            "contenders": list(contenders),
            "completedTrials": len(all_trials),
            "newTrialsThisSession": budget.completed_units,
            "output": str(output),
        }

    def run_or_resume(
        contender: str,
        *,
        path: Path,
        mode: str,
        repetition: int,
        rate: float,
    ) -> dict[str, Any]:
        if path.exists():
            trial = _load_resumable_trial(
                path,
                contender=contender,
                scenario=scenario,
                mode=mode,
                repetition=repetition,
                offered_rate=rate,
                expected_provenance=expected_provenance,
            )
        else:
            if not budget.can_start():
                raise _CapacityPaused
            trial = trial_runner(
                root,
                contender,
                scenario=scenario,
                output=path,
                mode=mode,
                repetition=repetition,
                offered_rate=rate,
                reference_tokens=fresh_reference_tokens(repetition),
                pause_database_after=True,
            )
            budget.record_completed()
        all_trials.append(trial)
        return trial

    for contender in contenders:
        attempt_number = 0

        def measure(rate: float) -> dict[str, Any]:
            nonlocal attempt_number
            attempt_number += 1
            trial = run_or_resume(
                contender,
                path=trial_directory
                / f"calibration-{contender}-{attempt_number:02d}-{rate}.json",
                mode="calibration",
                repetition=1,
                rate=rate,
            )
            return trial

        try:
            calibration = calibrate_boundary(measure)
        except _CapacityPaused:
            return pause_progress()
        calibrations[contender] = calibration
        boundaries[contender] = float(calibration["passingRate"])

    plan = measurement_plan(
        contenders,
        boundaries,
        repetition_seeds,
        scenario=scenario,
    )
    measurements: list[dict[str, Any]] = []
    for scheduled in plan:
        for contender in scheduled["contenders"]:
            rate = float(scheduled["offeredRates"][contender])
            path = trial_directory / (
                    f"measurement-{scheduled['targetPercent']:03d}-"
                    f"{scheduled['repetition']}-{contender}-{rate:g}.json"
                )
            try:
                trial = run_or_resume(
                    contender,
                    path=path,
                    mode="trial",
                    repetition=int(scheduled["repetition"]),
                    rate=rate,
                )
            except _CapacityPaused:
                return pause_progress()
            measurement = json.loads(json.dumps(trial))
            measurement["targetPercent"] = int(scheduled["targetPercent"])
            measurements.append(measurement)

    key = _enforce_comparability(all_trials)
    summarize_trial_bundles(
        measurements,
        expected_repetition_seeds=repetition_seeds,
    )
    raw_series = {
        "schemaVersion": 1,
        "kind": "result-series",
        "scenario": scenario,
        "comparabilityKey": key,
        "repetitionSeeds": repetition_seeds,
        "calibration": calibrations,
        "measurementPlan": plan,
        "measurements": measurements,
    }
    try:
        write_immutable_json(output, raw_series)
    except FileExistsError as error:
        raise ResultError(f"result bundle already exists: {output}") from error
    return raw_series


def comparability_key(bundle: dict[str, Any]) -> dict[str, Any]:
    """Extract the enforced Result Series identity from one raw Trial."""
    try:
        return {
            "apiContractVersion": str(bundle["apiContractVersion"]),
            "protocolVersion": str(bundle["protocolVersion"]),
            "datasetVersion": str(bundle["datasetVersion"]),
            "environmentFingerprint": bundle["environment"]["fingerprint"],
        }
    except (KeyError, TypeError) as error:
        raise ResultError("raw Trial bundle is missing its comparability key") from error


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _enforce_comparability(bundles: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not bundles:
        raise ResultError("at least one raw Trial bundle is required")
    expected = comparability_key(bundles[0])
    expected_json = _canonical_json(expected)
    for bundle in bundles[1:]:
        if _canonical_json(comparability_key(bundle)) != expected_json:
            raise ResultError("raw Trial bundles have unlike comparability keys")
    return expected


def percentile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _metric_statistics(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        raise ResultError("cannot summarize an empty sample")
    bootstrap_medians = [
        statistics.median(sample)
        for sample in itertools.product(values, repeat=len(values))
    ]
    mean = statistics.mean(values)
    coefficient = statistics.stdev(values) / mean if len(values) > 1 and mean else 0.0
    return {
        "median": statistics.median(values),
        "confidenceInterval95": {
            "lower": percentile(bootstrap_medians, 0.025),
            "upper": percentile(bootstrap_medians, 0.975),
        },
        "coefficientOfVariation": coefficient,
        "unstable": coefficient > 0.05,
    }


def _rate_summary(
    bundles: Sequence[dict[str, Any]],
    *,
    scenario: str,
    offered_rate: float,
) -> dict[str, Any]:
    if len(bundles) > 5:
        raise ResultError(
            f"duplicate official Trials for {scenario} at {offered_rate} requests/s"
        )
    samples = [_trial_sample(bundle) for bundle in bundles]
    statistics_by_metric = {
        metric: _metric_statistics([float(sample[metric]) for sample in samples])
        for metric in ("achievedThroughput", "completionRate", "errorRate", "p99Ms")
    }
    failures: list[str] = []
    repetitions = [sample["repetition"] for sample in samples]
    workload_seeds = [sample["workloadSeed"] for sample in samples]
    if (
        len(samples) != 5
        or sorted(repetitions) != [1, 2, 3, 4, 5]
        or len(set(workload_seeds)) != 5
    ):
        failures.append("not_five_independent_trials")
    if any(not sample["valid"] for sample in samples):
        failures.append("invalid_trial")
    if any(sample["completionRate"] < 0.99 for sample in samples):
        failures.append("completion_below_99_percent")
    if any(sample["errorRate"] >= 0.001 for sample in samples):
        failures.append("errors_not_below_0_1_percent")
    budget = P99_BUDGETS_MS[scenario]
    if any(sample["p99Ms"] > budget for sample in samples):
        failures.append("p99_budget_exceeded")
    unstable = any(values["unstable"] for values in statistics_by_metric.values())
    if unstable:
        failures.append("unstable")
    return {
        "offeredRate": offered_rate,
        "targetPercent": bundles[0].get("targetPercent"),
        "p99BudgetMs": budget,
        "samples": samples,
        "statistics": statistics_by_metric,
        "unstable": unstable,
        "qualified": not failures,
        "qualificationFailures": failures,
    }


def summarize_trial_bundles(
    bundles: Sequence[dict[str, Any]],
    *,
    expected_repetition_seeds: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Merge comparable raw Trials into compact per-Scenario summaries."""
    key = _enforce_comparability(bundles)
    if expected_repetition_seeds is None:
        try:
            published_seed_sets = [
                [int(seed) for seed in bundle["repetitionSeeds"]] for bundle in bundles
            ]
        except (KeyError, TypeError, ValueError) as error:
            raise ResultError("raw Trial does not publish its repetition seeds") from error
        expected_repetition_seeds = published_seed_sets[0]
        if any(seeds != list(expected_repetition_seeds) for seeds in published_seed_sets[1:]):
            raise ResultError("raw Trials publish different repetition seeds")
    if expected_repetition_seeds is not None:
        if len(expected_repetition_seeds) != 5:
            raise ResultError("a Result Series must publish exactly five repetition seeds")
        for bundle in bundles:
            try:
                repetition = int(bundle["repetition"])
                actual_seed = int(bundle["workloadSeed"])
                expected_seed = int(expected_repetition_seeds[repetition - 1])
            except (IndexError, KeyError, TypeError, ValueError) as error:
                raise ResultError("raw Trial has an invalid repetition seed") from error
            if not 1 <= repetition <= 5 or actual_seed != expected_seed:
                raise ResultError("raw Trial does not use the published repetition seed")
    grouped: dict[tuple[str, str, int, float], list[dict[str, Any]]] = {}
    for bundle in bundles:
        if not bundle.get("official"):
            raise ResultError("nonofficial Trial bundles cannot enter a Result Series")
        scenario = str(bundle.get("scenario"))
        if scenario not in P99_BUDGETS_MS:
            raise ResultError(f"unknown Scenario: {scenario}")
        try:
            contender = str(bundle["contender"]["id"])
            offered_rate = float(bundle["workload"]["offeredRate"])
            target_percent = int(bundle.get("targetPercent", -1))
        except (KeyError, TypeError, ValueError) as error:
            raise ResultError("raw Trial bundle is missing grouping fields") from error
        grouped.setdefault((scenario, contender, target_percent, offered_rate), []).append(
            bundle
        )

    scenarios: list[dict[str, Any]] = []
    for scenario in sorted({item[0] for item in grouped}):
        contenders: list[dict[str, Any]] = []
        for contender in sorted({item[1] for item in grouped if item[0] == scenario}):
            rates = [
                _rate_summary(
                    sorted(group, key=lambda bundle: int(bundle["repetition"])),
                    scenario=scenario,
                    offered_rate=offered_rate,
                )
                for (
                    group_scenario,
                    group_contender,
                    _target_percent,
                    offered_rate,
                ), group in sorted(
                    grouped.items()
                )
                if group_scenario == scenario and group_contender == contender
            ]
            qualified_rates = [rate["offeredRate"] for rate in rates if rate["qualified"]]
            contenders.append(
                {
                    "id": contender,
                    "rates": rates,
                    "maximumSustainableThroughput": (
                        max(qualified_rates) if qualified_rates else None
                    ),
                }
            )
        scenarios.append({"id": scenario, "contenders": contenders})
    return {
        "schemaVersion": 1,
        "kind": "result-summary",
        "comparabilityKey": key,
        "scenarios": scenarios,
    }
