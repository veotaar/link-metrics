import json
import subprocess
import sys
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_repository_exposes_versioned_authority_seams_and_local_contenders() -> None:
    workspace = yaml.safe_load((REPOSITORY_ROOT / "pnpm-workspace.yaml").read_text(encoding="utf-8"))

    assert workspace == {
        "allowBuilds": {"esbuild": True},
        "packages": ["backends/*", "packages/*"],
    }
    assert (REPOSITORY_ROOT / "contracts/http/openapi.yaml").is_file()
    assert (REPOSITORY_ROOT / "database/migrations").is_dir()
    assert (REPOSITORY_ROOT / "database/schema.sql").is_file()
    assert (REPOSITORY_ROOT / "backends/express-node/drizzle/schema.ts").is_file()
    assert not (REPOSITORY_ROOT / "packages/api-contract/openapi.yaml").exists()
    assert not (REPOSITORY_ROOT / "packages/db-migrations/package.json").exists()
    assert (REPOSITORY_ROOT / "benchmark/protocol/VERSION").read_text(encoding="utf-8").strip() == "2.0.0"
    assert (REPOSITORY_ROOT / "benchmark/dataset/VERSION").read_text(encoding="utf-8").strip() == "1.2.0"

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
    assert json.loads(result.stdout) == [
        {
            "apiContractVersion": "1.0.1",
            "container": {"context": ".", "dockerfile": "Dockerfile"},
            "displayName": "Express on Node.js",
            "framework": {"name": "Express", "version": "5.2.1"},
            "id": "express-node",
            "language": {"name": "TypeScript", "version": "7.0.2"},
            "manifest": "backends/express-node/contender.yaml",
            "port": 3000,
            "resourceProfile": "local-7800x3d",
            "runtime": {"name": "Node.js", "version": "26.2.0"},
            "schemaVersion": 1,
            "workers": {"count": 1, "model": "single-process"},
        }
    ]
