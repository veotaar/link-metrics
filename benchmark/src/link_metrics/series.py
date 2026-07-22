"""Time-bounded orchestration for the complete local Result Series."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from link_metrics.dataset_runtime import build_template
from link_metrics.environment import (
    LOCAL_RESOURCE_PROFILE,
    assess_host_preflight,
    capture_host_observation,
)
from link_metrics.evidence import write_immutable_json
from link_metrics.progress import ExecutionBudget
from link_metrics.reporting import write_reports
from link_metrics.results import run_capacity_sweep
from link_metrics.runtime import (
    _container_document,
    _container_exists,
    _docker,
    _resource_names,
    _wait_for_postgres,
    start_contender,
)
from link_metrics.scenarios import SCENARIOS
from link_metrics.startup import run_cold_startup
from link_metrics.trial import _stop_contender_container


class SeriesError(Exception):
    """The complete Result Series cannot advance or be verified."""


def prepare_series_contender(root: Path, contender_id: str) -> None:
    """Keep one migrated, templated PostgreSQL container ready across sessions."""
    names = _resource_names(root, contender_id)
    if not _container_exists(names.database):
        start_contender(root, contender_id)
    elif not bool(_container_document(names.database)["State"]["Running"]):
        _docker("start", names.database)
        _wait_for_postgres(names.database)
    build_template(root, contender_id)
    _stop_contender_container(root, contender_id)
    _docker("stop", "--time", "5", names.database, check=False)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_result_series(output_dir: Path) -> dict[str, Any]:
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
    for relative, expected in sorted(checksums.items()):
        path = (output_dir / str(relative)).resolve()
        if output_dir not in path.parents or not path.is_file():
            raise SeriesError(
                f"manifest file is missing or outside the Result Series: {relative}"
            )
        if _sha256(path) != expected:
            raise SeriesError(f"checksum mismatch for Result Series file: {relative}")
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
        verification = verify_result_series(output_dir)
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
    verification = verify_result_series(output_dir)
    return {
        **verification,
        "kind": "result-series-progress",
        "status": "complete",
        "completedScenarios": completed_scenarios,
        "completedColdStarts": completed_startups,
    }
