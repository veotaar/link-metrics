"""Persistent deterministic User seed artifacts shared by local Contenders."""

from __future__ import annotations

import csv
import fcntl
import hashlib
import hmac
import json
import os
import tempfile
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from argon2.low_level import Type, hash_secret

from link_metrics.dataset import DatasetError, _user_id


_CACHE_FORMAT_VERSION = 1
_CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class UserSeedCache:
    """One validated deterministic User seed artifact."""

    path: Path
    sha256: str
    source_checksum: str
    status: str
    users: int


def _cache_root() -> Path:
    configured = os.environ.get("LINK_METRICS_DATASET_CACHE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg_cache).expanduser() if xdg_cache else Path.home() / ".cache"
    return (base / "link-metrics" / "datasets").resolve()


def _password_hash(arguments: tuple[str, int, dict[str, int], str]) -> str:
    password, user_index, parameters, salt_seed = arguments
    salt = hmac.new(
        bytes.fromhex(salt_seed),
        user_index.to_bytes(8, "big"),
        hashlib.sha256,
    ).digest()[: parameters["saltLength"]]
    return hash_secret(
        password.encode("ascii"),
        salt,
        time_cost=parameters["timeCost"],
        memory_cost=parameters["memoryKiB"],
        parallelism=parameters["parallelism"],
        hash_len=parameters["hashLength"],
        type=Type.ID,
        version=parameters["version"],
    ).decode("ascii")


def _hash_arguments(
    manifest: dict[str, Any],
) -> Iterable[tuple[str, int, dict[str, int], str]]:
    for user_index in range(manifest["users"]):
        yield (
            manifest["benchmarkPassword"],
            user_index,
            manifest["argon2id"],
            manifest["saltSeed"],
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for block in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_user_seed_artifact(path: Path, manifest: dict[str, Any]) -> tuple[str, int]:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            writer = csv.writer(output, lineterminator="\n")
            workers = max(1, min(2, os.cpu_count() or 1))
            with ProcessPoolExecutor(max_workers=workers) as pool:
                hashes = pool.map(_password_hash, _hash_arguments(manifest), chunksize=8)
                for user_index, password_hash in enumerate(hashes):
                    writer.writerow(
                        (
                            _user_id(user_index),
                            f"benchmark-user-{user_index:06d}@example.invalid",
                            password_hash,
                            _CREATED_AT.isoformat(),
                        )
                    )
            output.flush()
            os.fsync(output.fileno())
        digest = _sha256(temporary_path)
        size = temporary_path.stat().st_size
        os.replace(temporary_path, path)
        return digest, size
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _write_metadata(path: Path, metadata: dict[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(metadata, output, separators=(",", ":"), sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _validated_artifact(
    artifact_path: Path,
    metadata_path: Path,
    manifest: dict[str, Any],
    source_checksum: str,
) -> UserSeedCache | None:
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        expected = {
            "datasetVersion": manifest["version"],
            "formatVersion": _CACHE_FORMAT_VERSION,
            "sourceChecksum": source_checksum,
            "users": manifest["users"],
        }
        if not isinstance(metadata, dict) or any(
            metadata.get(key) != value for key, value in expected.items()
        ):
            return None
        if metadata.get("sizeBytes") != artifact_path.stat().st_size:
            return None
        digest = _sha256(artifact_path)
        if metadata.get("sha256") != digest:
            return None
    except (OSError, json.JSONDecodeError):
        return None
    return UserSeedCache(
        path=artifact_path,
        sha256=digest,
        source_checksum=source_checksum,
        status="reused",
        users=manifest["users"],
    )


def ensure_user_seed_cache(
    manifest: dict[str, Any], source_checksum: str
) -> UserSeedCache:
    """Return a valid persistent User seed artifact, building it at most once."""
    if len(source_checksum) != 64 or any(
        character not in "0123456789abcdef" for character in source_checksum
    ):
        raise DatasetError("Dataset source checksum must be 64 lowercase hexadecimal characters")

    try:
        root = _cache_root()
        root.mkdir(parents=True, exist_ok=True)
        lock_path = root / f"{source_checksum}.lock"
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            artifact_directory = root / source_checksum
            artifact_directory.mkdir(exist_ok=True)
            artifact_path = artifact_directory / "users.csv"
            metadata_path = artifact_directory / "manifest.json"
            existing = _validated_artifact(
                artifact_path,
                metadata_path,
                manifest,
                source_checksum,
            )
            if existing is not None:
                return existing

            digest, size = _write_user_seed_artifact(artifact_path, manifest)
            _write_metadata(
                metadata_path,
                {
                    "datasetVersion": manifest["version"],
                    "formatVersion": _CACHE_FORMAT_VERSION,
                    "sha256": digest,
                    "sizeBytes": size,
                    "sourceChecksum": source_checksum,
                    "users": manifest["users"],
                },
            )
            return UserSeedCache(
                path=artifact_path,
                sha256=digest,
                source_checksum=source_checksum,
                status="built",
                users=manifest["users"],
            )
    except OSError as error:
        raise DatasetError(f"cannot use persistent Dataset cache: {error}") from error
