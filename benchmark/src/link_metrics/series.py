"""Time-bounded orchestration for the complete local Result Series."""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from link_metrics.dataset_runtime import prepare_template_runtime
from link_metrics.environment import (
    LOCAL_RESOURCE_PROFILE,
    assess_host_preflight,
    capture_host_observation,
)
from link_metrics.evidence import write_immutable_json
from link_metrics.progress import ExecutionBudget
from link_metrics.reporting import write_reports
from link_metrics.results import run_capacity_sweep
from link_metrics.scenarios import SCENARIOS
from link_metrics.startup import run_cold_startup


class SeriesError(Exception):
    """The complete Result Series cannot advance or be verified."""


def prepare_series_contender(root: Path, contender_id: str) -> None:
    """Keep one migrated, templated PostgreSQL container ready across sessions."""
    prepare_template_runtime(root, contender_id)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_result_series(
    output_dir: Path,
    *,
    report_regenerator: Callable[..., dict[str, Any]] = write_reports,
) -> dict[str, Any]:
    """Verify every raw and generated file named by the series manifest."""
    output_dir = output_dir.resolve()
    manifest_path = output_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        checksums = manifest["checksums"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise SeriesError(f"cannot read Result Series manifest: {manifest_path}") from error
    if manifest.get("kind") != "result-series-manifest" or not isinstance(checksums, dict):
        raise SeriesError(f"invalid Result Series manifest: {manifest_path}")
    actual_published = {
        str(path.relative_to(output_dir))
        for directory in (output_dir / "raw", output_dir / "report")
        if directory.is_dir()
        for path in directory.rglob("*")
        if path.is_file()
    }
    if set(checksums) != actual_published:
        raise SeriesError("Result Series manifest does not cover every published file")
    for relative, expected in sorted(checksums.items()):
        path = (output_dir / str(relative)).resolve()
        if output_dir not in path.parents or not path.is_file():
            raise SeriesError(
                f"manifest file is missing or outside the Result Series: {relative}"
            )
        if _sha256(path) != expected:
            raise SeriesError(f"checksum mismatch for Result Series file: {relative}")
    raw_paths = [
        *sorted((output_dir / "raw" / "capacity").glob("*.json")),
        *sorted((output_dir / "raw" / "startup").glob("*.json")),
    ]
    if not raw_paths:
        raise SeriesError("Result Series manifest contains no raw bundles")
    try:
        with tempfile.TemporaryDirectory(prefix="link-metrics-verify-") as temporary:
            regenerated_dir = Path(temporary)
            regenerated = report_regenerator(raw_paths, regenerated_dir)
            if regenerated.get("comparabilityKey") != manifest.get("comparabilityKey"):
                raise SeriesError("regenerated report provenance does not match the manifest")
            for relative in sorted(checksums):
                relative_path = Path(str(relative))
                if not relative_path.parts or relative_path.parts[0] != "report":
                    continue
                published = output_dir / relative_path
                regenerated_path = regenerated_dir / Path(*relative_path.parts[1:])
                if (
                    not regenerated_path.is_file()
                    or published.read_bytes() != regenerated_path.read_bytes()
                ):
                    raise SeriesError(
                        f"regenerated report mismatch for Result Series file: {relative}"
                    )
    except SeriesError:
        raise
    except Exception as error:
        raise SeriesError(f"cannot regenerate Result Series reports: {error}") from error
    return {
        "schemaVersion": 1,
        "kind": "result-series-verification",
        "valid": True,
        "filesVerified": len(checksums),
        "outputDirectory": str(output_dir),
    }


def _progress(
    output_dir: Path,
    contenders: Sequence[str],
    completed_scenarios: list[str],
    completed_startups: list[str],
    budget: ExecutionBudget,
) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "kind": "result-series-progress",
        "status": "paused",
        "outputDirectory": str(output_dir),
        "contenders": list(contenders),
        "completedScenarios": completed_scenarios,
        "completedColdStarts": completed_startups,
        "newAtomicUnitsThisSession": budget.completed_units,
    }


def run_result_series(
    root: Path,
    *,
    output_dir: Path,
    contenders: Sequence[str],
    budget: ExecutionBudget,
    prepare_contender: Callable[[Path, str], None] = prepare_series_contender,
    capacity_runner: Callable[..., dict[str, Any]] = run_capacity_sweep,
    startup_runner: Callable[..., dict[str, Any]] = run_cold_startup,
    report_writer: Callable[..., dict[str, Any]] = write_reports,
    result_verifier: Callable[[Path], dict[str, Any]] = verify_result_series,
    preflight: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Advance the complete cohort until the daily budget expires or work completes."""
    root = root.resolve()
    output_dir = output_dir.resolve()
    contenders = tuple(contenders)
    if not contenders or len(set(contenders)) != len(contenders):
        raise SeriesError("a Result Series requires unique Contenders")
    manifest_path = output_dir / "manifest.json"
    if manifest_path.exists():
        verification = result_verifier(output_dir)
        return {
            **verification,
            "kind": "result-series-progress",
            "status": "complete",
            "completedScenarios": list(SCENARIOS),
            "completedColdStarts": list(contenders),
        }
    if preflight is None:
        preflight = lambda: assess_host_preflight(
            LOCAL_RESOURCE_PROFILE,
            capture_host_observation(LOCAL_RESOURCE_PROFILE),
        )
    assessment = preflight()
    if not assessment.get("valid"):
        raise SeriesError(
            "host preflight failed: " + ", ".join(assessment.get("reasons", []))
        )
    if not budget.can_start():
        return _progress(output_dir, contenders, [], [], budget)

    for contender in contenders:
        if not budget.can_start():
            return _progress(output_dir, contenders, [], [], budget)
        prepare_contender(root, contender)

    capacity_paths: list[Path] = []
    completed_scenarios: list[str] = []
    for scenario in SCENARIOS:
        path = output_dir / "raw" / "capacity" / f"{scenario}.json"
        capacity_paths.append(path)
        result = capacity_runner(
            root,
            contenders,
            scenario=scenario,
            output=path,
            budget=budget,
        )
        if result.get("kind") != "result-series":
            return _progress(output_dir, contenders, completed_scenarios, [], budget)
        completed_scenarios.append(scenario)

    startup_paths: list[Path] = []
    completed_startups: list[str] = []
    for contender in contenders:
        path = output_dir / "raw" / "startup" / f"{contender}.json"
        startup_paths.append(path)
        result = startup_runner(root, contender, output=path, budget=budget)
        if result.get("kind") != "cold-start-series":
            return _progress(
                output_dir,
                contenders,
                completed_scenarios,
                completed_startups,
                budget,
            )
        completed_startups.append(contender)

    raw_paths = [*capacity_paths, *startup_paths]
    report_directory = output_dir / "report"
    report = report_writer(raw_paths, report_directory)
    published_paths = [
        *sorted(path for path in (output_dir / "raw").rglob("*") if path.is_file()),
        report_directory / "summary.json",
        report_directory / "report.md",
        report_directory / "report.html",
    ]
    checksums = {
        str(path.relative_to(output_dir)): _sha256(path) for path in published_paths
    }
    try:
        write_immutable_json(
            manifest_path,
            {
                "schemaVersion": 1,
                "kind": "result-series-manifest",
                "comparabilityKey": report["comparabilityKey"],
                "contenders": list(contenders),
                "scenarios": list(SCENARIOS),
                "checksums": checksums,
            },
        )
    except (FileExistsError, KeyError) as error:
        raise SeriesError(f"cannot publish Result Series manifest: {manifest_path}") from error
    verification = result_verifier(output_dir)
    return {
        **verification,
        "kind": "result-series-progress",
        "status": "complete",
        "completedScenarios": completed_scenarios,
        "completedColdStarts": completed_startups,
    }
