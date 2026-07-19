import http.client
import json
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit


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


def read_container_logs(container: str) -> str:
    result = subprocess.run(
        ["docker", "logs", container],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout + result.stderr


def read_health(url: str) -> tuple[int, bytes, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read(), response.headers.get_content_type()
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers.get_content_type()


def register_user(
    health_url: str,
    body: bytes,
    *,
    content_type: str | None = "application/json",
) -> tuple[int, bytes, str]:
    parsed_url = urlsplit(health_url)
    headers = {} if content_type is None else {"Content-Type": content_type}
    connection = http.client.HTTPConnection(
        parsed_url.hostname,
        parsed_url.port,
        timeout=10,
    )
    try:
        connection.request("POST", "/api/auth/register", body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read(), response.headers.get_content_type()
    finally:
        connection.close()


@contextmanager
def running_contender() -> Iterator[dict]:
    run_control_plane("contenders", "stop", CONTENDER_ID)

    try:
        started = run_control_plane("contenders", "start", CONTENDER_ID)
        assert started.returncode == 0, started.stderr
        yield json.loads(started.stdout)
    finally:
        stopped = run_control_plane("contenders", "stop", CONTENDER_ID)
        assert stopped.returncode == 0, stopped.stderr


def test_registration_returns_the_canonical_user_through_the_container_seam() -> None:
    with running_contender() as state:
        status, body, content_type = register_user(
            state["contender"]["url"],
            b'{"email":"User@Example.com","password":"benchmark-password"}',
        )

        assert status == 201
        assert content_type == "application/json"
        user = json.loads(body)
        assert set(user) == {"id", "email", "createdAt"}
        assert user["email"] == "user@example.com"
        assert uuid.UUID(user["id"]).version == 7
        assert datetime.fromisoformat(user["createdAt"]).isoformat(timespec="milliseconds").endswith(
            "+00:00"
        )
        assert user["createdAt"].endswith("Z")
        assert len(user["createdAt"].rsplit(".", 1)[1].removesuffix("Z")) == 3


def test_registration_rejects_a_duplicate_canonical_email() -> None:
    with running_contender() as state:
        health_url = state["contender"]["url"]
        first_status, _, _ = register_user(
            health_url,
            b'{"email":"duplicate@example.com","password":"first-password"}',
        )
        status, body, content_type = register_user(
            health_url,
            b'{"email":"DUPLICATE@EXAMPLE.COM","password":"second-password"}',
        )

        assert first_status == 201
        assert (status, body, content_type) == (
            409,
            b'{"error":"conflict"}',
            "application/json",
        )


def test_registration_enforces_the_contract_request_parser() -> None:
    with running_contender() as state:
        health_url = state["contender"]["url"]
        valid_body = b'{"email":"parser@example.com","password":"benchmark-password"}'

        accepted = register_user(
            health_url,
            valid_body,
            content_type="application/json; charset=UTF-8",
        )
        missing = register_user(health_url, valid_body, content_type=None)
        unsupported = register_user(health_url, valid_body, content_type="text/plain")
        unsupported_charset = register_user(
            health_url,
            valid_body,
            content_type="application/json; charset=iso-8859-1",
        )
        malformed = register_user(
            health_url,
            b'{"email":"parser@example.com",',
        )
        limit_body = b'{"email":"limit@example.com","password":"benchmark-password"}'
        exactly_at_limit = register_user(
            health_url,
            limit_body + (b" " * (4_096 - len(limit_body))),
        )
        oversized = register_user(health_url, limit_body + (b" " * (4_097 - len(limit_body))))

        assert accepted[0] == 201
        assert missing == (
            415,
            b'{"error":"unsupported_media_type"}',
            "application/json",
        )
        assert unsupported == missing
        assert unsupported_charset == missing
        assert malformed == (400, b'{"error":"invalid_json"}', "application/json")
        assert exactly_at_limit[0] == 201
        assert oversized == (
            413,
            b'{"error":"payload_too_large"}',
            "application/json",
        )


def test_registration_returns_sorted_code_only_validation_errors() -> None:
    invalid_cases = [
        ({}, [{"field": "email", "code": "required"}, {"field": "password", "code": "required"}]),
        (
            {"extra": True},
            [
                {"field": "body", "code": "unknown"},
                {"field": "email", "code": "required"},
                {"field": "password", "code": "required"},
            ],
        ),
        ([], [{"field": "body", "code": "invalid"}]),
        ("credentials", [{"field": "body", "code": "invalid"}]),
        (None, [{"field": "body", "code": "invalid"}]),
        (
            {"email": " user@example.com", "password": "benchmark-password"},
            [{"field": "email", "code": "invalid"}],
        ),
        (
            {"email": "usér@example.com", "password": "benchmark-password"},
            [{"field": "email", "code": "invalid"}],
        ),
        (
            {"email": ("a" * 243) + "@example.com", "password": "benchmark-password"},
            [{"field": "email", "code": "invalid"}],
        ),
        (
            {"email": "user@example.com", "password": "seven77"},
            [{"field": "password", "code": "invalid"}],
        ),
        (
            {"email": "user@example.com", "password": "x" * 129},
            [{"field": "password", "code": "invalid"}],
        ),
        (
            {"email": "user@example.com", "password": "line\nbreak"},
            [{"field": "password", "code": "invalid"}],
        ),
    ]

    with running_contender() as state:
        health_url = state["contender"]["url"]
        for request_body, details in invalid_cases:
            actual = register_user(
                health_url,
                json.dumps(request_body, separators=(",", ":")).encode(),
            )
            expected_body = json.dumps(
                {"error": "invalid_request", "details": details},
                separators=(",", ":"),
            ).encode()
            assert actual == (400, expected_body, "application/json"), request_body


def test_database_enforces_canonical_user_email_persistence() -> None:
    with running_contender() as state:
        database_container = state["database"]["container"]
        invalid_emails = [
            "Mixed@Example.com",
            " user@example.com",
            "usér@example.com",
            ("a" * 243) + "@example.com",
            "not-an-email",
        ]
        for email in invalid_emails:
            sql_email = email.replace("'", "''")
            inserted = run_psql(
                database_container,
                "INSERT INTO public.users (email, password_hash) "
                f"VALUES ('{sql_email}', 'encoded-hash')",
            )
            assert inserted.returncode != 0, email

        canonical = run_psql(
            database_container,
            "INSERT INTO public.users (email, password_hash) "
            "VALUES ('canonical@example.com', 'encoded-hash')",
        )
        backtick = run_psql(
            database_container,
            "INSERT INTO public.users (email, password_hash) "
            "VALUES ('tick`mark@example.com', 'encoded-hash')",
        )
        duplicate = run_psql(
            database_container,
            "INSERT INTO public.users (email, password_hash) "
            "VALUES ('canonical@example.com', 'another-hash')",
        )

        assert canonical.returncode == 0, canonical.stderr
        assert backtick.returncode == 0, backtick.stderr
        assert duplicate.returncode != 0


def test_registration_accepts_exact_email_and_password_boundaries() -> None:
    with running_contender() as state:
        health_url = state["contender"]["url"]
        longest_email = ("A" * 242) + "@EXAMPLE.COM"

        registrations = [
            {"email": longest_email, "password": " " * 8},
            {"email": "max-password@example.com", "password": "~" * 128},
        ]
        responses = [
            register_user(
                health_url,
                json.dumps(credentials, separators=(",", ":")).encode(),
            )
            for credentials in registrations
        ]

        assert [response[0] for response in responses] == [201, 201]
        assert json.loads(responses[0][1])["email"] == longest_email.lower()


def test_registration_uses_one_autocommit_read_committed_statement() -> None:
    with running_contender() as state:
        health_url = state["contender"]["url"]
        database_container = state["database"]["container"]

        isolation = run_psql(database_container, "SHOW default_transaction_isolation")
        logging = run_psql(database_container, "ALTER SYSTEM SET log_statement = 'all'")
        reloaded = run_psql(database_container, "SELECT pg_reload_conf()")
        disconnected = run_psql(
            database_container,
            "SELECT pg_terminate_backend(pid) FROM pg_catalog.pg_stat_activity "
            "WHERE usename = 'link_metrics_contender'",
        )
        assert isolation.returncode == 0, isolation.stderr
        assert isolation.stdout.strip() == "read committed"
        assert logging.returncode == 0, logging.stderr
        assert reloaded.returncode == 0, reloaded.stderr
        assert disconnected.returncode == 0, disconnected.stderr

        logs_before = read_container_logs(database_container)
        response = register_user(
            health_url,
            b'{"email":"one-statement@example.com","password":"benchmark-password"}',
        )
        logs_after = read_container_logs(database_container)

        assert response[0] == 201
        assert logs_after.startswith(logs_before)
        request_logs = logs_after[len(logs_before) :]
        statement_lines = [
            line
            for line in request_logs.splitlines()
            if "statement:" in line or "execute <unnamed>:" in line
        ]
        assert len(statement_lines) == 1, request_logs
        assert "INSERT INTO users" in request_logs
        assert "RETURNING" in request_logs
        assert "statement: BEGIN" not in request_logs
        assert "statement: COMMIT" not in request_logs


def test_control_plane_manages_the_express_contender_through_its_container_seam() -> None:
    run_control_plane("contenders", "stop", CONTENDER_ID)

    try:
        started = run_control_plane("contenders", "start", CONTENDER_ID)

        assert started.returncode == 0, started.stderr
        start_state = json.loads(started.stdout)
        assert start_state["database"]["status"] == "running"
        assert start_state["database"]["migrationVersion"] == "20260719000100"
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
            "SELECT version FROM public.schema_migrations ORDER BY version DESC LIMIT 1",
            contender_role=True,
        )
        assert migration_read.returncode == 0, migration_read.stderr
        assert migration_read.stdout.strip() == "20260719000100"

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
            "UPDATE public.schema_migrations SET version = 'unexpected' "
            "WHERE version = '20260719000100'",
        )
        assert drift.returncode == 0, drift.stderr
        assert read_health(url) == (503, b'{"error":"unavailable"}', "application/json")

        restored = run_psql(
            database_container,
            "UPDATE public.schema_migrations SET version = '20260719000100' "
            "WHERE version = 'unexpected'",
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
