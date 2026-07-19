"""Mandatory black-box API conformance gate for discovered Contenders."""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from link_metrics.contenders import discover_contenders
from link_metrics.contract import lint_contract
from link_metrics.runtime import start_contender, stop_contender


class ConformanceError(Exception):
    """A Contender failed deterministic or property-based API conformance."""


def _base_url(health_url: str) -> str:
    return health_url.removesuffix("/health")


def _failure_detail(result: subprocess.CompletedProcess[str]) -> str:
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    return output[-8_000:] if output else "conformance command failed without output"


def _run_gate_command(
    arguments: list[str],
    *,
    cwd: Path,
    failure_label: str,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        arguments,
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ConformanceError(f"{failure_label} failed:\n{_failure_detail(result)}")
    return result


def _run_deterministic_workflows(benchmark_root: Path, contender_url: str) -> None:
    environment = os.environ.copy()
    environment["LINK_METRICS_CONFORMANCE_URL"] = contender_url
    _run_gate_command(
        [
            sys.executable,
            "-m",
            "pytest",
            str(benchmark_root / "conformance" / "test_api.py"),
            "--quiet",
        ],
        cwd=benchmark_root,
        environment=environment,
        failure_label="deterministic conformance",
    )


def _request_json(
    contender_url: str,
    path: str,
    body: dict[str, str],
) -> tuple[int, object]:
    parsed_url = urlsplit(contender_url)
    connection = http.client.HTTPConnection(parsed_url.hostname, parsed_url.port, timeout=10)
    try:
        connection.request(
            "POST",
            path,
            body=json.dumps(body, separators=(",", ":")).encode(),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        response_body = response.read()
        try:
            return response.status, json.loads(response_body)
        except json.JSONDecodeError as error:
            raise ConformanceError(
                f"{path} returned non-JSON content during Schemathesis setup"
            ) from error
    finally:
        connection.close()


def _schemathesis_token(contender_url: str) -> str:
    credentials = {
        "email": "schemathesis@example.com",
        "password": "benchmark-password",
    }
    registration_status, _ = _request_json(
        contender_url,
        "/api/auth/register",
        credentials,
    )
    if registration_status != 201:
        raise ConformanceError(
            f"could not create Schemathesis User: registration returned {registration_status}"
        )
    login_status, login = _request_json(contender_url, "/api/auth/login", credentials)
    if (
        login_status != 200
        or not isinstance(login, dict)
        or not isinstance(login.get("token"), str)
    ):
        raise ConformanceError(f"could not authenticate Schemathesis User: login returned {login_status}")
    return login["token"]


def _run_schemathesis(
    repository_root: Path,
    benchmark_root: Path,
    contender_url: str,
) -> None:
    executable = Path(sys.executable).with_name("schemathesis")
    if not executable.is_file():
        raise ConformanceError("the locked Schemathesis executable is unavailable")
    token = _schemathesis_token(contender_url)
    with tempfile.TemporaryDirectory(prefix="link-metrics-schemathesis-") as report_directory:
        report_path = Path(report_directory) / "events.ndjson"
        _run_gate_command(
            [
                str(executable),
                "run",
                str(repository_root / "contracts" / "http" / "openapi.yaml"),
                "--url",
                contender_url,
                "--phases",
                "examples,coverage,fuzzing,stateful",
                "--checks",
                "all",
                "--exclude-checks",
                "unsupported_method",
                "--mode",
                "all",
                "--max-examples",
                "5",
                "--generation-deterministic",
                "--workers",
                "1",
                "--request-timeout",
                "5",
                "--request-retries",
                "0",
                "--max-redirects",
                "0",
                "--header",
                f"Authorization: Bearer {token}",
                "--report",
                "ndjson",
                "--report-ndjson-path",
                str(report_path),
                "--no-color",
            ],
            cwd=benchmark_root,
            failure_label="Schemathesis conformance",
        )
        _require_completed_stateful_phase(report_path)


def _require_completed_stateful_phase(report_path: Path) -> None:
    try:
        events = [json.loads(line) for line in report_path.read_text(encoding="utf-8").splitlines()]
    except (OSError, json.JSONDecodeError) as error:
        raise ConformanceError("Schemathesis did not produce a valid machine report") from error

    completed = any(
        event.get("PhaseFinished", {}).get("phase", {}).get("name") == "Stateful"
        and event["PhaseFinished"].get("status") == "success"
        for event in events
        if isinstance(event, dict)
    )
    if not completed:
        raise ConformanceError("Schemathesis stateful phase did not complete")


def run_conformance_checks(repository_root: Path, contender_id: str, contender_url: str) -> dict:
    """Run deterministic and generative checks against an already-running Contender."""
    repository_root = repository_root.resolve()
    benchmark_root = repository_root / "benchmark"
    contender = next(
        (item for item in discover_contenders(repository_root) if item["id"] == contender_id),
        None,
    )
    if contender is None:
        raise ConformanceError(f"unknown Contender '{contender_id}'")
    contract = lint_contract(repository_root / "contracts" / "http" / "openapi.yaml")
    if contender["apiContractVersion"] != contract["version"]:
        raise ConformanceError(
            f"Contender '{contender_id}' declares API Contract "
            f"{contender['apiContractVersion']}, expected {contract['version']}"
        )
    _run_deterministic_workflows(benchmark_root, contender_url)
    _run_schemathesis(repository_root, benchmark_root, contender_url)
    return {
        "apiContractVersion": contract["version"],
        "checks": ["deterministic", "schemathesis"],
        "conforming": True,
        "contenderId": contender_id,
        "eligible": True,
    }


def conform_contender(repository_root: Path, contender_id: str) -> dict:
    """Operate a fresh Contender and return eligibility only after all checks pass."""
    repository_root = repository_root.resolve()
    state = start_contender(repository_root, contender_id)
    try:
        return run_conformance_checks(
            repository_root,
            contender_id,
            _base_url(state["contender"]["url"]),
        )
    finally:
        stop_contender(repository_root, contender_id)
