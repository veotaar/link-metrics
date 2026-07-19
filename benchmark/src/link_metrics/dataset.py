"""Deterministic Benchmark Dataset and workload inputs."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from pathlib import Path
from typing import Any


JWT_LIFETIME_SECONDS = 15 * 60
VIRAL_SHORT_CODE_INDEX = 0
_USER_TIMESTAMP_MILLISECONDS = 1_767_225_600_000


class DatasetError(Exception):
    """The control plane could not construct deterministic Dataset inputs."""


def _manifest_path(root: Path) -> Path:
    return root / "benchmark" / "dataset" / "manifest.json"


def describe_dataset(root: Path) -> dict[str, Any]:
    """Return the committed, versioned Dataset description."""
    try:
        manifest = json.loads(_manifest_path(root).read_text(encoding="utf-8"))
        version = (root / "benchmark" / "dataset" / "VERSION").read_text(
            encoding="utf-8"
        ).strip()
    except (OSError, json.JSONDecodeError) as error:
        raise DatasetError(f"cannot read Benchmark Dataset metadata: {error}") from error
    if manifest.get("version") != version:
        raise DatasetError(
            "Benchmark Dataset version mismatch between dataset/VERSION and manifest.json"
        )
    return manifest


class _DeterministicRandom:
    """Versioned SHA-256 counter PRNG with stable sampling across Python releases."""

    def __init__(self, seed: int, stream: str) -> None:
        self._key = seed.to_bytes(8, "big") + b":" + stream.encode("ascii")
        self._counter = 0

    def _uint64(self) -> int:
        block = hashlib.sha256(self._key + self._counter.to_bytes(8, "big")).digest()
        self._counter += 1
        return int.from_bytes(block[:8], "big")

    def randrange(self, stop: int) -> int:
        if stop <= 0:
            raise ValueError("stop must be positive")
        limit = (1 << 64) - ((1 << 64) % stop)
        while (candidate := self._uint64()) >= limit:
            pass
        return candidate % stop

    def shuffle(self, values: list[bool]) -> None:
        for index in range(len(values) - 1, 0, -1):
            other = self.randrange(index + 1)
            values[index], values[other] = values[other], values[index]

    def sample_indexes(self, population: int, count: int) -> list[int]:
        values = list(range(population))
        for index in range(count):
            other = index + self.randrange(population - index)
            values[index], values[other] = values[other], values[index]
        return values[:count]


def _rng(seed: int, stream: str) -> _DeterministicRandom:
    return _DeterministicRandom(seed, stream)


def _repetition_seed(manifest: dict[str, Any], repetition: int) -> int:
    seeds = manifest["repetitionSeeds"]
    if not 1 <= repetition <= len(seeds):
        raise DatasetError(f"repetition must be between 1 and {len(seeds)}")
    return int(seeds[repetition - 1])


def _user_id(index: int) -> str:
    random_bits = int.from_bytes(
        hashlib.sha256(f"link-metrics:user:{index}".encode("ascii")).digest()[:10],
        "big",
    ) & ((1 << 74) - 1)
    value = (
        (_USER_TIMESTAMP_MILLISECONDS + index) << 80
        | 0x7 << 76
        | (random_bits >> 62) << 64
        | 0b10 << 62
        | random_bits & ((1 << 62) - 1)
    )
    return str(uuid.UUID(int=value))


def _short_code(index: int) -> str:
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    value = index + 1
    encoded = ""
    while value:
        value, remainder = divmod(value, len(alphabet))
        encoded = alphabet[remainder] + encoded
    return encoded.rjust(8, "0")


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _reference_token(secret: bytes, user_id: str, issued_at: int) -> str:
    header = _b64url(
        json.dumps(
            {"alg": "HS256", "typ": "JWT"}, separators=(",", ":"), sort_keys=True
        ).encode("ascii")
    )
    claims = _b64url(
        json.dumps(
            {
                "aud": "link-metrics-api",
                "exp": issued_at + JWT_LIFETIME_SECONDS,
                "iat": issued_at,
                "iss": "link-metrics",
                "sub": user_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    )
    signing_input = f"{header}.{claims}"
    signature = _b64url(hmac.new(secret, signing_input.encode("ascii"), hashlib.sha256).digest())
    return f"{signing_input}.{signature}"


def write_reference_tokens(
    root: Path,
    repetition: int,
    output: Path,
    issued_at: int | None = None,
) -> dict[str, Any]:
    """Write fresh, deterministically serialized tokens for protected Scenarios."""
    manifest = describe_dataset(root)
    seed = _repetition_seed(manifest, repetition)
    issued_at = int(time.time()) if issued_at is None else issued_at
    if issued_at < 0:
        raise DatasetError("issued-at must be a nonnegative Unix timestamp")

    secret = (root / "benchmark" / "fixtures" / "jwt-hs256.key").read_bytes().rstrip(b"\n")
    if len(secret) != 32:
        raise DatasetError("benchmark JWT fixture must contain exactly 32 bytes")
    selected_users = _rng(seed, "tokens").sample_indexes(
        manifest["users"], manifest["referenceTokenUsers"]
    )
    entries = []
    for entry_index, user_index in enumerate(selected_users):
        user_id = _user_id(user_index)
        owned_offset = (
            _rng(seed, f"owned-short-code:{user_index}").randrange(5)
            + 5 * (entry_index % 2)
        )
        entries.append(
            {
                "shortCode": _short_code(user_index * 10 + owned_offset),
                "token": _reference_token(secret, user_id, issued_at),
                "userId": user_id,
            }
        )

    corpus = {
        "datasetVersion": manifest["version"],
        "expiresAt": issued_at + JWT_LIFETIME_SECONDS,
        "issuedAt": issued_at,
        "repetition": repetition,
        "seed": seed,
        "tokens": entries,
    }
    serialized = (json.dumps(corpus, separators=(",", ":"), sort_keys=True) + "\n").encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(serialized)
    return {
        "count": len(entries),
        "expiresAt": corpus["expiresAt"],
        "issuedAt": issued_at,
        "output": str(output),
        "repetition": repetition,
        "seed": seed,
        "sha256": hashlib.sha256(serialized).hexdigest(),
    }


def sample_workload(
    root: Path, repetition: int, count: int
) -> dict[str, Any]:
    """Emit independent deterministic samples used by every workload surface."""
    if count < 1:
        raise DatasetError("count must be positive")
    manifest = describe_dataset(root)
    seed = _repetition_seed(manifest, repetition)
    user_rng = _rng(seed, "users")
    token_rng = _rng(seed, "tokens")
    short_code_rng = _rng(seed, "shortCodes")
    validation_rng = _rng(seed, "validation")
    access_rng = _rng(seed, "access")
    total_links = manifest["shortLinks"]["total"]

    uniform_access = [_short_code(access_rng.randrange(total_links)) for _ in range(count)]
    viral_access = []
    viral_pattern: list[bool] = []
    while len(viral_pattern) < count:
        block = [True] * 90 + [False] * 10
        access_rng.shuffle(block)
        viral_pattern.extend(block)
    for viral in viral_pattern[:count]:
        viral_access.append(
            _short_code(VIRAL_SHORT_CODE_INDEX if viral else access_rng.randrange(total_links))
        )

    return {
        "count": count,
        "repetition": repetition,
        "seed": seed,
        "samples": {
            "access": {"uniform": uniform_access, "viral": viral_access},
            "shortCodes": [_short_code(short_code_rng.randrange(total_links)) for _ in range(count)],
            "tokens": [token_rng.randrange(manifest["referenceTokenUsers"]) for _ in range(count)],
            "users": [user_rng.randrange(manifest["users"]) for _ in range(count)],
            "validation": [validation_rng.randrange(100) == 0 for _ in range(count)],
        },
    }
