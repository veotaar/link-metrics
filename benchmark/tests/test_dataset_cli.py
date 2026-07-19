import base64
import hashlib
import hmac
import json
import subprocess
import sys
import uuid
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def run_dataset(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "link_metrics", "dataset", *arguments, "--root", str(REPOSITORY_ROOT)],
        check=False,
        capture_output=True,
        text=True,
    )


def decode_segment(segment: str) -> dict[str, object]:
    padding = "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment + padding))


def short_code_index(short_code: str) -> int:
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    value = 0
    for character in short_code:
        value = value * len(alphabet) + alphabet.index(character)
    return value - 1


def test_describes_the_versioned_benchmark_dataset_and_published_seeds() -> None:
    result = run_dataset("describe")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "argon2id": {
            "hashLength": 32,
            "memoryKiB": 65_536,
            "parallelism": 4,
            "saltLength": 16,
            "timeCost": 3,
            "version": 19,
        },
        "benchmarkPassword": "link-metrics-benchmark-only",
        "prng": "sha256-counter-v1",
        "referenceTokenUsers": 10_000,
        "repetitionSeeds": [
            1_350_403_001_542_084_573,
            16_626_817_107_421_360_574,
            1_288_854_886_252_412_864,
            16_145_191_919_997_344_020,
            12_322_208_812_885_659_100,
        ],
        "saltSeed": "083f465a3788b3c734346368d60ef16bc351eb197ae42035c52601f9bba138bd",
        "samplingStreams": ["users", "tokens", "shortCodes", "validation", "access"],
        "shortLinks": {
            "clickedPerUser": 5,
            "neverClickedPerUser": 5,
            "perUser": 10,
            "total": 1_000_000,
        },
        "users": 100_000,
        "version": "1.2.0",
    }


def test_generates_an_identical_fresh_reference_token_corpus(tmp_path: Path) -> None:
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"

    first = run_dataset(
        "tokens", "--repetition", "1", "--issued-at", "1_800_000_000", "--output", str(first_path)
    )
    second = run_dataset(
        "tokens", "--repetition", "1", "--issued-at", "1_800_000_000", "--output", str(second_path)
    )

    assert first.returncode == second.returncode == 0, first.stderr or second.stderr
    assert first_path.read_bytes() == second_path.read_bytes()
    summary = json.loads(first.stdout)
    assert summary["count"] == 10_000
    assert summary["issuedAt"] == 1_800_000_000
    assert summary["expiresAt"] == 1_800_000_900
    assert summary["sha256"] == hashlib.sha256(first_path.read_bytes()).hexdigest()

    corpus = json.loads(first_path.read_text(encoding="utf-8"))
    assert set(corpus) == {"datasetVersion", "expiresAt", "issuedAt", "repetition", "seed", "tokens"}
    assert len(corpus["tokens"]) == 10_000
    assert len({entry["userId"] for entry in corpus["tokens"]}) == 10_000
    owned_offsets = [short_code_index(entry["shortCode"]) % 10 for entry in corpus["tokens"]]
    assert sum(offset < 5 for offset in owned_offsets) == 5_000
    assert sum(offset >= 5 for offset in owned_offsets) == 5_000

    entry = corpus["tokens"][0]
    assert uuid.UUID(entry["userId"]).version == 7
    assert (entry["userId"], entry["shortCode"]) == (
        "019b76dc-1ccd-7888-87bb-af4334ddbdb6",
        "000040H7",
    )
    encoded_header, encoded_claims, encoded_signature = entry["token"].split(".")
    assert decode_segment(encoded_header) == {"alg": "HS256", "typ": "JWT"}
    assert decode_segment(encoded_claims) == {
        "aud": "link-metrics-api",
        "exp": 1_800_000_900,
        "iat": 1_800_000_000,
        "iss": "link-metrics",
        "sub": entry["userId"],
    }
    secret = (REPOSITORY_ROOT / "benchmark/fixtures/jwt-hs256.key").read_bytes().rstrip(b"\n")
    expected_signature = base64.urlsafe_b64encode(
        hmac.new(
            secret,
            f"{encoded_header}.{encoded_claims}".encode("ascii"),
            hashlib.sha256,
        ).digest()
    ).rstrip(b"=").decode("ascii")
    assert encoded_signature == expected_signature


def test_published_seed_reproduces_all_sampling_streams() -> None:
    first = run_dataset("sample", "--repetition", "3", "--count", "12")
    second = run_dataset("sample", "--repetition", "3", "--count", "12")
    other = run_dataset("sample", "--repetition", "4", "--count", "12")

    assert first.returncode == second.returncode == other.returncode == 0
    assert first.stdout == second.stdout
    assert first.stdout != other.stdout
    samples = json.loads(first.stdout)
    assert set(samples["samples"]) == {
        "access",
        "shortCodes",
        "tokens",
        "users",
        "validation",
    }
    assert len(samples["samples"]["users"]) == 12
    assert all(0 <= index < 100_000 for index in samples["samples"]["users"])
    assert all(0 <= index < 10_000 for index in samples["samples"]["tokens"])
    assert all(len(code) == 8 for code in samples["samples"]["shortCodes"])
    assert all(value in {False, True} for value in samples["samples"]["validation"])
    assert set(samples["samples"]["access"]) == {"uniform", "viral"}
    assert len(samples["samples"]["access"]["uniform"]) == 12
    assert len(samples["samples"]["access"]["viral"]) == 12

    viral = run_dataset("sample", "--repetition", "1", "--count", "100")
    viral_samples = json.loads(viral.stdout)["samples"]["access"]["viral"]
    assert viral_samples.count("00000001") == 90


def test_template_commands_require_a_running_control_plane_database() -> None:
    result = run_dataset("inspect", "express-node")

    assert result.returncode == 2
    assert "Contender 'express-node' is not started" in result.stderr
