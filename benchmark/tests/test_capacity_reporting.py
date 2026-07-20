"""Capacity and reporting behavior at the raw-result seam."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from link_metrics.reporting import write_reports
from link_metrics.results import (
    ResultError,
    calibrate_boundary,
    measurement_plan,
    run_capacity_sweep,
    summarize_trial_bundles,
)


SEEDS = [
    1_350_403_001_542_084_573,
    16_626_817_107_421_360_574,
    1_288_854_886_252_412_864,
    16_145_191_919_997_344_020,
    12_322_208_812_885_659_100,
]


def trial_bundle(
    repetition: int,
    *,
    contender: str = "express-node",
    offered_rate: float = 100,
    achieved_iterations: int = 6_000,
    unexpected_responses: int = 0,
    transport_failures: int = 0,
    p99_ms: float = 900.0,
    valid: bool = True,
    environment: str = "local-7800x3d-v1",
) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "official": True,
        "mode": "trial",
        "apiContractVersion": "1.0.1",
        "protocolVersion": "1.0.0",
        "datasetVersion": "1.2.0",
        "repetitionSeeds": SEEDS,
        "scenario": "registration",
        "repetition": repetition,
        "workloadSeed": SEEDS[repetition - 1],
        "contender": {"id": contender},
        "environment": {"fingerprint": {"profileVersion": environment}},
        "lifecycle": {"measureSeconds": 60},
        "workload": {"offeredRate": offered_rate},
        "results": {
            "offeredIterations": offered_rate * 60,
            "achievedIterations": achieved_iterations,
            "latency": {"p99Ms": p99_ms},
            "errors": {
                "unexpectedResponses": unexpected_responses,
                "transportFailures": transport_failures,
            },
        },
        "validity": {"valid": valid, "reasons": [] if valid else ["invalid"]},
    }


def run_control_plane(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "link_metrics", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def test_calibration_doubles_then_binary_searches_to_a_five_percent_bracket() -> None:
    attempted_rates: list[int] = []

    def measure(rate: int) -> dict[str, Any]:
        attempted_rates.append(rate)
        return trial_bundle(
            1,
            offered_rate=rate,
            achieved_iterations=rate * 60,
            p99_ms=900 if rate <= 100 else 1_001,
        )

    calibration = calibrate_boundary(measure)

    assert attempted_rates[:8] == [1, 2, 4, 8, 16, 32, 64, 128]
    assert calibration["passingRate"] == 100
    assert calibration["failingRate"] > calibration["passingRate"]
    assert calibration["relativeBracketWidth"] <= 0.05
    assert [sample["offeredRate"] for sample in calibration["samples"]] == attempted_rates


def test_calibration_keeps_five_percent_precision_for_low_capacity_scenarios() -> None:
    def measure(rate: float) -> dict[str, Any]:
        return trial_bundle(
            1,
            offered_rate=rate,
            achieved_iterations=round(rate * 60),
            p99_ms=900 if rate <= 10 else 1_001,
        )

    calibration = calibrate_boundary(measure)

    assert calibration["passingRate"] == 10
    assert calibration["relativeBracketWidth"] <= 0.05


def test_measurement_plan_uses_all_seeds_and_randomizes_contender_order() -> None:
    contenders = ["express-node", "hono-bun", "nest-node"]
    boundaries = {contender: 100 for contender in contenders}

    first = measurement_plan(contenders, boundaries, SEEDS, scenario="registration")
    second = measurement_plan(contenders, boundaries, SEEDS, scenario="registration")

    assert first == second
    assert {item["targetPercent"] for item in first} == {25, 50, 75, 90, 100, 110}
    assert len(first) == 30
    assert all(sorted(item["contenders"]) == sorted(contenders) for item in first)
    assert {tuple(item["contenders"]) for item in first} != {tuple(contenders)}
    assert [item["workloadSeed"] for item in first[:5]] == SEEDS


def test_measurement_plan_preserves_sub_one_request_targets() -> None:
    plan = measurement_plan(
        ["express-node"],
        {"express-node": 1},
        SEEDS,
        scenario="registration",
    )

    assert plan[0]["offeredRates"]["express-node"] == 0.25


def test_summary_qualifies_only_five_clean_stable_registration_trials() -> None:
    bundles = [
        trial_bundle(index, p99_ms=value)
        for index, value in enumerate([900, 910, 905, 895, 900], start=1)
    ]

    summary = summarize_trial_bundles(bundles)
    rate = summary["scenarios"][0]["contenders"][0]["rates"][0]

    assert rate["qualified"] is True
    assert rate["p99BudgetMs"] == 1_000
    assert rate["statistics"]["p99Ms"]["median"] == 900
    assert rate["statistics"]["p99Ms"]["confidenceInterval95"]["lower"] <= 900
    assert rate["statistics"]["p99Ms"]["confidenceInterval95"]["upper"] >= 900
    assert rate["statistics"]["p99Ms"]["coefficientOfVariation"] < 0.05
    assert rate["unstable"] is False
    assert len(rate["samples"]) == 5
    assert summary["scenarios"][0]["contenders"][0]["maximumSustainableThroughput"] == 100


@pytest.mark.parametrize(
    ("replacement", "failure"),
    [
        ({"achieved_iterations": 5_939}, "completion_below_99_percent"),
        ({"unexpected_responses": 6}, "errors_not_below_0_1_percent"),
        ({"p99_ms": 1_001}, "p99_budget_exceeded"),
        ({"valid": False}, "invalid_trial"),
    ],
)
def test_summary_rejects_each_trial_qualification_edge(
    replacement: dict[str, Any], failure: str
) -> None:
    bundles = [trial_bundle(index) for index in range(1, 6)]
    bundles[2] = trial_bundle(3, **replacement)

    rate = summarize_trial_bundles(bundles)["scenarios"][0]["contenders"][0]["rates"][0]

    assert rate["qualified"] is False
    assert failure in rate["qualificationFailures"]


def test_summary_flags_p99_variability_above_five_percent() -> None:
    bundles = [
        trial_bundle(index, p99_ms=value)
        for index, value in enumerate([700, 700, 700, 700, 900], start=1)
    ]

    rate = summarize_trial_bundles(bundles)["scenarios"][0]["contenders"][0]["rates"][0]

    assert rate["statistics"]["p99Ms"]["coefficientOfVariation"] > 0.05
    assert rate["unstable"] is True
    assert rate["qualified"] is False
    assert "unstable" in rate["qualificationFailures"]


def test_summary_enforces_the_published_workload_seed_for_each_repetition() -> None:
    bundles = [trial_bundle(index) for index in range(1, 6)]
    bundles[-1]["workloadSeed"] = 999

    with pytest.raises(ResultError, match="published repetition seed"):
        summarize_trial_bundles(bundles)


def test_summary_flags_variability_in_any_published_metric() -> None:
    bundles = [trial_bundle(index) for index in range(1, 6)]
    bundles[-1] = trial_bundle(5, unexpected_responses=1)

    rate = summarize_trial_bundles(bundles)["scenarios"][0]["contenders"][0]["rates"][0]

    assert rate["statistics"]["errorRate"]["coefficientOfVariation"] > 0.05
    assert rate["unstable"] is True
    assert rate["qualified"] is False


def test_unlike_result_series_keys_cannot_be_merged() -> None:
    bundles = [trial_bundle(index) for index in range(1, 6)]
    bundles[-1] = trial_bundle(5, environment="different-host-v1")

    with pytest.raises(ResultError, match="comparability"):
        summarize_trial_bundles(bundles)


def test_reports_regenerate_deterministically_without_composite_ranking(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "raw-series.json"
    raw.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "kind": "result-series",
                "repetitionSeeds": SEEDS,
                "calibration": {
                    "express-node": {
                        "passingRate": 100,
                        "failingRate": 103,
                        "relativeBracketWidth": 0.03,
                        "samples": [
                            {
                                "offeredRate": 100,
                                "passed": True,
                                "trial": trial_bundle(1),
                            }
                        ],
                    }
                },
                "measurements": [trial_bundle(index) for index in range(1, 6)],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "report"

    first = write_reports([raw], output)
    first_bytes = {path.name: path.read_bytes() for path in output.iterdir()}
    second = write_reports([raw], output)
    second_bytes = {path.name: path.read_bytes() for path in output.iterdir()}

    assert first == second
    assert first_bytes == second_bytes
    assert set(first_bytes) == {"summary.json", "report.md", "report.html"}
    published = b"\n".join(first_bytes.values()).lower()
    assert b"compositescore" not in published
    assert b"overall winner" not in published
    assert b"winner" not in published
    assert b"qualification: qualified" in published
    compact = json.loads(first_bytes["summary.json"])
    assert compact["calibration"]["express-node"] == {
        "passingRate": 100,
        "failingRate": 103,
        "relativeBracketWidth": 0.03,
    }


def test_capacity_sweep_records_calibration_and_five_trials_at_every_target(
    tmp_path: Path,
) -> None:
    output = tmp_path / "raw-series.json"
    calls: list[tuple[str, int, int]] = []
    modes: list[str] = []

    def run_trial(
        root: Path,
        contender_id: str,
        *,
        output: Path,
        mode: str,
        repetition: int,
        offered_rate: int,
    ) -> dict[str, Any]:
        del root
        modes.append(mode)
        calls.append((contender_id, repetition, offered_rate))
        bundle = trial_bundle(
            repetition,
            contender=contender_id,
            offered_rate=offered_rate,
            achieved_iterations=offered_rate * 60,
            p99_ms=900 if offered_rate <= 100 else 1_001,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(bundle), encoding="utf-8")
        return bundle

    raw = run_capacity_sweep(
        Path(__file__).resolve().parents[2],
        ["express-node", "hono-bun"],
        scenario="registration",
        output=output,
        trial_runner=run_trial,
    )

    assert output.is_file()
    assert raw["kind"] == "result-series"
    assert set(raw["calibration"]) == {"express-node", "hono-bun"}
    assert all(value["passingRate"] == 100 for value in raw["calibration"].values())
    assert len(raw["measurements"]) == 2 * 6 * 5
    assert {bundle["targetPercent"] for bundle in raw["measurements"]} == {
        25,
        50,
        75,
        90,
        100,
        110,
    }
    assert all(
        sorted(
            bundle["repetition"]
            for bundle in raw["measurements"]
            if bundle["contender"]["id"] == contender
            and bundle["targetPercent"] == percentage
        )
        == [1, 2, 3, 4, 5]
        for contender in ("express-node", "hono-bun")
        for percentage in (25, 50, 75, 90, 100, 110)
    )
    assert raw["measurementPlan"][0]["workloadSeed"] == SEEDS[0]
    assert calls[0][:2] == ("express-node", 1)
    assert modes[0] == "calibration"
    assert modes[-1] == "trial"


def test_capacity_sweep_refuses_to_overwrite_raw_evidence(tmp_path: Path) -> None:
    output = tmp_path / "raw-series.json"
    output.write_text("existing\n", encoding="utf-8")

    with pytest.raises(ResultError, match="already exists"):
        run_capacity_sweep(
            Path(__file__).resolve().parents[2],
            ["express-node"],
            scenario="registration",
            output=output,
            trial_runner=lambda *args, **kwargs: {},
        )

    assert output.read_text(encoding="utf-8") == "existing\n"


def test_report_command_regenerates_all_formats_from_raw_series(tmp_path: Path) -> None:
    raw = tmp_path / "raw-series.json"
    raw.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "kind": "result-series",
                "repetitionSeeds": SEEDS,
                "calibration": {
                    "express-node": {
                        "passingRate": 100,
                        "failingRate": 103,
                        "relativeBracketWidth": 0.03,
                    }
                },
                "measurements": [trial_bundle(index) for index in range(1, 6)],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "published"

    result = run_control_plane(
        "report",
        "generate",
        str(raw),
        "--output-dir",
        str(output),
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["files"] == ["report.html", "report.md", "summary.json"]
    assert {path.name for path in output.iterdir()} == {
        "report.html",
        "report.md",
        "summary.json",
    }


def test_capacity_command_requires_raw_output_path() -> None:
    result = run_control_plane(
        "capacity",
        "run",
        "express-node",
        "--scenario",
        "registration",
    )

    assert result.returncode == 2
    assert "output" in result.stderr.lower() or "required" in result.stderr.lower()
