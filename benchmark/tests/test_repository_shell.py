import json
import subprocess
import sys
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_repository_exposes_versioned_authority_seams_and_local_contenders() -> None:
    workspace = yaml.safe_load((REPOSITORY_ROOT / "pnpm-workspace.yaml").read_text(encoding="utf-8"))

    assert workspace == {"packages": ["backends/*", "packages/*"]}
    assert (REPOSITORY_ROOT / "contracts/http/openapi.yaml").is_file()
    assert (REPOSITORY_ROOT / "database/migrations").is_dir()
    assert (REPOSITORY_ROOT / "database/schema.sql").is_file()
    assert not (REPOSITORY_ROOT / "packages/api-contract/openapi.yaml").exists()
    assert not (REPOSITORY_ROOT / "packages/db-migrations/package.json").exists()
    assert (REPOSITORY_ROOT / "benchmark/protocol/VERSION").read_text(encoding="utf-8").strip() == "1.0.0"
    assert (REPOSITORY_ROOT / "benchmark/dataset/VERSION").read_text(encoding="utf-8").strip() == "1.0.0"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "link_metrics",
            "contenders",
            "discover",
            "--root",
            str(REPOSITORY_ROOT),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []
