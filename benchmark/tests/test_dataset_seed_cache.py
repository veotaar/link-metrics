import hashlib
from pathlib import Path

import pytest

import link_metrics.dataset_seed_cache as seed_cache


SMALL_MANIFEST = {
    "argon2id": {
        "hashLength": 16,
        "memoryKiB": 8,
        "parallelism": 1,
        "saltLength": 16,
        "timeCost": 1,
        "version": 19,
    },
    "benchmarkPassword": "cache-test-password",
    "saltSeed": "083f465a3788b3c734346368d60ef16bc351eb197ae42035c52601f9bba138bd",
    "users": 2,
    "version": "test-1",
}


@pytest.fixture
def cache_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "persistent-dataset-cache"
    monkeypatch.setenv("LINK_METRICS_DATASET_CACHE_DIR", str(directory))
    return directory


def test_reuses_a_valid_user_seed_artifact_without_regenerating_hashes(
    cache_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = seed_cache.ensure_user_seed_cache(SMALL_MANIFEST, "a" * 64)

    def unexpected_generation(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("valid cache should not regenerate Argon2 hashes")

    monkeypatch.setattr(seed_cache, "_write_user_seed_artifact", unexpected_generation)
    second = seed_cache.ensure_user_seed_cache(SMALL_MANIFEST, "a" * 64)

    assert first.status == "built"
    assert second.status == "reused"
    assert first.path == second.path
    assert first.sha256 == second.sha256
    assert first.users == second.users == 2
    assert first.path.is_relative_to(cache_directory)


def test_rebuilds_a_corrupt_user_seed_artifact(cache_directory: Path) -> None:
    first = seed_cache.ensure_user_seed_cache(SMALL_MANIFEST, "b" * 64)
    first.path.write_text("corrupt\n", encoding="utf-8")

    rebuilt = seed_cache.ensure_user_seed_cache(SMALL_MANIFEST, "b" * 64)

    assert rebuilt.status == "built"
    assert rebuilt.path == first.path
    assert rebuilt.sha256 == hashlib.sha256(rebuilt.path.read_bytes()).hexdigest()
    assert rebuilt.sha256 == first.sha256


def test_keys_user_seed_artifacts_by_dataset_provenance(cache_directory: Path) -> None:
    first = seed_cache.ensure_user_seed_cache(SMALL_MANIFEST, "c" * 64)
    changed = seed_cache.ensure_user_seed_cache(SMALL_MANIFEST, "d" * 64)

    assert first.status == changed.status == "built"
    assert first.path != changed.path
    assert first.path.is_file()
    assert changed.path.is_file()
