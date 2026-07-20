"""Pinned k6 Scenario seam."""

from __future__ import annotations

import subprocess
from pathlib import Path

from link_metrics.trial import K6_IMAGE, SCENARIO_SCRIPT, SCENARIOS, registration_email


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_k6_image_is_pinned_by_digest() -> None:
    assert K6_IMAGE.startswith("grafana/k6:")
    assert "@sha256:" in K6_IMAGE
    assert len(K6_IMAGE.split("@sha256:", 1)[1]) == 64


def test_scenario_script_uses_open_loop_http11_keepalive() -> None:
    script = (REPOSITORY_ROOT / SCENARIO_SCRIPT).read_text(encoding="utf-8")

    assert "constant-arrival-rate" in script
    assert "Math.round(offeredRate * rateScale)" in script
    assert "timeUnit: `${rateScale}s`" in script
    assert "dropped_iterations" in script
    assert 'timeout: "5s"' in script
    assert "noConnectionReuse: false" in script
    assert "/api/auth/register" in script
    assert "/api/auth/login" in script
    assert "/api/links" in script
    assert "/stats" in script
    assert "redirects: 0" in script
    assert "Location" in script
    assert "expectedDestination" in script
    assert set(SCENARIOS) == {
        "registration",
        "login",
        "short-link-creation",
        "uniform-resolution",
        "viral-resolution",
        "statistics",
    }
    assert "link-metrics-benchmark-only" in script
    assert "application/json" in script
    assert "Content-Type" in script
    assert "WORKLOAD_PATH" in script
    assert "TOKENS_PATH" in script
    assert "isJsonContentType(contentType(response))" in script
    assert script.count("unexpectedResponses.add(1)") == 1


def test_scenario_script_archives_under_pinned_k6(tmp_path: Path) -> None:
    version = subprocess.run(
        ["docker", "run", "--rm", K6_IMAGE, "version"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert version.returncode == 0, version.stderr
    assert "k6 v" in version.stdout

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    out_dir.chmod(0o777)

    compiled = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "0:0",
            "--volume",
            f"{REPOSITORY_ROOT / 'benchmark/protocol/k6'}:/scripts:ro",
            "--volume",
            f"{out_dir}:/out",
            "--workdir",
            "/scripts",
            K6_IMAGE,
            "archive",
            SCENARIO_SCRIPT.name,
            "-O",
            "/out/registration.tar",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert compiled.returncode == 0, compiled.stderr + compiled.stdout
    assert (out_dir / "registration.tar").is_file()


def test_registration_email_helper_matches_k6_identity_contract() -> None:
    assert registration_email(repetition=1, iteration=42) == "reg-01-000000000042@trial.invalid"
