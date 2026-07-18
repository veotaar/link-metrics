import json
import subprocess
import sys
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_lints_the_versioned_minimal_api_contract() -> None:
    document = yaml.safe_load(
        (REPOSITORY_ROOT / "contracts/http/openapi.yaml").read_text(encoding="utf-8")
    )
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
    summary = json.loads(result.stdout)
    assert summary["openapi"] == document["openapi"] == "3.1.2"
    assert summary["version"] == document["info"]["version"]
    assert summary["operations"] == sorted(
        operation["operationId"]
        for path_item in document["paths"].values()
        for method, operation in path_item.items()
        if method in {"get", "post", "put", "patch", "delete", "options", "head", "trace"}
    )
