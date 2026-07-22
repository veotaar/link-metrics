"""PostgreSQL construction and cloning for the Benchmark Dataset."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from link_metrics.dataset import DatasetError, _short_code, _user_id, describe_dataset
from link_metrics.dataset_seed_cache import UserSeedCache, ensure_user_seed_cache
from link_metrics.runtime import (
    CONTROL_ROLE,
    CONTENDER_ROLE,
    DATABASE_NAME,
    _container_document,
    _container_exists,
    _contender_url,
    _docker,
    _find_manifest,
    _resource_names,
    ensure_database_running,
    pause_database_container,
    remove_contender_container,
    start_contender,
    stop_contender,
    _wait_for_readiness,
)


_CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)
_CLICKED_AT = datetime(2026, 1, 2, tzinfo=UTC)


def _template_name(version: str) -> str:
    return f"link_metrics_template_{version.replace('.', '_').replace('-', '_')}"


def _psql(
    container: str,
    database: str,
    sql: str,
    *,
    tuples_only: bool = False,
) -> str:
    arguments = [
        "exec",
        "--interactive",
        container,
        "psql",
        "--username",
        CONTROL_ROLE,
        "--dbname",
        database,
        "--set",
        "ON_ERROR_STOP=1",
    ]
    if tuples_only:
        arguments.extend(["--tuples-only", "--no-align"])
    result = _docker(*arguments, input_text=sql)
    return result.stdout.strip()


def _database_container(root: Path, contender_id: str) -> tuple[str, dict[str, Any]]:
    contender = _find_manifest(root, contender_id)
    names = _resource_names(root, contender_id)
    if not _container_exists(names.database):
        raise DatasetError(f"Contender '{contender_id}' is not started")
    return names.database, contender


def _source_checksum(root: Path) -> str:
    digest = hashlib.sha256()
    paths = [
        root / "benchmark" / "dataset" / "VERSION",
        root / "benchmark" / "dataset" / "manifest.json",
        root / "benchmark" / "fixtures" / "jwt-hs256.key",
        root / "benchmark" / "src" / "link_metrics" / "dataset.py",
        root / "benchmark" / "src" / "link_metrics" / "dataset_runtime.py",
        root / "benchmark" / "src" / "link_metrics" / "dataset_seed_cache.py",
        *sorted((root / "database" / "migrations").glob("*.sql")),
    ]
    for path in paths:
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _database_metadata(container: str, database: str) -> dict[str, Any] | None:
    value = _psql(
        container,
        "postgres",
        "SELECT pg_catalog.shobj_description(oid, 'pg_database') "
        f"FROM pg_catalog.pg_database WHERE datname = '{database}';",
        tuples_only=True,
    )
    if not value:
        return None
    try:
        metadata = json.loads(value)
    except json.JSONDecodeError as error:
        raise DatasetError(f"template database {database} has invalid metadata") from error
    if not isinstance(metadata, dict):
        raise DatasetError(f"template database {database} has invalid metadata")
    return metadata


def _validate_template_metadata(
    container: str,
    database: str,
    metadata: dict[str, Any],
    manifest: dict[str, Any],
    source_checksum: str,
) -> None:
    if metadata.get("datasetVersion") != manifest["version"]:
        raise DatasetError("template Dataset version does not match committed Dataset version")
    if metadata.get("sourceChecksum") != source_checksum:
        raise DatasetError("template checksum does not match committed Dataset inputs")
    fingerprint = metadata.get("fingerprint")
    if not isinstance(fingerprint, dict):
        raise DatasetError("template metadata does not contain a Dataset fingerprint")
    if metadata.get("templateChecksum") != _template_checksum(fingerprint):
        raise DatasetError("template metadata checksum does not match its Dataset fingerprint")
    flags = _psql(
        container,
        "postgres",
        "SELECT datistemplate::text || ':' || datallowconn::text "
        f"FROM pg_catalog.pg_database WHERE datname = '{database}';",
        tuples_only=True,
    )
    if flags != "true:false":
        raise DatasetError(f"template database {database} is not immutable")


def _stream_dataset(
    container: str,
    manifest: dict[str, Any],
    source_checksum: str,
) -> UserSeedCache:
    user_seed_cache = ensure_user_seed_cache(manifest, source_checksum)
    command = [
        "docker",
        "exec",
        "--interactive",
        container,
        "psql",
        "--username",
        CONTROL_ROLE,
        "--dbname",
        DATABASE_NAME,
        "--set",
        "ON_ERROR_STOP=1",
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None
    writer = csv.writer(process.stdin, lineterminator="\n")
    try:
        process.stdin.write("BEGIN;\n")
        process.stdin.write(
            "COPY public.users (id, email, password_hash, created_at) FROM STDIN WITH (FORMAT csv);\n"
        )
        with user_seed_cache.path.open("r", encoding="utf-8", newline="") as users:
            shutil.copyfileobj(users, process.stdin, length=1024 * 1024)
        process.stdin.write("\\.\n")
        process.stdin.write(
            "COPY public.links "
            "(short_code, original_url, click_count, user_id, created_at, last_clicked_at) "
            "FROM STDIN WITH (FORMAT csv, NULL '\\N');\n"
        )
        for user_index in range(manifest["users"]):
            user_id = _user_id(user_index)
            for owned_index in range(manifest["shortLinks"]["perUser"]):
                link_index = user_index * manifest["shortLinks"]["perUser"] + owned_index
                if owned_index < manifest["shortLinks"]["neverClickedPerUser"]:
                    click_count = 0
                    last_clicked_at = r"\N"
                else:
                    click_count = (user_index + 1) * (
                        owned_index - manifest["shortLinks"]["neverClickedPerUser"] + 1
                    )
                    last_clicked_at = (
                        _CLICKED_AT + timedelta(seconds=link_index)
                    ).isoformat()
                writer.writerow(
                    (
                        _short_code(link_index),
                        f"https://benchmark.invalid/users/{user_index}/links/{owned_index}",
                        click_count,
                        user_id,
                        _CREATED_AT.isoformat(),
                        last_clicked_at,
                    )
                )
        process.stdin.write("\\.\n")
        process.stdin.write(
            f"SELECT pg_catalog.setval('public.links_short_code_sequence', "
            f"{manifest['shortLinks']['total']}, true);\n"
        )
        process.stdin.write("ANALYZE public.users; ANALYZE public.links; COMMIT;\n")
        process.stdin.close()
        stdout = process.stdout.read() if process.stdout is not None else ""
        stderr = process.stderr.read() if process.stderr is not None else ""
        return_code = process.wait()
    except BaseException:
        process.kill()
        process.wait()
        raise
    if return_code != 0:
        detail = stderr.strip() or stdout.strip() or "unknown PostgreSQL COPY failure"
        raise DatasetError(f"Benchmark Dataset construction failed: {detail}")
    return user_seed_cache


def _fingerprint(container: str, database: str) -> dict[str, Any]:
    sql = r"""
SELECT json_build_object(
    'users', (SELECT count(*) FROM public.users),
    'shortLinks', (SELECT count(*) FROM public.links),
    'neverClicked', (
        SELECT count(*) FROM public.links WHERE click_count = 0 AND last_clicked_at IS NULL
    ),
    'clicked', (
        SELECT count(*) FROM public.links WHERE click_count > 0 AND last_clicked_at IS NOT NULL
    ),
    'ownership', (
        SELECT json_build_object(
            'minimumShortLinks', min(short_links),
            'maximumShortLinks', max(short_links),
            'minimumNeverClicked', min(never_clicked),
            'maximumNeverClicked', max(never_clicked),
            'minimumClicked', min(clicked),
            'maximumClicked', max(clicked)
        )
        FROM (
            SELECT
                user_id,
                count(*) AS short_links,
                count(*) FILTER (WHERE click_count = 0 AND last_clicked_at IS NULL) AS never_clicked,
                count(*) FILTER (WHERE click_count > 0 AND last_clicked_at IS NOT NULL) AS clicked
            FROM public.links
            GROUP BY user_id
        ) AS ownership
    ),
    'distinctPasswordHashes', (SELECT count(DISTINCT password_hash) FROM public.users),
    'distinctPasswordSalts', (
        SELECT count(DISTINCT split_part(password_hash, '$', 5)) FROM public.users
    ),
    'argon2idProfile', (
        SELECT bool_and(password_hash LIKE '$argon2id$v=19$m=65536,t=3,p=4$%') FROM public.users
    ),
    'sequence', (
        SELECT json_build_object('lastValue', last_value, 'isCalled', is_called)
        FROM public.links_short_code_sequence
    ),
    'userDataHash', (
        SELECT json_build_object(
            'sum', sum(hashtextextended(concat_ws(E'\x1f', id, email, password_hash, created_at), 0))::text,
            'xor', bit_xor(hashtextextended(concat_ws(E'\x1f', id, email, password_hash, created_at), 1))::text
        ) FROM public.users
    ),
    'shortLinkDataHash', (
        SELECT json_build_object(
            'sum', sum(hashtextextended(concat_ws(E'\x1f', short_code, original_url, click_count, user_id, created_at, last_clicked_at), 0))::text,
            'xor', bit_xor(hashtextextended(concat_ws(E'\x1f', short_code, original_url, click_count, user_id, created_at, last_clicked_at), 1))::text
        ) FROM public.links
    ),
    'constraints', (
        SELECT json_agg(definition ORDER BY definition)
        FROM (
            SELECT format('%s:%s', conrelid::regclass, pg_get_constraintdef(oid)) AS definition
            FROM pg_catalog.pg_constraint
            WHERE connamespace = 'public'::regnamespace
        ) AS constraints
    ),
    'indexes', (
        SELECT json_agg(indexdef ORDER BY indexdef)
        FROM pg_catalog.pg_indexes
        WHERE schemaname = 'public'
    ),
    'columns', (
        SELECT json_agg(
            format('%s.%s:%s:%s:%s', table_name, column_name, data_type, is_nullable, coalesce(column_default, ''))
            ORDER BY table_name, ordinal_position
        )
        FROM information_schema.columns
        WHERE table_schema = 'public'
    ),
    'migrationVersions', (
        SELECT json_agg(version ORDER BY version) FROM public.schema_migrations
    ),
    'roles', json_build_object(
        'databaseConnect', has_database_privilege('link_metrics_contender', current_database(), 'CONNECT'),
        'usersSelect', has_table_privilege('link_metrics_contender', 'public.users', 'SELECT'),
        'usersInsert', has_table_privilege('link_metrics_contender', 'public.users', 'INSERT'),
        'linksSelect', has_table_privilege('link_metrics_contender', 'public.links', 'SELECT'),
        'linksInsert', has_table_privilege('link_metrics_contender', 'public.links', 'INSERT'),
        'linksUpdate', has_table_privilege('link_metrics_contender', 'public.links', 'UPDATE'),
        'sequenceUse', has_sequence_privilege('link_metrics_contender', 'public.links_short_code_sequence', 'USAGE')
    ),
    'representative', json_build_object(
        'firstUser', (SELECT row_to_json(value) FROM (SELECT id, email, password_hash, created_at FROM public.users ORDER BY email LIMIT 1) AS value),
        'neverClicked', (SELECT row_to_json(value) FROM (SELECT short_code, original_url, click_count, user_id, created_at, last_clicked_at FROM public.links WHERE click_count = 0 ORDER BY short_code LIMIT 1) AS value),
        'clicked', (SELECT row_to_json(value) FROM (SELECT short_code, original_url, click_count, user_id, created_at, last_clicked_at FROM public.links WHERE click_count > 0 ORDER BY short_code LIMIT 1) AS value)
    )
);
"""
    output = _psql(container, database, sql, tuples_only=True)
    try:
        return json.loads(output)
    except json.JSONDecodeError as error:
        raise DatasetError(f"could not fingerprint database {database}") from error


def _validate_fingerprint(fingerprint: dict[str, Any], manifest: dict[str, Any]) -> None:
    users = manifest["users"]
    total_links = manifest["shortLinks"]["total"]
    clicked = users * manifest["shortLinks"]["clickedPerUser"]
    never_clicked = users * manifest["shortLinks"]["neverClickedPerUser"]
    expected = {
        "users": users,
        "shortLinks": total_links,
        "clicked": clicked,
        "neverClicked": never_clicked,
        "distinctPasswordHashes": users,
        "distinctPasswordSalts": users,
        "argon2idProfile": True,
    }
    mismatches = {
        key: {"expected": value, "actual": fingerprint.get(key)}
        for key, value in expected.items()
        if fingerprint.get(key) != value
    }
    sequence = fingerprint.get("sequence")
    if sequence != {"lastValue": total_links, "isCalled": True}:
        mismatches["sequence"] = {
            "expected": {"lastValue": total_links, "isCalled": True},
            "actual": sequence,
        }
    per_user = manifest["shortLinks"]["perUser"]
    expected_ownership = {
        "minimumShortLinks": per_user,
        "maximumShortLinks": per_user,
        "minimumNeverClicked": manifest["shortLinks"]["neverClickedPerUser"],
        "maximumNeverClicked": manifest["shortLinks"]["neverClickedPerUser"],
        "minimumClicked": manifest["shortLinks"]["clickedPerUser"],
        "maximumClicked": manifest["shortLinks"]["clickedPerUser"],
    }
    if fingerprint.get("ownership") != expected_ownership:
        mismatches["ownership"] = {
            "expected": expected_ownership,
            "actual": fingerprint.get("ownership"),
        }
    if mismatches:
        raise DatasetError(
            f"Benchmark Dataset fidelity check failed: {json.dumps(mismatches, sort_keys=True)}"
        )


def _template_checksum(fingerprint: dict[str, Any]) -> str:
    canonical = json.dumps(fingerprint, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(canonical).hexdigest()


def _prewarm(container: str) -> dict[str, int]:
    output = _psql(
        container,
        DATABASE_NAME,
        r"""
SELECT json_object_agg(relname, warmed ORDER BY relname)
FROM (
    SELECT relname, pg_catalog.pg_prewarm(oid, 'buffer') AS warmed
    FROM pg_catalog.pg_class
    WHERE relnamespace = 'public'::regnamespace
      AND relname IN (
          'users', 'users_pkey', 'idx_users_email',
          'links', 'links_pkey', 'idx_links_user_id'
      )
) AS relations;
""",
        tuples_only=True,
    )
    warmed = json.loads(output)
    if not isinstance(warmed, dict) or len(warmed) != 6:
        raise DatasetError("prewarming did not cover all required tables and indexes")
    return {str(key): int(value) for key, value in warmed.items()}


def inspect_template(root: Path, contender_id: str) -> dict[str, Any]:
    """Inspect immutable template provenance without opening the template database."""
    root = root.resolve()
    container, _ = _database_container(root, contender_id)
    manifest = describe_dataset(root)
    template = _template_name(manifest["version"])
    metadata = _database_metadata(container, template)
    if metadata is None:
        raise DatasetError(f"template database {template} has not been built")
    _validate_template_metadata(
        container, template, metadata, manifest, _source_checksum(root)
    )
    return {"database": template, **metadata}


def build_template(root: Path, contender_id: str) -> dict[str, Any]:
    """Construct the Dataset once and preserve it as an immutable template database."""
    root = root.resolve()
    container, contender = _database_container(root, contender_id)
    manifest = describe_dataset(root)
    template = _template_name(manifest["version"])
    source_checksum = _source_checksum(root)
    existing = _database_metadata(container, template)
    if existing is not None:
        _validate_template_metadata(
            container, template, existing, manifest, source_checksum
        )
        return {"database": template, "status": "reused", **existing}

    counts = _psql(
        container,
        DATABASE_NAME,
        "SELECT (SELECT count(*) FROM public.users) || ':' || "
        "(SELECT count(*) FROM public.links);",
        tuples_only=True,
    )
    if counts != "0:0":
        raise DatasetError("the Trial database must be empty before Dataset construction")

    names = _resource_names(root, contender_id)
    contender_was_running = _container_exists(names.contender) and bool(
        _container_document(names.contender)["State"]["Running"]
    )
    if contender_was_running:
        _docker("stop", "--time", "5", names.contender)
    try:
        user_seed_cache = _stream_dataset(container, manifest, source_checksum)
        fingerprint = _fingerprint(container, DATABASE_NAME)
        _validate_fingerprint(fingerprint, manifest)
        checksum = _template_checksum(fingerprint)
        metadata = {
            "datasetVersion": manifest["version"],
            "sourceChecksum": source_checksum,
            "templateChecksum": checksum,
            "fingerprint": fingerprint,
        }
        serialized_metadata = json.dumps(
            metadata, separators=(",", ":"), sort_keys=True
        ).replace("'", "''")
        try:
            _psql(
                container,
                "postgres",
                f"""
ALTER DATABASE {DATABASE_NAME} WITH ALLOW_CONNECTIONS false;
SELECT pg_catalog.pg_terminate_backend(pid)
FROM pg_catalog.pg_stat_activity
WHERE datname = '{DATABASE_NAME}' AND pid <> pg_catalog.pg_backend_pid();
CREATE DATABASE {template} WITH TEMPLATE {DATABASE_NAME} OWNER {CONTROL_ROLE};
ALTER DATABASE {DATABASE_NAME} WITH ALLOW_CONNECTIONS true;
REVOKE ALL ON DATABASE {template} FROM PUBLIC;
COMMENT ON DATABASE {template} IS '{serialized_metadata}';
ALTER DATABASE {template} WITH IS_TEMPLATE true ALLOW_CONNECTIONS false;
""",
            )
        except Exception:
            _psql(
                container,
                "postgres",
                f"ALTER DATABASE {DATABASE_NAME} WITH ALLOW_CONNECTIONS true;",
            )
            raise
    finally:
        if contender_was_running:
            _docker("start", names.contender)
            document = _container_document(names.contender)
            _wait_for_readiness(_contender_url(document, contender["port"]))

    return {
        "database": template,
        "status": "built",
        "userSeedCache": {
            "sha256": user_seed_cache.sha256,
            "sourceChecksum": user_seed_cache.source_checksum,
            "status": user_seed_cache.status,
            "users": user_seed_cache.users,
        },
        **metadata,
    }


def prepare_template_runtime(root: Path, contender_id: str) -> dict[str, Any]:
    """Prepare or resume one persistent Dataset template, then pause its containers."""
    root = root.resolve()
    names = _resource_names(root, contender_id)
    try:
        if _container_exists(names.database):
            ensure_database_running(root, contender_id)
        else:
            start_contender(root, contender_id)
        try:
            return build_template(root, contender_id)
        except DatasetError as error:
            if "Trial database must be empty" not in str(error):
                raise
            stop_contender(root, contender_id)
            start_contender(root, contender_id)
            return build_template(root, contender_id)
    finally:
        remove_contender_container(root, contender_id)
        pause_database_container(root, contender_id)


def reset_from_template(
    root: Path,
    contender_id: str,
    expected_checksum: str | None = None,
) -> dict[str, Any]:
    """Clone, verify, and prewarm the fixed Trial database while its pool reconnects."""
    root = root.resolve()
    container, contender = _database_container(root, contender_id)
    manifest = describe_dataset(root)
    template = _template_name(manifest["version"])
    metadata = _database_metadata(container, template)
    if metadata is None:
        raise DatasetError(f"template database {template} has not been built")
    _validate_template_metadata(
        container, template, metadata, manifest, _source_checksum(root)
    )
    actual_checksum = metadata.get("templateChecksum")
    if expected_checksum is not None and expected_checksum != actual_checksum:
        raise DatasetError(
            f"template checksum mismatch: expected {expected_checksum}, got {actual_checksum}"
        )

    _psql(container, "postgres", f"ALTER ROLE {CONTENDER_ROLE} NOLOGIN;")
    try:
        _psql(
            container,
            "postgres",
            f"""
ALTER DATABASE {DATABASE_NAME} WITH ALLOW_CONNECTIONS false;
SELECT pg_catalog.pg_terminate_backend(pid)
FROM pg_catalog.pg_stat_activity
WHERE datname = '{DATABASE_NAME}' AND pid <> pg_catalog.pg_backend_pid();
DROP DATABASE {DATABASE_NAME};
CREATE DATABASE {DATABASE_NAME} WITH TEMPLATE {template} OWNER {CONTROL_ROLE};
ALTER DATABASE {DATABASE_NAME} WITH IS_TEMPLATE false ALLOW_CONNECTIONS true;
REVOKE ALL ON DATABASE {DATABASE_NAME} FROM PUBLIC;
GRANT CONNECT ON DATABASE {DATABASE_NAME} TO {CONTENDER_ROLE};
""",
        )
        fingerprint = _fingerprint(container, DATABASE_NAME)
        _validate_fingerprint(fingerprint, manifest)
        cloned_checksum = _template_checksum(fingerprint)
        if cloned_checksum != actual_checksum:
            raise DatasetError(
                f"cloned Dataset checksum mismatch: expected {actual_checksum}, got {cloned_checksum}"
            )
        warmed = _prewarm(container)
    finally:
        _psql(container, "postgres", f"ALTER ROLE {CONTENDER_ROLE} LOGIN;")
    names = _resource_names(root, contender_id)
    if _container_exists(names.contender):
        document = _container_document(names.contender)
        _wait_for_readiness(_contender_url(document, contender["port"]))
    return {
        "database": DATABASE_NAME,
        "datasetVersion": manifest["version"],
        "prewarmed": warmed,
        "status": "reset",
        "template": template,
        "templateChecksum": actual_checksum,
    }
