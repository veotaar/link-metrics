import base64
import hashlib
import hmac
import http.client
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONTENDER_ID = os.environ.get("LINK_METRICS_TEST_CONTENDER", "express-node")
PUBLIC_BENCHMARK_JWT_KEY = (
    REPOSITORY_ROOT / "benchmark" / "fixtures" / "jwt-hs256.key"
).read_bytes().strip()
assert len(PUBLIC_BENCHMARK_JWT_KEY) == 32


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
    contender_url: str,
    body: bytes,
    *,
    content_type: str | None = "application/json",
) -> tuple[int, bytes, str]:
    headers = {} if content_type is None else {"Content-Type": content_type}
    return request_api(
        contender_url,
        "POST",
        "/api/auth/register",
        body=body,
        headers=headers,
    )


def request_api(
    contender_url: str,
    method: str,
    path: str,
    *,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes, str]:
    parsed_url = urlsplit(contender_url)
    connection = http.client.HTTPConnection(
        parsed_url.hostname,
        parsed_url.port,
        timeout=10,
    )
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read(), response.headers.get_content_type()
    finally:
        connection.close()


def login_user(contender_url: str, email: str, password: str) -> tuple[int, bytes, str]:
    return request_api(
        contender_url,
        "POST",
        "/api/auth/login",
        body=json.dumps(
            {"email": email, "password": password},
            separators=(",", ":"),
        ).encode(),
        headers={"Content-Type": "application/json"},
    )


def create_short_link(
    contender_url: str,
    token: str,
    body: bytes,
) -> tuple[int, bytes, str]:
    return request_api(
        contender_url,
        "POST",
        "/api/links",
        body=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )


def resolve_short_link(contender_url: str, short_code: str) -> tuple[int, bytes, str | None]:
    parsed_url = urlsplit(contender_url)
    connection = http.client.HTTPConnection(
        parsed_url.hostname,
        parsed_url.port,
        timeout=10,
    )
    try:
        connection.request("GET", f"/{short_code}")
        response = connection.getresponse()
        return response.status, response.read(), response.headers.get("Location")
    finally:
        connection.close()


def get_short_link_stats(
    contender_url: str,
    token: str,
    short_code: str,
) -> tuple[int, bytes, str]:
    return request_api(
        contender_url,
        "GET",
        f"/api/links/{short_code}/stats",
        headers={"Authorization": f"Bearer {token}"},
    )


def create_short_link_for_new_user(
    contender_url: str,
    *,
    email: str,
    destination: str,
) -> tuple[str, str]:
    registration = register_user(
        contender_url,
        json.dumps(
            {"email": email, "password": "benchmark-password"},
            separators=(",", ":"),
        ).encode(),
    )
    login = login_user(contender_url, email, "benchmark-password")
    assert registration[0] == 201
    assert login[0] == 200
    token = json.loads(login[1])["token"]
    creation = create_short_link(
        contender_url,
        token,
        json.dumps({"url": destination}, separators=(",", ":")).encode(),
    )
    assert creation[0] == 201
    return token, json.loads(creation[1])["shortCode"]


def decode_jwt_part(part: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(part + ("=" * (-len(part) % 4))))


def encode_jwt(
    claims: dict,
    *,
    header: dict | None = None,
    signature_algorithm: str = "sha256",
    alter_signature: bool = False,
) -> str:
    encoded_header = base64.urlsafe_b64encode(
        json.dumps(
            header if header is not None else {"alg": "HS256", "typ": "JWT"},
            separators=(",", ":"),
        ).encode()
    ).rstrip(b"=")
    encoded_claims = base64.urlsafe_b64encode(
        json.dumps(claims, separators=(",", ":")).encode()
    ).rstrip(b"=")
    signing_input = encoded_header + b"." + encoded_claims
    signature = hmac.new(PUBLIC_BENCHMARK_JWT_KEY, signing_input, signature_algorithm).digest()
    if alter_signature:
        signature = bytes([signature[0] ^ 1]) + signature[1:]
    return b".".join(
        [encoded_header, encoded_claims, base64.urlsafe_b64encode(signature).rstrip(b"=")]
    ).decode()


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


@contextmanager
def holding_links_table_lock(
    database_container: str,
    *,
    duration_seconds: int,
) -> Iterator[None]:
    lock_process = subprocess.Popen(
        [
            "docker",
            "exec",
            database_container,
            "psql",
            "--host",
            "127.0.0.1",
            "--username",
            "link_metrics_control",
            "--dbname",
            "link_metrics",
            "--command",
            "BEGIN; LOCK TABLE public.links IN ACCESS EXCLUSIVE MODE; "
            f"SELECT pg_sleep({duration_seconds}); COMMIT;",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lock_deadline = time.monotonic() + 2
    while time.monotonic() < lock_deadline:
        lock_state = run_psql(
            database_container,
            "SELECT count(*) FROM pg_catalog.pg_locks "
            "WHERE relation = 'public.links'::regclass "
            "AND mode = 'AccessExclusiveLock' AND granted",
        )
        if lock_state.returncode == 0 and lock_state.stdout.strip() == "1":
            break
        time.sleep(0.02)
    else:
        lock_process.terminate()
        lock_process.communicate(timeout=5)
        raise AssertionError("failed to acquire the links table lock")

    try:
        yield
    finally:
        lock_stdout, lock_stderr = lock_process.communicate(timeout=duration_seconds + 5)
        assert lock_process.returncode == 0, lock_stdout + lock_stderr


def test_short_link_creation_returns_owned_deterministic_short_links() -> None:
    with running_contender() as state:
        contender_url = state["contender"]["url"]
        users = []
        for index in range(2):
            email = f"owner-{index}@example.com"
            registration = register_user(
                contender_url,
                json.dumps(
                    {"email": email, "password": "benchmark-password"},
                    separators=(",", ":"),
                ).encode(),
            )
            login = login_user(contender_url, email, "benchmark-password")
            assert registration[0] == 201
            assert login[0] == 200
            users.append((json.loads(registration[1]), json.loads(login[1])["token"]))

        destinations = [
            "https://Example.COM/a%2Fb?source=Benchmark#Result",
            "http://127.0.0.1:8080/path",
        ]
        responses = [
            create_short_link(
                contender_url,
                token,
                json.dumps({"url": destination}, separators=(",", ":")).encode(),
            )
            for (_, token), destination in zip(users, destinations, strict=True)
        ]

        assert [(response[0], response[2]) for response in responses] == [
            (201, "application/json"),
            (201, "application/json"),
        ]
        short_links = [json.loads(response[1]) for response in responses]
        assert [short_link["shortCode"] for short_link in short_links] == [
            "00000001",
            "00000002",
        ]
        for short_link, (user, _), destination in zip(
            short_links, users, destinations, strict=True
        ):
            assert set(short_link) == {"userId", "shortCode", "originalUrl", "createdAt"}
            assert short_link["userId"] == user["id"]
            assert short_link["originalUrl"] == destination
            assert re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z",
                short_link["createdAt"],
            )
            assert datetime.fromisoformat(short_link["createdAt"]).isoformat(
                timespec="milliseconds"
            ).endswith("+00:00")


def test_short_link_creation_requires_authentication_before_body_processing() -> None:
    with running_contender() as state:
        contender_url = state["contender"]["url"]
        missing = request_api(contender_url, "POST", "/api/links")
        invalid = request_api(
            contender_url,
            "POST",
            "/api/links",
            headers={"Authorization": "Bearer not-a-token"},
        )

        assert missing == (401, b'{"error":"unauthorized"}', "application/json")
        assert invalid == missing


def test_short_link_creation_enforces_the_destination_contract() -> None:
    with running_contender() as state:
        contender_url = state["contender"]["url"]
        registration = register_user(
            contender_url,
            b'{"email":"url-owner@example.com","password":"benchmark-password"}',
        )
        login = login_user(contender_url, "url-owner@example.com", "benchmark-password")
        assert registration[0] == 201
        assert login[0] == 200
        token = json.loads(login[1])["token"]

        maximum_prefix = "https://example.com/"
        maximum_destination = maximum_prefix + ("a" * (2_048 - len(maximum_prefix)))
        accepted_destinations = ["http://a", maximum_destination]
        accepted = [
            create_short_link(
                contender_url,
                token,
                json.dumps({"url": destination}, separators=(",", ":")).encode(),
            )
            for destination in accepted_destinations
        ]

        invalid_destinations = [
            "ftp://example.com",
            "https://user@example.com/path",
            "https:///missing-host",
            maximum_destination + "a",
            "https://example.com/non-ascii-é",
            "https://example.com/path with-space",
        ]
        rejected = [
            create_short_link(
                contender_url,
                token,
                json.dumps({"url": destination}, separators=(",", ":")).encode(),
            )
            for destination in invalid_destinations
        ]
        unknown = create_short_link(
            contender_url,
            token,
            b'{"url":"https://example.com","customCode":"not-allowed"}',
        )
        missing = create_short_link(contender_url, token, b"{}")

        assert [response[0] for response in accepted] == [201, 201]
        assert [json.loads(response[1])["originalUrl"] for response in accepted] == (
            accepted_destinations
        )
        assert rejected == [
            (
                400,
                b'{"error":"invalid_request","details":[{"field":"url","code":"invalid"}]}',
                "application/json",
            )
        ] * len(invalid_destinations)
        assert unknown == (
            400,
            b'{"error":"invalid_request","details":[{"field":"body","code":"unknown"}]}',
            "application/json",
        )
        assert missing == (
            400,
            b'{"error":"invalid_request","details":[{"field":"url","code":"required"}]}',
            "application/json",
        )


def test_short_link_constraints_match_the_api_destination_boundary() -> None:
    with running_contender() as state:
        database_container = state["database"]["container"]
        inserted_user = run_psql(
            database_container,
            "INSERT INTO public.users (email, password_hash) "
            "VALUES ('database-owner@example.com', 'unused-hash') RETURNING id",
        )
        assert inserted_user.returncode == 0, inserted_user.stderr
        user_id = inserted_user.stdout.splitlines()[0]

        accepted = run_psql(
            database_container,
            "INSERT INTO public.links (original_url, user_id) "
            f"VALUES ('http://a', '{user_id}') "
            "RETURNING short_code, original_url",
        )
        assert accepted.returncode == 0, accepted.stderr
        assert accepted.stdout.splitlines()[0] == "00000001|http://a"

        maximum = run_psql(
            database_container,
            "INSERT INTO public.links (short_code, original_url, user_id) "
            f"VALUES ('0000000D', 'https://example.com/' || repeat('a', 2028), '{user_id}') "
            "RETURNING short_code, octet_length(original_url)",
        )
        assert maximum.returncode == 0, maximum.stderr
        assert maximum.stdout.splitlines()[0] == "0000000D|2048"

        alphabet = run_psql(
            database_container,
            "SELECT string_agg(public.short_code_from_sequence(value), ',' ORDER BY value) "
            "FROM unnest(ARRAY[0, 9, 10, 35, 36, 61, 62, 3843, 3844, "
            "218340105584895]::bigint[]) AS value",
        )
        assert alphabet.returncode == 0, alphabet.stderr
        assert alphabet.stdout.strip() == (
            "00000000,00000009,0000000A,0000000Z,0000000a,0000000z,"
            "00000010,000000zz,00000100,zzzzzzzz"
        )

        rejected_statements = [
            "INSERT INTO public.links (short_code, original_url, user_id) "
            f"VALUES ('0000000!', 'https://example.com', '{user_id}')",
            "INSERT INTO public.links (short_code, original_url, user_id) "
            f"VALUES ('000000D', 'https://example.com', '{user_id}')",
            "INSERT INTO public.links (short_code, original_url, user_id) "
            f"VALUES ('0000000A', 'ftp://example.com', '{user_id}')",
            "INSERT INTO public.links (short_code, original_url, user_id) "
            f"VALUES ('0000000B', 'https://user@example.com', '{user_id}')",
            "INSERT INTO public.links (short_code, original_url, user_id) "
            f"VALUES ('0000000E', 'https://a/' || repeat('a', 2039), '{user_id}')",
            "INSERT INTO public.links (short_code, original_url, user_id) "
            f"VALUES ('0000000F', 'https:///missing-host', '{user_id}')",
            "INSERT INTO public.links (short_code, original_url, user_id) "
            f"VALUES ('0000000G', 'https://example.com/non-ascii-é', '{user_id}')",
            "INSERT INTO public.links (short_code, original_url, user_id) "
            f"VALUES ('0000000H', 'https://example.com/path with-space', '{user_id}')",
            "INSERT INTO public.links (short_code, original_url, user_id, click_count) "
            f"VALUES ('0000000C', 'https://example.com', '{user_id}', -1)",
        ]
        rejected = [run_psql(database_container, statement) for statement in rejected_statements]
        assert all(result.returncode != 0 for result in rejected)


def test_short_link_analytics_columns_preserve_the_database_invariants() -> None:
    with running_contender() as state:
        database_container = state["database"]["container"]
        columns = run_psql(
            database_container,
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'links' "
            "AND column_name IN ('click_count', 'last_clicked_at') ORDER BY column_name",
        )
        assert columns.returncode == 0, columns.stderr
        assert columns.stdout.strip().splitlines() == [
            "click_count|bigint|NO",
            "last_clicked_at|timestamp with time zone|YES",
        ]

        inserted_user = run_psql(
            database_container,
            "INSERT INTO public.users (email, password_hash) "
            "VALUES ('bigint-clicks@example.com', 'unused-hash') RETURNING id",
        )
        assert inserted_user.returncode == 0, inserted_user.stderr
        user_id = inserted_user.stdout.splitlines()[0]
        large_count = run_psql(
            database_container,
            "INSERT INTO public.links (original_url, click_count, user_id) "
            f"VALUES ('https://example.com/bigint', 2147483648, '{user_id}') "
            "RETURNING click_count, last_clicked_at IS NULL",
        )
        assert large_count.returncode == 0, large_count.stderr
        assert large_count.stdout.splitlines()[0] == "2147483648|t"


def test_short_link_creation_uses_one_autocommit_read_committed_statement() -> None:
    with running_contender() as state:
        contender_url = state["contender"]["url"]
        database_container = state["database"]["container"]
        registration = register_user(
            contender_url,
            b'{"email":"link-statement@example.com","password":"benchmark-password"}',
        )
        login = login_user(contender_url, "link-statement@example.com", "benchmark-password")
        assert registration[0] == 201
        assert login[0] == 200

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
        response = create_short_link(
            contender_url,
            json.loads(login[1])["token"],
            b'{"url":"https://example.com/one-statement"}',
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
        assert 'insert into "links"' in request_logs
        assert "returning" in request_logs
        assert "statement: BEGIN" not in request_logs
        assert "statement: COMMIT" not in request_logs


def test_short_link_resolution_commits_the_first_click_before_redirecting() -> None:
    with running_contender() as state:
        contender_url = state["contender"]["url"]
        database_container = state["database"]["container"]
        destination = "https://Example.COM/a%2Fb?source=Resolution#Result"
        _, short_code = create_short_link_for_new_user(
            contender_url,
            email="resolution@example.com",
            destination=destination,
        )

        before = run_psql(
            database_container,
            "SELECT click_count, last_clicked_at IS NULL FROM public.links "
            f"WHERE short_code = '{short_code}'",
        )
        assert before.returncode == 0, before.stderr
        assert before.stdout.strip() == "0|t"

        with ThreadPoolExecutor(max_workers=1) as executor:
            with holding_links_table_lock(database_container, duration_seconds=1):
                pending_response = executor.submit(resolve_short_link, contender_url, short_code)
                blocked_deadline = time.monotonic() + 1
                while time.monotonic() < blocked_deadline:
                    blocked = run_psql(
                        database_container,
                        "SELECT count(*) FROM pg_catalog.pg_stat_activity "
                        "WHERE usename = 'link_metrics_contender' "
                        "AND wait_event_type = 'Lock' AND query LIKE '%UPDATE links%'",
                    )
                    if blocked.returncode == 0 and blocked.stdout.strip() == "1":
                        break
                    time.sleep(0.02)
                else:
                    raise AssertionError("resolution update did not wait for the links lock")
                assert not pending_response.done()
            response = pending_response.result(timeout=5)

        assert response == (302, b"", destination)
        after = run_psql(
            database_container,
            "SELECT click_count, last_clicked_at FROM public.links "
            f"WHERE short_code = '{short_code}'",
        )
        assert after.returncode == 0, after.stderr
        click_count, last_clicked_at = after.stdout.strip().split("|")
        assert click_count == "1"
        assert datetime.fromisoformat(last_clicked_at).utcoffset().total_seconds() == 0


def test_short_link_resolution_returns_canonical_not_found_for_a_missing_code() -> None:
    with running_contender() as state:
        contender_url = state["contender"]["url"]
        missing = resolve_short_link(contender_url, "zzzzzzzz")
        invalid = resolve_short_link(contender_url, "not-a-code")

        assert missing == (404, b'{"error":"not_found"}', None)
        assert invalid == missing


def test_short_link_resolution_failure_never_redirects_or_changes_analytics() -> None:
    with running_contender() as state:
        contender_url = state["contender"]["url"]
        database_container = state["database"]["container"]
        _, short_code = create_short_link_for_new_user(
            contender_url,
            email="failed-resolution@example.com",
            destination="https://example.com/database-failure",
        )
        revoked = run_psql(
            database_container,
            "REVOKE UPDATE ON TABLE public.links FROM link_metrics_contender",
        )
        assert revoked.returncode == 0, revoked.stderr

        response = resolve_short_link(contender_url, short_code)

        assert response == (503, b'{"error":"unavailable"}', None)
        analytics = run_psql(
            database_container,
            "SELECT click_count, last_clicked_at IS NULL FROM public.links "
            f"WHERE short_code = '{short_code}'",
        )
        assert analytics.returncode == 0, analytics.stderr
        assert analytics.stdout.strip() == "0|t"


def test_short_link_resolution_timeout_never_redirects_or_retries() -> None:
    with running_contender() as state:
        contender_url = state["contender"]["url"]
        database_container = state["database"]["container"]
        _, short_code = create_short_link_for_new_user(
            contender_url,
            email="timed-resolution@example.com",
            destination="https://example.com/database-timeout",
        )
        logging = run_psql(database_container, "ALTER SYSTEM SET log_statement = 'all'")
        reloaded = run_psql(database_container, "SELECT pg_reload_conf()")
        assert logging.returncode == 0, logging.stderr
        assert reloaded.returncode == 0, reloaded.stderr

        with holding_links_table_lock(database_container, duration_seconds=5):
            logs_before = read_container_logs(database_container)
            request_started = time.monotonic()
            response = resolve_short_link(contender_url, short_code)
            request_elapsed = time.monotonic() - request_started
        logs_after = read_container_logs(database_container)

        assert response == (503, b'{"error":"unavailable"}', None)
        assert 1.5 <= request_elapsed < 3.5
        assert logs_after.startswith(logs_before)
        request_logs = logs_after[len(logs_before) :]
        assert request_logs.count("UPDATE links") == 1, request_logs
        analytics = run_psql(
            database_container,
            "SELECT click_count, last_clicked_at IS NULL FROM public.links "
            f"WHERE short_code = '{short_code}'",
        )
        assert analytics.returncode == 0, analytics.stderr
        assert analytics.stdout.strip() == "0|t"


def test_short_link_resolution_uses_one_atomic_autocommit_statement() -> None:
    with running_contender() as state:
        contender_url = state["contender"]["url"]
        database_container = state["database"]["container"]
        destination = "https://example.com/one-resolution-statement"
        _, short_code = create_short_link_for_new_user(
            contender_url,
            email="resolution-statement@example.com",
            destination=destination,
        )

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
        response = resolve_short_link(contender_url, short_code)
        logs_after = read_container_logs(database_container)

        assert response == (302, b"", destination)
        assert logs_after.startswith(logs_before)
        request_logs = logs_after[len(logs_before) :]
        statement_lines = [
            line
            for line in request_logs.splitlines()
            if "statement:" in line or "execute <unnamed>:" in line
        ]
        assert len(statement_lines) == 1, request_logs
        assert "UPDATE links" in request_logs
        assert "click_count = click_count + 1" in request_logs
        assert "last_clicked_at = clock_timestamp()" in request_logs
        assert "RETURNING original_url" in request_logs
        assert "statement: BEGIN" not in request_logs
        assert "statement: COMMIT" not in request_logs


def test_concurrent_short_link_resolutions_account_for_every_click() -> None:
    with running_contender() as state:
        contender_url = state["contender"]["url"]
        database_container = state["database"]["container"]
        destination = "https://example.com/viral-resolution"
        _, short_code = create_short_link_for_new_user(
            contender_url,
            email="viral-resolution@example.com",
            destination=destination,
        )
        request_count = 20
        start = threading.Barrier(request_count)

        def resolve_at_once(_index: int) -> tuple[int, bytes, str | None]:
            start.wait(timeout=5)
            return resolve_short_link(contender_url, short_code)

        with ThreadPoolExecutor(max_workers=request_count) as executor:
            responses = list(executor.map(resolve_at_once, range(request_count)))

        assert responses == [(302, b"", destination)] * request_count
        analytics = run_psql(
            database_container,
            "SELECT click_count, last_clicked_at IS NOT NULL FROM public.links "
            f"WHERE short_code = '{short_code}'",
        )
        assert analytics.returncode == 0, analytics.stderr
        assert analytics.stdout.strip() == f"{request_count}|t"


def test_short_link_owner_reads_never_clicked_statistics() -> None:
    with running_contender() as state:
        contender_url = state["contender"]["url"]
        destination = "https://Example.COM/a%2Fb?source=Statistics#Result"
        token, short_code = create_short_link_for_new_user(
            contender_url,
            email="statistics-owner@example.com",
            destination=destination,
        )

        statistics = get_short_link_stats(contender_url, token, short_code)

        assert statistics[0] == 200
        assert statistics[2] == "application/json"
        assert json.loads(statistics[1]) == {
            "shortCode": short_code,
            "originalUrl": destination,
            "clickCount": 0,
            "lastClickedAt": None,
        }


def test_short_link_statistics_hide_non_owned_and_missing_codes_identically() -> None:
    with running_contender() as state:
        contender_url = state["contender"]["url"]
        owner_token, short_code = create_short_link_for_new_user(
            contender_url,
            email="stats-owner@example.com",
            destination="https://example.com/private-statistics",
        )
        other_token, _ = create_short_link_for_new_user(
            contender_url,
            email="stats-other@example.com",
            destination="https://example.com/other-statistics",
        )

        non_owned = get_short_link_stats(contender_url, other_token, short_code)
        missing = get_short_link_stats(contender_url, owner_token, "zzzzzzzz")

        assert non_owned == (404, b'{"error":"not_found"}', "application/json")
        assert missing == non_owned


def test_short_link_statistics_report_the_exact_click_transition() -> None:
    with running_contender() as state:
        contender_url = state["contender"]["url"]
        destination = "https://example.com/exact-click-transition"
        token, short_code = create_short_link_for_new_user(
            contender_url,
            email="clicked-stats@example.com",
            destination=destination,
        )
        assert resolve_short_link(contender_url, short_code) == (302, b"", destination)
        assert resolve_short_link(contender_url, short_code) == (302, b"", destination)

        statistics = get_short_link_stats(contender_url, token, short_code)

        assert statistics[0] == 200
        body = json.loads(statistics[1])
        assert body["shortCode"] == short_code
        assert body["originalUrl"] == destination
        assert body["clickCount"] == 2
        assert re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z",
            body["lastClickedAt"],
        )
        assert datetime.fromisoformat(body["lastClickedAt"]).utcoffset().total_seconds() == 0


def test_short_link_statistics_use_one_autocommit_read_committed_select() -> None:
    with running_contender() as state:
        contender_url = state["contender"]["url"]
        database_container = state["database"]["container"]
        token, short_code = create_short_link_for_new_user(
            contender_url,
            email="stats-statement@example.com",
            destination="https://example.com/one-statistics-statement",
        )

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
        statistics = get_short_link_stats(contender_url, token, short_code)
        logs_after = read_container_logs(database_container)

        assert statistics[0] == 200
        assert logs_after.startswith(logs_before)
        request_logs = logs_after[len(logs_before) :]
        statement_lines = [
            line
            for line in request_logs.splitlines()
            if "statement:" in line or "execute <unnamed>:" in line
        ]
        assert len(statement_lines) == 1, request_logs
        assert "SELECT" in request_logs
        assert "FROM links" in request_logs
        assert "short_code = $1" in request_logs
        assert "user_id = $2" in request_logs
        assert "statement: BEGIN" not in request_logs
        assert "statement: COMMIT" not in request_logs


def test_short_link_statistics_timeout_is_unavailable_without_retry() -> None:
    with running_contender() as state:
        contender_url = state["contender"]["url"]
        database_container = state["database"]["container"]
        token, short_code = create_short_link_for_new_user(
            contender_url,
            email="stats-timeout@example.com",
            destination="https://example.com/statistics-timeout",
        )
        logging = run_psql(database_container, "ALTER SYSTEM SET log_statement = 'all'")
        reloaded = run_psql(database_container, "SELECT pg_reload_conf()")
        assert logging.returncode == 0, logging.stderr
        assert reloaded.returncode == 0, reloaded.stderr

        with holding_links_table_lock(database_container, duration_seconds=5):
            logs_before = read_container_logs(database_container)
            request_started = time.monotonic()
            statistics = get_short_link_stats(contender_url, token, short_code)
            request_elapsed = time.monotonic() - request_started
        logs_after = read_container_logs(database_container)

        assert statistics == (503, b'{"error":"unavailable"}', "application/json")
        assert 1.5 <= request_elapsed < 3.5
        assert logs_after.startswith(logs_before)
        request_logs = logs_after[len(logs_before) :]
        assert request_logs.count("FROM links") == 1, request_logs


def test_login_returns_the_standard_jwt_through_the_container_seam() -> None:
    with running_contender() as state:
        health_url = state["contender"]["url"]
        registration_status, registration_body, _ = register_user(
            health_url,
            b'{"email":"Login@Example.com","password":"benchmark-password"}',
        )
        issued_after = int(time.time())
        status, body, content_type = login_user(
            health_url,
            "LOGIN@EXAMPLE.COM",
            "benchmark-password",
        )
        issued_before = int(time.time())

        assert registration_status == 201
        assert (status, content_type) == (200, "application/json")
        response = json.loads(body)
        assert set(response) == {"token"}

        encoded_header, encoded_claims, encoded_signature = response["token"].split(".")
        header = decode_jwt_part(encoded_header)
        claims = decode_jwt_part(encoded_claims)
        expected_signature = hmac.new(
            PUBLIC_BENCHMARK_JWT_KEY,
            f"{encoded_header}.{encoded_claims}".encode(),
            hashlib.sha256,
        ).digest()

        assert header == {"alg": "HS256", "typ": "JWT"}
        assert set(claims) == {"sub", "iss", "aud", "iat", "exp"}
        assert claims["sub"] == json.loads(registration_body)["id"]
        assert uuid.UUID(claims["sub"]).version == 7
        assert claims["iss"] == "link-metrics"
        assert claims["aud"] == "link-metrics-api"
        assert type(claims["iat"]) is int
        assert issued_after <= claims["iat"] <= issued_before
        assert claims["exp"] == claims["iat"] + 900
        assert hmac.compare_digest(
            base64.urlsafe_b64decode(encoded_signature + ("=" * (-len(encoded_signature) % 4))),
            expected_signature,
        )


def test_protected_authentication_accepts_only_the_standard_jwt_profile() -> None:
    issued_at = int(time.time())
    valid_claims = {
        "sub": "0197f96c-b278-7f64-a32f-dae3cabe1ff0",
        "iss": "link-metrics",
        "aud": "link-metrics-api",
        "iat": issued_at,
        "exp": issued_at + 900,
    }
    invalid_tokens = [
        encode_jwt(valid_claims, alter_signature=True),
        encode_jwt(
            valid_claims,
            header={"alg": "HS384", "typ": "JWT"},
            signature_algorithm="sha384",
        ),
        encode_jwt(valid_claims, header={"alg": "HS256", "typ": "not-JWT"}),
        encode_jwt({**valid_claims, "iss": "another-issuer"}),
        encode_jwt({**valid_claims, "aud": "another-audience"}),
        encode_jwt({**valid_claims, "exp": issued_at - 1}),
        encode_jwt({**valid_claims, "sub": "not-a-uuid"}),
    ]

    with running_contender() as state:
        health_url = state["contender"]["url"]
        valid_response = request_api(
            health_url,
            "GET",
            "/api/links/00000000/stats",
            headers={"Authorization": f"Bearer {encode_jwt(valid_claims)}"},
        )
        invalid_responses = [
            request_api(
                health_url,
                "GET",
                "/api/links/00000000/stats",
                headers={"Authorization": f"Bearer {token}"},
            )
            for token in invalid_tokens
        ]

        assert valid_response[0] != 401
        assert invalid_responses == [
            (401, b'{"error":"unauthorized"}', "application/json")
        ] * len(invalid_tokens)


def test_login_rejects_wrong_email_and_password_identically() -> None:
    with running_contender() as state:
        health_url = state["contender"]["url"]
        registration = register_user(
            health_url,
            b'{"email":"credentials@example.com","password":"correct-password"}',
        )
        wrong_email = login_user(
            health_url,
            "missing@example.com",
            "correct-password",
        )
        wrong_password = login_user(
            health_url,
            "credentials@example.com",
            "wrong-password",
        )

        assert registration[0] == 201
        assert wrong_email == (401, b'{"error":"unauthorized"}', "application/json")
        assert wrong_password == wrong_email


def test_login_verifies_an_independently_generated_standard_argon2id_hash() -> None:
    # Generated with argon2-cffi's reference-backed low-level API using the exact
    # profile in ADR 0006 and the fixed 16-byte salt "0123456789abcdef".
    independent_hash = (
        "$argon2id$v=19$m=65536,t=3,p=4$MDEyMzQ1Njc4OWFiY2RlZg$"
        "eNFKE0ewrRxVJsC3Al3ZHiMQnqW0GlreWeIe3OFGzmQ"
    )

    with running_contender() as state:
        database_container = state["database"]["container"]
        inserted = run_psql(
            database_container,
            "INSERT INTO public.users (email, password_hash) VALUES "
            f"('independent-hash@example.com', '{independent_hash}')",
        )
        response = login_user(
            state["contender"]["url"],
            "independent-hash@example.com",
            "independent-password",
        )

        assert inserted.returncode == 0, inserted.stderr
        assert response[0] == 200


def test_login_uses_one_autocommit_read_committed_lookup() -> None:
    with running_contender() as state:
        health_url = state["contender"]["url"]
        database_container = state["database"]["container"]
        registration = register_user(
            health_url,
            b'{"email":"login-statement@example.com","password":"benchmark-password"}',
        )
        assert registration[0] == 201

        isolation = run_psql(database_container, "SHOW default_transaction_isolation")
        logging = run_psql(database_container, "ALTER SYSTEM SET log_statement = 'all'")
        reloaded = run_psql(database_container, "SELECT pg_reload_conf()")
        assert isolation.returncode == 0, isolation.stderr
        assert isolation.stdout.strip() == "read committed"
        assert logging.returncode == 0, logging.stderr
        assert reloaded.returncode == 0, reloaded.stderr

        logs_before = read_container_logs(database_container)
        response = login_user(
            health_url,
            "LOGIN-STATEMENT@EXAMPLE.COM",
            "benchmark-password",
        )
        logs_after = read_container_logs(database_container)

        assert response[0] == 200
        assert logs_after.startswith(logs_before)
        request_logs = logs_after[len(logs_before) :]
        statement_lines = [
            line
            for line in request_logs.splitlines()
            if "statement:" in line or "execute <unnamed>:" in line
        ]
        assert len(statement_lines) == 1, request_logs
        assert "SELECT id, password_hash" in request_logs
        assert "statement: BEGIN" not in request_logs
        assert "statement: COMMIT" not in request_logs


def test_login_pool_and_statement_timeouts_are_unavailable_without_retry() -> None:
    with running_contender() as state:
        health_url = state["contender"]["url"]
        database_container = state["database"]["container"]
        registration = register_user(
            health_url,
            b'{"email":"timeout-login@example.com","password":"benchmark-password"}',
        )
        assert registration[0] == 201

        logging = run_psql(database_container, "ALTER SYSTEM SET log_statement = 'all'")
        reloaded = run_psql(database_container, "SELECT pg_reload_conf()")
        assert logging.returncode == 0, logging.stderr
        assert reloaded.returncode == 0, reloaded.stderr

        lock_process = subprocess.Popen(
            [
                "docker",
                "exec",
                database_container,
                "psql",
                "--host",
                "127.0.0.1",
                "--username",
                "link_metrics_control",
                "--dbname",
                "link_metrics",
                "--command",
                "BEGIN; LOCK TABLE public.users IN ACCESS EXCLUSIVE MODE; "
                "SELECT pg_sleep(5); COMMIT;",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        lock_deadline = time.monotonic() + 2
        while time.monotonic() < lock_deadline:
            lock_state = run_psql(
                database_container,
                "SELECT count(*) FROM pg_catalog.pg_locks "
                "WHERE relation = 'public.users'::regclass "
                "AND mode = 'AccessExclusiveLock' AND granted",
            )
            if lock_state.returncode == 0 and lock_state.stdout.strip() == "1":
                break
            time.sleep(0.05)
        else:
            lock_process.terminate()
            raise AssertionError("failed to acquire the users table lock")

        logs_before = read_container_logs(database_container)
        statement_started = time.monotonic()
        statement_timeout = login_user(
            health_url,
            "timeout-login@example.com",
            "benchmark-password",
        )
        statement_elapsed = time.monotonic() - statement_started
        lock_stdout, lock_stderr = lock_process.communicate(timeout=10)
        assert lock_process.returncode == 0, lock_stdout + lock_stderr
        logs_after = read_container_logs(database_container)

        assert statement_timeout == (503, b'{"error":"unavailable"}', "application/json")
        assert 1.5 <= statement_elapsed < 3.5
        assert logs_after.startswith(logs_before)
        request_logs = logs_after[len(logs_before) :]
        assert request_logs.count("SELECT id, password_hash") == 1, request_logs

        disconnected = run_psql(
            database_container,
            "SELECT pg_terminate_backend(pid) FROM pg_catalog.pg_stat_activity "
            "WHERE usename = 'link_metrics_contender'",
        )
        assert disconnected.returncode == 0, disconnected.stderr
        time.sleep(0.1)

        paused = subprocess.run(
            ["docker", "pause", database_container],
            check=False,
            capture_output=True,
            text=True,
        )
        assert paused.returncode == 0, paused.stderr
        try:
            pool_started = time.monotonic()
            pool_timeout = login_user(
                health_url,
                "timeout-login@example.com",
                "benchmark-password",
            )
            pool_elapsed = time.monotonic() - pool_started
        finally:
            unpaused = subprocess.run(
                ["docker", "unpause", database_container],
                check=False,
                capture_output=True,
                text=True,
            )
            assert unpaused.returncode == 0, unpaused.stderr

        assert pool_timeout == (503, b'{"error":"unavailable"}', "application/json")
        assert 1.5 <= pool_elapsed < 3.5


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
        assert start_state["database"]["migrationVersion"] == "20260719000300"
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
        assert migration_read.stdout.strip() == "20260719000300"

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
            "WHERE version = '20260719000300'",
        )
        assert drift.returncode == 0, drift.stderr
        assert read_health(url) == (503, b'{"error":"unavailable"}', "application/json")

        restored = run_psql(
            database_container,
            "UPDATE public.schema_migrations SET version = '20260719000300' "
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
