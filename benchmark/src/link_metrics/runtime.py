"""Container lifecycle for local Contenders and their PostgreSQL authority."""

from __future__ import annotations

import hashlib
import json
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from link_metrics.contenders import discover_contenders


POSTGRES_IMAGE = (
    "postgres:18.4-bookworm@"
    "sha256:16fa100a3a6e92c0556632870455e7f8c6f3df5cefddd67d6b95292732bd7ff0"
)
DBMATE_VERSION = "2.34.1"
DATABASE_NAME = "link_metrics"
CONTROL_ROLE = "link_metrics_control"
CONTENDER_ROLE = "link_metrics_contender"
CONTENDER_PASSWORD = "benchmark-contender-only"


class ContenderRuntimeError(Exception):
    """The control plane could not operate a Contender runtime."""


@dataclass(frozen=True)
class ResourceNames:
    network: str
    database: str
    contender: str
    image: str


def _resource_names(root: Path, contender_id: str) -> ResourceNames:
    repository_key = hashlib.sha256(str(root).encode()).hexdigest()[:8]
    prefix = f"link-metrics-{repository_key}"
    return ResourceNames(
        network=f"{prefix}-{contender_id}-network",
        database=f"{prefix}-{contender_id}-postgres",
        contender=f"{prefix}-{contender_id}",
        image=f"{prefix}-{contender_id}:local",
    )


def _docker(
    *arguments: str,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["docker", *arguments],
            check=False,
            capture_output=True,
            input=input_text,
            text=True,
        )
    except FileNotFoundError as error:
        raise ContenderRuntimeError("docker is required to operate Contenders") from error

    if check and result.returncode != 0:
        detail = "\n".join(
            output for output in (result.stdout.strip(), result.stderr.strip()) if output
        ) or "unknown Docker failure"
        raise ContenderRuntimeError(f"docker {' '.join(arguments)}: {detail}")
    return result


def _container_exists(name: str) -> bool:
    return _docker("container", "inspect", name, check=False).returncode == 0


def _network_exists(name: str) -> bool:
    return _docker("network", "inspect", name, check=False).returncode == 0


def _migration_version(root: Path) -> str:
    migrations = sorted((root / "database" / "migrations").glob("*.sql"))
    if not migrations:
        raise ContenderRuntimeError("database/migrations contains no migrations")
    return migrations[-1].stem.split("_", 1)[0]


def _wait_for_postgres(database_container: str, timeout_seconds: float = 30) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        ready = _docker(
            "exec",
            database_container,
            "psql",
            "--username",
            CONTROL_ROLE,
            "--dbname",
            DATABASE_NAME,
            "--command",
            "SELECT 1",
            check=False,
        )
        if ready.returncode == 0:
            return
        time.sleep(0.2)
    raise ContenderRuntimeError("PostgreSQL did not become ready within 30 seconds")


def _psql(database_container: str, sql: str) -> None:
    _docker(
        "exec",
        "--interactive",
        database_container,
        "psql",
        "--username",
        CONTROL_ROLE,
        "--dbname",
        DATABASE_NAME,
        "--set",
        "ON_ERROR_STOP=1",
        input_text=sql,
    )


def _run_dbmate(root: Path, database_url: str) -> None:
    try:
        version = subprocess.run(
            ["dbmate", "--version"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise ContenderRuntimeError(f"dbmate {DBMATE_VERSION} is required") from error
    if version.returncode != 0 or version.stdout.strip() != f"dbmate version {DBMATE_VERSION}":
        raise ContenderRuntimeError(f"dbmate {DBMATE_VERSION} is required")

    result = subprocess.run(
        [
            "dbmate",
            "--url",
            database_url,
            "--migrations-dir",
            str(root / "database" / "migrations"),
            "--schema-file",
            str(root / "database" / "schema.sql"),
            "--no-dump-schema",
            "up",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown dbmate failure"
        raise ContenderRuntimeError(f"dbmate up: {detail}")


def _prepare_database(root: Path, database_container: str, database_url: str) -> None:
    _psql(
        database_container,
        f"""
DROP EXTENSION IF EXISTS plpgsql;
CREATE EXTENSION pg_prewarm WITH SCHEMA pg_catalog;
REVOKE ALL ON DATABASE {DATABASE_NAME} FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM PUBLIC;

CREATE ROLE {CONTENDER_ROLE}
    LOGIN PASSWORD '{CONTENDER_PASSWORD}'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
ALTER ROLE {CONTENDER_ROLE} SET statement_timeout = '2s';
GRANT CONNECT ON DATABASE {DATABASE_NAME} TO {CONTENDER_ROLE};
GRANT USAGE ON SCHEMA public TO {CONTENDER_ROLE};
""",
    )

    _run_dbmate(root, database_url)

    _psql(
        database_container,
        f"""
GRANT SELECT, INSERT ON TABLE public.users TO {CONTENDER_ROLE};
GRANT SELECT, INSERT, UPDATE ON TABLE public.links TO {CONTENDER_ROLE};
GRANT SELECT ON TABLE public.schema_migrations TO {CONTENDER_ROLE};
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {CONTENDER_ROLE};

SELECT format('REVOKE ALL ON FUNCTION %s FROM PUBLIC;', member.objid::regprocedure)
FROM pg_catalog.pg_depend AS member
JOIN pg_catalog.pg_extension AS extension ON extension.oid = member.refobjid
WHERE extension.extname = 'pg_prewarm'
  AND member.classid = 'pg_catalog.pg_proc'::regclass
  AND member.deptype = 'e'
\\gexec
""",
    )


def _container_document(name: str) -> dict[str, Any]:
    result = _docker("container", "inspect", name)
    return json.loads(result.stdout)[0]


def _published_port(document: dict[str, Any], port: int) -> str:
    bindings = document["NetworkSettings"]["Ports"].get(f"{port}/tcp")
    if not bindings:
        raise ContenderRuntimeError(f"container port {port} is not published on the host")
    return bindings[0]["HostPort"]


def _contender_url(document: dict[str, Any], port: int) -> str:
    return f"http://127.0.0.1:{_published_port(document, port)}/health"


def _control_database_url(document: dict[str, Any], password: str) -> str:
    port = _published_port(document, 5432)
    return (
        f"postgresql://{CONTROL_ROLE}:{password}@127.0.0.1:{port}/"
        f"{DATABASE_NAME}?sslmode=disable"
    )


def _readiness_status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as error:
        error.read()
        return error.code
    except OSError:
        return 0


def _wait_for_readiness(url: str, timeout_seconds: float = 60) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _readiness_status(url) == 204:
            return
        time.sleep(0.2)
    raise ContenderRuntimeError("Contender did not become ready within 60 seconds")


def _find_manifest(root: Path, contender_id: str) -> dict[str, Any]:
    contender = next(
        (item for item in discover_contenders(root) if item["id"] == contender_id),
        None,
    )
    if contender is None:
        raise ContenderRuntimeError(f"unknown Contender '{contender_id}'")
    return contender


def start_contender(root: Path, contender_id: str) -> dict[str, Any]:
    """Build and start a Contender with a freshly migrated PostgreSQL container."""
    root = root.resolve()
    contender = _find_manifest(root, contender_id)
    names = _resource_names(root, contender_id)
    if _container_exists(names.database) or _container_exists(names.contender):
        raise ContenderRuntimeError(
            f"Contender '{contender_id}' is already started; stop it before starting again"
        )

    config_path = (root / "database" / "postgresql.conf").resolve()
    control_password = secrets.token_urlsafe(32)
    context_path = (root / Path(contender["manifest"]).parent / contender["container"]["context"])
    dockerfile_path = context_path / contender["container"]["dockerfile"]

    try:
        _docker(
            "build",
            "--tag",
            names.image,
            "--file",
            str(dockerfile_path),
            str(context_path),
        )
        _docker(
            "network",
            "create",
            "--label",
            "dev.link-metrics.control-plane=true",
            names.network,
        )
        _docker(
            "run",
            "--detach",
            "--name",
            names.database,
            "--network",
            names.network,
            "--network-alias",
            "postgres",
            "--label",
            "dev.link-metrics.control-plane=true",
            "--env",
            f"POSTGRES_DB={DATABASE_NAME}",
            "--env",
            f"POSTGRES_USER={CONTROL_ROLE}",
            "--env",
            f"POSTGRES_PASSWORD={control_password}",
            "--volume",
            f"{config_path}:/etc/postgresql/postgresql.conf:ro",
            "--publish",
            "127.0.0.1:0:5432",
            POSTGRES_IMAGE,
            "-c",
            "config_file=/etc/postgresql/postgresql.conf",
        )
        _wait_for_postgres(names.database)
        database_document = _container_document(names.database)
        _prepare_database(
            root,
            names.database,
            _control_database_url(database_document, control_password),
        )

        migration_version = _migration_version(root)
        database_url = (
            f"postgresql://{CONTENDER_ROLE}:{CONTENDER_PASSWORD}@postgres:5432/{DATABASE_NAME}"
        )
        _docker(
            "run",
            "--detach",
            "--name",
            names.contender,
            "--network",
            names.network,
            "--label",
            "dev.link-metrics.control-plane=true",
            "--env",
            f"DATABASE_URL={database_url}",
            "--env",
            f"EXPECTED_MIGRATION_VERSION={migration_version}",
            "--env",
            f"PORT={contender['port']}",
            "--publish",
            f"127.0.0.1:0:{contender['port']}",
            names.image,
        )
        document = _container_document(names.contender)
        _wait_for_readiness(_contender_url(document, contender["port"]))
        return inspect_contender(root, contender_id)
    except Exception:
        stop_contender(root, contender_id)
        raise


def inspect_contender(root: Path, contender_id: str) -> dict[str, Any]:
    """Inspect the database and HTTP readiness without framework knowledge."""
    root = root.resolve()
    contender = _find_manifest(root, contender_id)
    names = _resource_names(root, contender_id)
    if not _container_exists(names.database) or not _container_exists(names.contender):
        raise ContenderRuntimeError(f"Contender '{contender_id}' is not started")

    database_document = _container_document(names.database)
    contender_document = _container_document(names.contender)
    url = _contender_url(contender_document, contender["port"])
    migration = _docker(
        "exec",
        names.database,
        "psql",
        "--username",
        CONTROL_ROLE,
        "--dbname",
        DATABASE_NAME,
        "--tuples-only",
        "--no-align",
        "--command",
        "SELECT version FROM public.schema_migrations ORDER BY version DESC LIMIT 1",
        check=False,
    )

    return {
        "database": {
            "container": names.database,
            "image": POSTGRES_IMAGE,
            "migrationVersion": migration.stdout.strip() if migration.returncode == 0 else None,
            "port": int(_published_port(database_document, 5432)),
            "status": database_document["State"]["Status"],
        },
        "contender": {
            "container": names.contender,
            "id": contender_id,
            "readiness": _readiness_status(url),
            "status": contender_document["State"]["Status"],
            "url": url,
        },
    }


def database_owner_connection(root: Path, contender_id: str) -> dict[str, str]:
    """Return the ephemeral owner URL to an explicit host control-plane caller."""
    root = root.resolve()
    _find_manifest(root, contender_id)
    names = _resource_names(root, contender_id)
    if not _container_exists(names.database):
        raise ContenderRuntimeError(f"Contender '{contender_id}' is not started")

    database_document = _container_document(names.database)
    password_entry = next(
        (
            value
            for value in database_document["Config"]["Env"]
            if value.startswith("POSTGRES_PASSWORD=")
        ),
        None,
    )
    if password_entry is None:
        raise ContenderRuntimeError("running PostgreSQL has no control-plane credential")
    password = password_entry.split("=", 1)[1]
    return {"url": _control_database_url(database_document, password)}


def stop_contender(root: Path, contender_id: str) -> dict[str, str]:
    """Stop and remove only the control-plane resources for one Contender."""
    names = _resource_names(root.resolve(), contender_id)
    for container in (names.contender, names.database):
        if _container_exists(container):
            _docker("stop", "--time", "5", container, check=False)
            _docker("container", "rm", "--force", "--volumes", container)
    if _network_exists(names.network):
        _docker("network", "rm", names.network)
    return {"status": "stopped"}
