import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from link_metrics import conformance
from link_metrics.conformance import (
    ConformanceError,
    _require_completed_stateful_phase,
    run_conformance_checks,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def run_control_plane(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "link_metrics", *arguments, "--root", str(REPOSITORY_ROOT)],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("contender_id", ["express-node", "nest-node"])
def test_contender_passes_the_mandatory_conformance_gate(contender_id: str) -> None:
    result = run_control_plane("contenders", "conform", contender_id)

    assert result.returncode == 0, result.stderr + result.stdout
    assert json.loads(result.stdout) == {
        "apiContractVersion": "1.0.1",
        "checks": ["deterministic", "schemathesis"],
        "conforming": True,
        "contenderId": contender_id,
        "eligible": True,
    }

    inspected = run_control_plane("contenders", "inspect", contender_id)
    assert inspected.returncode == 2
    assert "is not started" in inspected.stderr


class NonconformingService(BaseHTTPRequestHandler):
    def respond(self) -> None:
        body = b'{"unexpected":true}'
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = respond
    do_POST = respond

    def log_message(self, _format: str, *args: object) -> None:
        pass


def test_nonconforming_service_cannot_produce_eligibility() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), NonconformingService)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        with pytest.raises(ConformanceError, match="deterministic conformance failed"):
            run_conformance_checks(
                REPOSITORY_ROOT,
                "express-node",
                f"http://127.0.0.1:{server.server_port}",
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_schemathesis_excludes_the_deterministically_checked_redirect_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(conformance, "_schemathesis_token", lambda contender_url: "token")
    monkeypatch.setattr(
        conformance,
        "_run_gate_command",
        lambda arguments, **kwargs: commands.append(arguments)
        or subprocess.CompletedProcess(arguments, 0, "", ""),
    )
    monkeypatch.setattr(conformance, "_require_completed_stateful_phase", lambda path: None)

    conformance._run_schemathesis(
        REPOSITORY_ROOT,
        REPOSITORY_ROOT / "benchmark",
        "http://contender",
    )

    command = commands[0]
    exclusion = command.index("--exclude-operation-id")
    assert command[exclusion + 1] == "resolveShortLink"


def test_machine_report_requires_a_completed_stateful_phase(tmp_path: Path) -> None:
    report = tmp_path / "schemathesis.ndjson"
    report.write_text(
        '{"PhaseFinished":{"phase":{"name":"Stateful","is_enabled":true},'
        '"status":"skip"}}\n',
        encoding="utf-8",
    )

    with pytest.raises(ConformanceError, match="stateful phase did not complete"):
        _require_completed_stateful_phase(report)


def test_machine_report_accepts_a_successful_stateful_phase(tmp_path: Path) -> None:
    report = tmp_path / "schemathesis.ndjson"
    report.write_text(
        '{"PhaseFinished":{"phase":{"name":"Stateful","is_enabled":true},'
        '"status":"success"}}\n',
        encoding="utf-8",
    )

    _require_completed_stateful_phase(report)
