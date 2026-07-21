# Benchmark Dataset

`VERSION` identifies the deterministic PostgreSQL state restored before every Trial.
Schema or Dataset-content changes increment this version independently of the API
Contract and benchmark protocol. `manifest.json` publishes the exact row counts,
Argon2id profile, benchmark-only password, five repetition seeds, and independent
sampling streams that construct version `1.2.0`.

Operate the Dataset through the control-plane command seam from `benchmark/`:

```sh
uv run link-metrics dataset describe --root ..
uv run link-metrics dataset sample --repetition 1 --count 10 --root ..
uv run link-metrics dataset tokens --repetition 1 --output /tmp/tokens.json --root ..
uv run link-metrics dataset build express-node --root ..
uv run link-metrics dataset inspect express-node --root ..
uv run link-metrics dataset reset express-node --expected-checksum <sha256> --root ..
```

`build` streams exactly 100,000 Users and 1,000,000 Short Links, computes a
catalog-and-content fingerprint, and clones an immutable versioned template database.
Every User receives ten Short Links: five never clicked and five with deterministic
nonzero Click counts and timestamps. Argon2id hashes use distinct salts from the
versioned HMAC-SHA256 cryptographic pseudorandom generator so rebuilding the same
Dataset version produces the same state.

The expensive User seed generation is persistent across local Contender environments.
The first `build` for a Dataset provenance checksum computes the 100,000 production-profile
Argon2id hashes and atomically stores a validated User CSV under
`${XDG_CACHE_HOME:-~/.cache}/link-metrics/datasets`. Later builds—including builds for a
different Contender—verify and reuse that artifact, then regenerate only the inexpensive
Short Link stream. Set `LINK_METRICS_DATASET_CACHE_DIR` to override the cache location.
Changes to Dataset inputs or generation code select a new cache key automatically, and a
missing or corrupt artifact is rebuilt under a cross-process file lock.

`reset` does not reconstruct data. It verifies committed source provenance, terminates
sessions on the fixed Trial database, recreates it from the template, checks the full
fingerprint and sequence state, prewarms the User and Short Link tables and all of their
indexes, and waits for the live Contender pool to reconnect. Both build and reset emit
the template checksum for result provenance; a supplied checksum mismatch fails before
the reset.

`tokens` selects 10,000 distinct seeded Users for one of the five repetitions and writes
fresh, identically serialized 15-minute reference JWTs. Each token entry includes an
owned Short Code. Generate the corpus once per Trial and share that file across
Contenders; an explicit `--issued-at` is available for reproducibility checks.

The million-row construction acceptance test is intentionally opt-in. On a cache miss it
performs all 100,000 production-profile Argon2id hashes; it then builds a second Contender
environment from the same cached artifact to prove cross-Contender reuse. Run it against
disposable Docker resources before publishing a Dataset version:

```sh
LINK_METRICS_TEST_FULL_DATASET=1 uv run pytest tests/test_dataset_runtime.py -q
```
