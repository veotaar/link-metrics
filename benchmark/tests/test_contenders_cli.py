import json
import subprocess
import sys
from pathlib import Path


VALID_MANIFEST = """\
schemaVersion: 1
id: {contender_id}
displayName: Express on Node.js
apiContractVersion: 1.0.0
language:
  name: TypeScript
  version: 7.0.2
runtime:
  name: Node.js
  version: 26.0.0
framework:
  name: Express
  version: 5.2.1
container:
  context: .
  dockerfile: Dockerfile
port: {port}
workers:
  model: single-process
  count: 1
resourceProfile: local-7800x3d
"""


def write_manifest(
    root: Path,
    directory: str,
    *,
    contender_id: str = "express-node",
    port: int = 3000,
    dockerfile: bool = True,
) -> None:
    backend = root / "backends" / directory
    backend.mkdir(parents=True)
    (backend / "contender.yaml").write_text(
        VALID_MANIFEST.format(contender_id=contender_id, port=port),
        encoding="utf-8",
    )
    if dockerfile:
        (backend / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")


def discover(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "link_metrics", "contenders", "discover", "--root", str(root)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_discovers_a_valid_contender_manifest(tmp_path: Path) -> None:
    write_manifest(tmp_path, "express-node")

    result = discover(tmp_path)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        {
            "apiContractVersion": "1.0.0",
            "container": {"context": ".", "dockerfile": "Dockerfile"},
            "displayName": "Express on Node.js",
            "framework": {"name": "Express", "version": "5.2.1"},
            "id": "express-node",
            "language": {"name": "TypeScript", "version": "7.0.2"},
            "manifest": "backends/express-node/contender.yaml",
            "port": 3000,
            "resourceProfile": "local-7800x3d",
            "runtime": {"name": "Node.js", "version": "26.0.0"},
            "schemaVersion": 1,
            "workers": {"count": 1, "model": "single-process"},
        }
    ]


def test_rejects_an_invalid_contender_manifest_with_the_field_path(tmp_path: Path) -> None:
    write_manifest(tmp_path, "express-node", port=70000)

    result = discover(tmp_path)

    assert result.returncode == 2
    assert "backends/express-node/contender.yaml: port:" in result.stderr
    assert "65535" in result.stderr


def test_rejects_a_missing_container_file_with_the_field_path(tmp_path: Path) -> None:
    write_manifest(tmp_path, "express-node", dockerfile=False)

    result = discover(tmp_path)

    assert result.returncode == 2
    assert "backends/express-node/contender.yaml: container.dockerfile:" in result.stderr
    assert "Dockerfile" in result.stderr


def test_rejects_duplicate_contender_identities(tmp_path: Path) -> None:
    write_manifest(tmp_path, "express", contender_id="express-node")
    write_manifest(tmp_path, "express-copy", contender_id="express-node")

    result = discover(tmp_path)

    assert result.returncode == 2
    assert "duplicate Contender id 'express-node'" in result.stderr
    assert "backends/express/contender.yaml" in result.stderr
    assert "backends/express-copy/contender.yaml" in result.stderr
