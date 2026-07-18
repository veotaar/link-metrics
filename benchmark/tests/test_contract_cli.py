import json
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_lints_the_versioned_minimal_api_contract() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "link_metrics",
            "contract",
            "lint",
            "--document",
            str(REPOSITORY_ROOT / "contracts/http/openapi.yaml"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "openapi": "3.1.2",
        "operations": [
            "createShortLink",
            "getShortLinkStats",
            "health",
            "loginUser",
            "registerUser",
            "resolveShortLink",
        ],
        "version": "1.0.0",
    }


def test_rejects_a_redirect_status_other_than_302(tmp_path: Path) -> None:
    source = (REPOSITORY_ROOT / "contracts/http/openapi.yaml").read_text(encoding="utf-8")
    drifted_contract = tmp_path / "openapi.yaml"
    drifted_contract.write_text(source.replace('"302":', '"301":', 1), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "link_metrics",
            "contract",
            "lint",
            "--document",
            str(drifted_contract),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "responses must be ['302', '404', '503']" in result.stderr
    assert "['301', '404', '503']" in result.stderr
