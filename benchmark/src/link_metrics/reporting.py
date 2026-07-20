"""Deterministic compact, Markdown, and HTML Result Series reports."""

from __future__ import annotations

import html
import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from link_metrics.results import ResultError, summarize_trial_bundles


def _load_raw_series(
    paths: Iterable[Path],
) -> tuple[list[dict[str, Any]], dict[str, Any], list[int] | None]:
    measurements: list[dict[str, Any]] = []
    calibration: dict[str, Any] = {}
    repetition_seeds: list[int] | None = None
    for path in paths:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ResultError(f"cannot read raw result bundle {path}: {error}") from error
        if document.get("kind") == "result-series":
            raw_measurements = document.get("measurements")
            if not isinstance(raw_measurements, list):
                raise ResultError(f"raw Result Series {path} has no measurements")
            measurements.extend(raw_measurements)
            raw_seeds = document.get("repetitionSeeds")
            if not isinstance(raw_seeds, list) or len(raw_seeds) != 5:
                raise ResultError(f"raw Result Series {path} has no five repetition seeds")
            parsed_seeds = [int(seed) for seed in raw_seeds]
            if repetition_seeds is not None and repetition_seeds != parsed_seeds:
                raise ResultError("raw Result Series publish different repetition seeds")
            repetition_seeds = parsed_seeds
            raw_calibration = document.get("calibration", {})
            if not isinstance(raw_calibration, dict):
                raise ResultError(f"raw Result Series {path} has invalid calibration")
            for contender, value in raw_calibration.items():
                if contender in calibration and calibration[contender] != value:
                    raise ResultError(f"conflicting calibration for Contender {contender}")
                calibration[contender] = value
        else:
            measurements.append(document)
    return measurements, calibration, repetition_seeds


def _format_number(value: Any) -> str:
    if value is None:
        return "not qualified"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _compact_calibration(calibration: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for contender, result in sorted(calibration.items()):
        try:
            compact[contender] = {
                "passingRate": float(result["passingRate"]),
                "failingRate": float(result["failingRate"]),
                "relativeBracketWidth": float(result["relativeBracketWidth"]),
            }
        except (KeyError, TypeError, ValueError) as error:
            raise ResultError(f"calibration for Contender {contender} is incomplete") from error
    return compact


def _markdown_report(summary: dict[str, Any]) -> str:
    lines = [
        "# Link Metrics Result Series",
        "",
        "Comparability key:",
        "",
        "```json",
        json.dumps(summary["comparabilityKey"], indent=2, sort_keys=True),
        "```",
    ]
    if summary.get("calibration"):
        lines.extend(["", "Calibration boundaries (calibration samples are unreported):"])
        for contender, calibration in summary["calibration"].items():
            lines.append(
                f"- {contender}: {_format_number(calibration['passingRate'])} requests/s "
                f"passing, {_format_number(calibration['failingRate'])} requests/s failing"
            )
    for scenario in summary["scenarios"]:
        lines.extend(["", f"## Scenario: {scenario['id']}"])
        for contender in scenario["contenders"]:
            lines.extend(
                [
                    "",
                    f"### Contender: {contender['id']}",
                    "",
                    "Maximum sustainable throughput: "
                    f"{_format_number(contender['maximumSustainableThroughput'])} requests/s",
                    "",
                    "| Offered | Repetition | Seed | Achieved/s | Completion | Error rate | p99 ms | Valid |",
                    "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | :---: |",
                ]
            )
            for rate in contender["rates"]:
                for sample in rate["samples"]:
                    lines.append(
                        "| "
                        + " | ".join(
                            [
                                _format_number(rate["offeredRate"]),
                                str(sample["repetition"]),
                                str(sample["workloadSeed"]),
                                _format_number(sample["achievedThroughput"]),
                                _format_number(sample["completionRate"]),
                                _format_number(sample["errorRate"]),
                                _format_number(sample["p99Ms"]),
                                "yes" if sample["valid"] else "no",
                            ]
                        )
                        + " |"
                    )
                lines.extend(
                    [
                        "",
                        f"Rate {_format_number(rate['offeredRate'])} statistics:",
                        "",
                        "| Metric | Median | 95% bootstrap CI | CV | Unstable |",
                        "| --- | ---: | --- | ---: | :---: |",
                    ]
                )
                for metric, values in rate["statistics"].items():
                    interval = values["confidenceInterval95"]
                    lines.append(
                        f"| {metric} | {_format_number(values['median'])} | "
                        f"{_format_number(interval['lower'])}–{_format_number(interval['upper'])} | "
                        f"{_format_number(values['coefficientOfVariation'])} | "
                        f"{'yes' if values['unstable'] else 'no'} |"
                    )
                qualification = (
                    "qualified"
                    if rate["qualified"]
                    else "not qualified: " + ", ".join(rate["qualificationFailures"])
                )
                lines.extend(["", f"Qualification: {qualification}."])
    return "\n".join(lines) + "\n"


def _html_report(summary: dict[str, Any]) -> str:
    markdown = _markdown_report(summary)
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        "<title>Link Metrics Result Series</title>"
        "<style>body{font-family:system-ui;max-width:1100px;margin:2rem auto;"
        "padding:0 1rem}pre{white-space:pre-wrap}</style></head><body>"
        "<h1>Link Metrics Result Series</h1>"
        "<p>This deterministic report preserves every sample and its per-rate statistics.</p>"
        f"<pre>{html.escape(markdown)}</pre>"
        "</body></html>\n"
    )


def write_reports(raw_paths: Sequence[Path], output_dir: Path) -> dict[str, Any]:
    """Regenerate compact JSON, Markdown, and HTML from raw Result Series."""
    measurements, calibration, repetition_seeds = _load_raw_series(raw_paths)
    summary = summarize_trial_bundles(
        measurements,
        expected_repetition_seeds=repetition_seeds,
    )
    summary["repetitionSeeds"] = repetition_seeds
    summary["calibration"] = _compact_calibration(calibration)
    output_dir.mkdir(parents=True, exist_ok=True)
    contents = {
        "summary.json": json.dumps(summary, indent=2, sort_keys=True) + "\n",
        "report.md": _markdown_report(summary),
        "report.html": _html_report(summary),
    }
    for filename, content in contents.items():
        (output_dir / filename).write_text(content, encoding="utf-8")
    return {
        "outputDirectory": str(output_dir),
        "files": sorted(contents),
        "comparabilityKey": summary["comparabilityKey"],
    }
