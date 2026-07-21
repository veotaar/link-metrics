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
    assert (REPOSITORY_ROOT / "benchmark/protocol/VERSION").read_text(encoding="utf-8").strip() == "3.0.0"
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


def test_hosted_ci_is_a_discovery_driven_correctness_gate() -> None:
    workflow_path = REPOSITORY_ROOT / ".github/workflows/ci.yml"
    workflow_source = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_source)
    jobs = workflow["jobs"]

    discovery = jobs["discover-contenders"]
    assert discovery["outputs"]["contenders"] == "${{ steps.discover.outputs.contenders }}"
    assert "contenders discover" in workflow_source
    assert "map(.id)" in workflow_source

    contender_gate = jobs["contender-correctness"]
    assert contender_gate["needs"] == ["discover-contenders", "authorities"]
    assert contender_gate["strategy"]["matrix"]["contender"] == (
        "${{ fromJSON(needs.discover-contenders.outputs.contenders) }}"
    )

    authority_steps = jobs["authorities"]["steps"]
    authority_commands = "\n".join(step.get("run", "") for step in authority_steps)
    required_authority_commands = (
        "pnpm build",
        "pnpm check-types",
        "pnpm lint",
        "link-metrics contract lint",
        "uv run pytest",
        "dbmate",
        "git diff --exit-code -- database/schema.sql",
        'scripts["db:introspect"]',
        "git status --porcelain --untracked-files=all -- backends",
    )
    for command in required_authority_commands:
        assert command in authority_commands

    contender_commands = "\n".join(step.get("run", "") for step in contender_gate["steps"])
    assert 'contenders conform "${{ matrix.contender }}"' in contender_commands

    forbidden_hosted_commands = (
        "dataset build",
        "trial smoke",
        "trial run",
        "capacity run",
        "startup run",
        "report generate",
    )
    for command in forbidden_hosted_commands:
        assert command not in workflow_source
