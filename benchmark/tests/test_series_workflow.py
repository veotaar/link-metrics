"""Complete resumable Result Series behavior at the command seam."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from link_metrics.progress import ExecutionBudget
from link_metrics.scenarios import SCENARIOS
from link_metrics.series import SeriesError, run_result_series, verify_result_series


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONTENDERS = ("elysia-bun", "express-node", "hono-bun", "nest-node")


def test_complete_series_resumes_across_bounded_daily_sessions(tmp_path: Path) -> None:
    capacity_calls: list[str] = []
    startup_calls: list[str] = []
    prepare_calls: list[str] = []

    def prepare(root: Path, contender: str) -> None:
        assert root == REPOSITORY_ROOT
        if contender not in prepare_calls:
            prepare_calls.append(contender)

    def capacity(
        root: Path,
        contenders: tuple[str, ...],
        *,
        scenario: str,
        output: Path,
        budget: ExecutionBudget,
    ) -> dict[str, Any]:
        assert root == REPOSITORY_ROOT
        assert contenders == CONTENDERS
        if output.exists():
            return json.loads(output.read_text(encoding="utf-8"))
        if not budget.can_start():
            return {"kind": "capacity-progress", "status": "paused"}
        capacity_calls.append(scenario)
        budget.record_completed()
        document = {"kind": "result-series", "scenario": scenario}
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(document), encoding="utf-8")
        return document

    def startup(
        root: Path,
        contender: str,
        *,
        output: Path,
        budget: ExecutionBudget,
    ) -> dict[str, Any]:
        assert root == REPOSITORY_ROOT
        if output.exists():
            return json.loads(output.read_text(encoding="utf-8"))
        if not budget.can_start():
            return {"kind": "cold-start-progress", "status": "paused"}
        startup_calls.append(contender)
        budget.record_completed()
        document = {"kind": "cold-start-series", "contender": {"id": contender}}
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(document), encoding="utf-8")
        return document

    def report(raw: list[Path], output: Path) -> dict[str, Any]:
        assert len(raw) == len(SCENARIOS) + len(CONTENDERS)
        output.mkdir(parents=True, exist_ok=True)
        for name in ("summary.json", "report.md", "report.html"):
            (output / name).write_text(name + "\n", encoding="utf-8")
        return {"comparabilityKey": {"environment": "local-7800x3d"}}

    first = run_result_series(
        REPOSITORY_ROOT,
        output_dir=tmp_path,
        contenders=CONTENDERS,
        budget=ExecutionBudget(maximum_units=2),
        prepare_contender=prepare,
        capacity_runner=capacity,
        startup_runner=startup,
        report_writer=report,
        preflight=lambda: {"valid": True, "reasons": []},
    )

    assert first["status"] == "paused"
    assert first["completedScenarios"] == list(SCENARIOS[:2])
    assert capacity_calls == list(SCENARIOS[:2])
    assert startup_calls == []

    completed = run_result_series(
        REPOSITORY_ROOT,
        output_dir=tmp_path,
        contenders=CONTENDERS,
        budget=ExecutionBudget(),
        prepare_contender=prepare,
        capacity_runner=capacity,
        startup_runner=startup,
        report_writer=report,
        preflight=lambda: {"valid": True, "reasons": []},
    )

    assert completed["status"] == "complete"
    assert capacity_calls == list(SCENARIOS)
    assert startup_calls == list(CONTENDERS)
    assert prepare_calls == list(CONTENDERS)
    assert verify_result_series(tmp_path)["valid"] is True


def test_series_verification_rejects_changed_generated_or_raw_evidence(tmp_path: Path) -> None:
    evidence = tmp_path / "raw" / "capacity" / "registration.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("original\n", encoding="utf-8")
    report = tmp_path / "report" / "summary.json"
    report.parent.mkdir(parents=True)
    report.write_text("{}\n", encoding="utf-8")
    checksums = {
        str(path.relative_to(tmp_path)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (evidence, report)
    }
    (tmp_path / "manifest.json").write_text(
        json.dumps({"kind": "result-series-manifest", "checksums": checksums}),
        encoding="utf-8",
    )

    report.write_text('{"changed":true}\n', encoding="utf-8")

    with pytest.raises(SeriesError, match="checksum mismatch"):
        verify_result_series(tmp_path)


def test_series_cli_requires_a_daily_time_budget_and_exposes_verification() -> None:
    run_help = subprocess.run(
        [sys.executable, "-m", "link_metrics", "series", "run", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    verify_help = subprocess.run(
        [sys.executable, "-m", "link_metrics", "series", "verify", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert run_help.returncode == 0
    assert "--time-budget-hours" in run_help.stdout
    assert "--output-dir" in run_help.stdout
    assert verify_help.returncode == 0
    assert "--output-dir" in verify_help.stdout
