import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONTENDER_ID = "express-node"


def run_control_plane(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "link_metrics", *arguments, "--root", str(REPOSITORY_ROOT)],
        check=False,
        capture_output=True,
        text=True,
    )


def run_psql(
    database_container: str,
    sql: str,
    *,
    contender_role: bool = False,
) -> subprocess.CompletedProcess[str]:
    arguments = ["docker", "exec"]
    if contender_role:
        arguments.extend(["--env", "PGPASSWORD=benchmark-contender-only"])
    arguments.extend(
        [
            database_container,
            "psql",
            "--host",
            "127.0.0.1",
            "--username",
            "link_metrics_contender" if contender_role else "link_metrics_control",
            "--dbname",
            "link_metrics",
            "--tuples-only",
            "--no-align",
            "--command",
            sql,
        ]
    )
    return subprocess.run(arguments, check=False, capture_output=True, text=True)


def read_health(url: str) -> tuple[int, bytes, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read(), response.headers.get_content_type()
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers.get_content_type()


def test_control_plane_manages_the_express_contender_through_its_container_seam() -> None:
    run_control_plane("contenders", "stop", CONTENDER_ID)

    try:
        started = run_control_plane("contenders", "start", CONTENDER_ID)

        assert started.returncode == 0, started.stderr
        start_state = json.loads(started.stdout)
        assert start_state["database"]["status"] == "running"
        assert start_state["database"]["migrationVersion"] == "20260604222601"
        assert isinstance(start_state["database"]["port"], int)
        assert start_state["contender"]["id"] == CONTENDER_ID
        assert start_state["contender"]["status"] == "running"
        assert start_state["contender"]["readiness"] == 204

        owner_connection = run_control_plane("contenders", "database-url", CONTENDER_ID)
        assert owner_connection.returncode == 0, owner_connection.stderr
        owner_url = json.loads(owner_connection.stdout)["url"]
        assert "link_metrics_control" in owner_url
        assert "benchmark-control-only" not in owner_url

        with urllib.request.urlopen(start_state["contender"]["url"], timeout=5) as response:
            assert response.status == 204
            assert response.read() == b""

        inspected = run_control_plane("contenders", "inspect", CONTENDER_ID)

        assert inspected.returncode == 0, inspected.stderr
        inspect_state = json.loads(inspected.stdout)
        assert inspect_state["database"]["status"] == "running"
        assert inspect_state["contender"]["status"] == "running"
        assert inspect_state["contender"]["readiness"] == 204
    finally:
        stopped = run_control_plane("contenders", "stop", CONTENDER_ID)

    assert stopped.returncode == 0, stopped.stderr
    assert json.loads(stopped.stdout)["status"] == "stopped"


def test_database_role_and_extension_boundaries_are_mechanically_enforced() -> None:
    run_control_plane("contenders", "stop", CONTENDER_ID)

    try:
        started = run_control_plane("contenders", "start", CONTENDER_ID)
        assert started.returncode == 0, started.stderr
        state = json.loads(started.stdout)
        database_container = state["database"]["container"]

        migration_read = run_psql(
            database_container,
            "SELECT version FROM public.schema_migrations",
            contender_role=True,
        )
        assert migration_read.returncode == 0, migration_read.stderr
        assert migration_read.stdout.strip() == "20260604222601"

        database_address = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                database_container,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert database_address.returncode == 0, database_address.stderr
        leaked_control_credential = subprocess.run(
            [
                "docker",
                "exec",
                "--env",
                "PGPASSWORD=benchmark-control-only",
                database_container,
                "psql",
                "--host",
                database_address.stdout.strip(),
                "--username",
                "link_metrics_control",
                "--dbname",
                "link_metrics",
                "--command",
                "SELECT 1",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert leaked_control_credential.returncode != 0

        prohibited_statements = [
            "CREATE TABLE public.forbidden (id integer)",
            "INSERT INTO public.schema_migrations (version) VALUES ('forbidden')",
            "UPDATE public.users SET email = email",
            "DELETE FROM public.users",
            "CREATE EXTENSION hstore",
            "SELECT pg_catalog.pg_prewarm('public.users'::regclass)",
        ]
        for statement in prohibited_statements:
            result = run_psql(database_container, statement, contender_role=True)
            assert result.returncode != 0, statement

        extensions = run_psql(
            database_container,
            "SELECT string_agg(extname, ',' ORDER BY extname) FROM pg_catalog.pg_extension",
        )
        assert extensions.returncode == 0, extensions.stderr
        assert extensions.stdout.strip() == "pg_prewarm"

        settings = run_psql(
            database_container,
            "SELECT current_setting('fsync'), current_setting('full_page_writes'), "
            "current_setting('synchronous_commit'), current_setting('autovacuum'), "
            "current_setting('shared_preload_libraries')",
        )
        assert settings.returncode == 0, settings.stderr
        assert settings.stdout.strip() == "on|on|on|on|"

        version = run_psql(database_container, "SELECT current_setting('server_version_num')")
        assert version.returncode == 0, version.stderr
        assert version.stdout.strip() == "180004"
    finally:
        stopped = run_control_plane("contenders", "stop", CONTENDER_ID)

    assert stopped.returncode == 0, stopped.stderr


def test_readiness_reports_migration_drift_and_database_loss() -> None:
    run_control_plane("contenders", "stop", CONTENDER_ID)

    try:
        started = run_control_plane("contenders", "start", CONTENDER_ID)
        assert started.returncode == 0, started.stderr
        state = json.loads(started.stdout)
        database_container = state["database"]["container"]
        url = state["contender"]["url"]

        drift = run_psql(
            database_container,
            "UPDATE public.schema_migrations SET version = 'unexpected'",
        )
        assert drift.returncode == 0, drift.stderr
        assert read_health(url) == (503, b'{"error":"unavailable"}', "application/json")

        restored = run_psql(
            database_container,
            "UPDATE public.schema_migrations SET version = '20260604222601'",
        )
        assert restored.returncode == 0, restored.stderr
        assert read_health(url) == (204, b"", "text/plain")

        stopped_database = subprocess.run(
            ["docker", "stop", "--time", "1", database_container],
            check=False,
            capture_output=True,
            text=True,
        )
        assert stopped_database.returncode == 0, stopped_database.stderr
        assert read_health(url) == (503, b'{"error":"unavailable"}', "application/json")
    finally:
        stopped = run_control_plane("contenders", "stop", CONTENDER_ID)

    assert stopped.returncode == 0, stopped.stderr
