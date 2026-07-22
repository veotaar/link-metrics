# Benchmark control plane

This locked Python project operates Link Metrics through a language-neutral command seam. It is never imported into a measured Contender.

```sh
uv sync --locked
uv run link-metrics contenders discover --root ..
uv run link-metrics contenders conform express-node --root ..
uv run link-metrics dataset describe --root ..
uv run link-metrics contract lint
uv run pytest
```

The Express vertical slice can be operated entirely through the command seam:

```sh
uv run link-metrics contenders start express-node --root ..
uv run link-metrics contenders inspect express-node --root ..
uv run link-metrics contenders database-url express-node --root ..
uv run link-metrics contenders conform express-node --root ..
uv run link-metrics contenders stop express-node --root ..
```

`start` builds the manifest-declared OCI image, starts pinned PostgreSQL and the
Contender on a private bridge network, applies migrations as the control-plane owner,
and waits for HTTP readiness. `inspect` reports container state, the migration version,
and the readiness status without importing or inspecting framework code. `stop` removes
the disposable containers, database volume, and network.

`database-url` explicitly reveals the random, per-start owner connection to the trusted
host control plane for migration and generated-schema work. It is never passed into the
Contender container; the Contender receives only its restricted fixture credential.

`conform` owns a fresh Contender lifecycle and withholds eligibility unless the selected
manifest version matches the API Contract and both mandatory gates pass. Deterministic
pytest workflows cover exact readiness, identity, authentication, Short Link, Click, and
ownership transitions. Pinned Schemathesis exercises examples, coverage, positive and
negative fuzzing, response and header checks, and stateful creation-to-statistics sequences
from OpenAPI 3.1. The Contender remains opaque throughout both gates.

The `dataset` command group publishes deterministic workload samples and fresh reference
tokens, builds the immutable PostgreSQL template separately from Trials using a persistent
local cache for expensive User seed generation, and resets the fixed Trial database by
verified clone plus explicit buffer prewarming. See
[`dataset/README.md`](dataset/README.md) for the lifecycle and provenance commands.

`contenders discover` scans `backends/*/contender.yaml`, validates each document against `schemas/contender.schema.json`, rejects duplicate identities, and emits deterministic JSON. `contract lint` validates the OpenAPI 3.1.2 document, its semantic version, and unique operation identifiers; the API Contract itself remains the sole authority for its operation surface.

The `trial` command group owns the performance harness. `trial smoke` runs a short
nonofficial registration Trial that proves scheduling, response checks, Dataset reset, and
bundle production without presenting the numbers as benchmark data. `trial run` executes one
official success-path Scenario at a caller-supplied open-loop rate for the protocol warm and
measure windows. The accepted Scenarios are `registration`, `login`, `short-link-creation`,
`statistics`, `uniform-resolution`, and `viral-resolution`. Both modes require a previously
built Dataset template, pin Grafana k6 by
digest, and emit an immutable raw result bundle. Bundle paths are create-only so existing
evidence is never overwritten. Official Trials also run the conformance gate, enforce the
`local-7800x3d` physical-core and no-swap profile, require a stable-host preflight, record
frequency and temperature evidence, and capture mandatory Contender and PostgreSQL cgroup
CPU time, sampled resident-memory, network, transaction, sampled-lock, read, and write
telemetry. Missing telemetry
or frequency and thermal excursions invalidate a Trial without deleting its raw evidence.

```sh
uv run link-metrics dataset build express-node --root ..
uv run link-metrics trial smoke express-node --scenario registration \
  --output /tmp/registration-smoke.json --root ..
uv run link-metrics trial run express-node --scenario registration --rate 10 \
  --output /tmp/registration-trial.json --root ..
uv run link-metrics trial run express-node --scenario statistics --rate 10 \
  --output /tmp/statistics-trial.json --root ..
```

The `capacity` command replaces manual rate selection for publishable measurements. It runs
unreported calibration Trials, doubling the offered rate and then binary-searching the
passing/failing bracket, before scheduling five seeded Trials at 25%, 50%, 75%, 90%, 100%,
and 110% of each Contender's boundary. Contender order is deterministically randomized for
each rate and repetition. The create-only raw Result Series retains calibration evidence,
the official measurement plan, and every Trial bundle.

```sh
uv run link-metrics capacity run express-node --scenario registration \
  --output /tmp/registration-series.json --root ..
```

For quick local comparisons, `lite run` is an exploratory instrument rather than a
smaller official protocol. It defaults to every discovered Contender and the
`short-link-creation` and `uniform-resolution` Scenarios. Each Contender passes the
conformance gate once, then each selected Scenario uses 15-second adaptive calibration
samples and one 45-second confirmation measurement. Lite Trials retain the same Benchmark
Dataset, request validation, container limits, and resource telemetry as official Trials,
but host frequency and thermal excursions are warnings rather than blockers.

Lite mode prints a terminal table and removes its temporary Trial bundles when it exits. Its
results are prominently marked non-publishable and cannot be consumed by Result Series
reporting or verification. `--scenario` is repeatable. The two-hour default budget is also
the maximum; expiration prevents the next atomic Trial from starting, while an active Trial
is allowed to finish and clean up.

Before a Contender's first lite Trial, the control plane prepares its immutable PostgreSQL
template automatically. Existing templates are reused, and new contender-local templates
reuse the persistent User seed cache described above instead of recomputing the expensive
Argon2id corpus.

```sh
uv run link-metrics lite run \
  --scenario short-link-creation \
  --scenario uniform-resolution \
  --max-hours 2 \
  --root ..
```

Cold startup is measured independently from warm capacity. It requires the same running
PostgreSQL container and previously built Dataset template as a Trial, but no running
Contender. `startup run` performs 20 process starts, resetting the ready Dataset before
each repetition. Container creation, image build/pull, migrations, and seeding are outside
the timed interval; Docker's process `StartedAt` timestamp anchors time to the first
`/health` 204, followed by the latency of the first real registration request.

```sh
uv run link-metrics startup run express-node \
  --output /tmp/express-node-startup.json --root ..
```

Each Scenario is calibrated, qualified, and reported independently. Login and registration
use a 1,000 ms p99 budget; creation, statistics, and resolution use 250 ms. Protected
Scenarios cycle the seeded 10,000-User reference-token corpus. Resolution distributions are
deterministic, and every redirect `Location` is checked against its seeded destination.

Reports are regenerated from one or more comparable raw Result Series. Generation rejects
different API Contract, protocol, Benchmark Dataset, or environment fingerprints and writes
a compact JSON summary plus deterministic Markdown and HTML. Each rate includes every sample,
median, exact 95% bootstrap interval, coefficient of variation, instability, qualification
reasons, mandatory resource evidence, and the per-Scenario maximum sustainable throughput.
Cold-start bundles may be supplied alone or alongside a comparable capacity series; their
20 samples, median, and p95 are rendered in a separate report section.

```sh
uv run link-metrics report generate /tmp/registration-series.json \
  --output-dir /tmp/registration-report
```

## Run the complete local series over multiple days

The complete first cohort has a resumable, time-bounded command. It discovers every local
Contender, prepares and preserves each immutable Dataset template, advances all six
Scenarios, runs all four cold-start series, generates the comparison, and writes a checksum
manifest. Only the PostgreSQL container for the active atomic unit runs; prepared containers
remain stopped between units and sessions.

Configure the host yourself before each session, then confirm the read-only preflight:

```sh
sudo cpupower frequency-set --governor performance
echo 0 | sudo tee /sys/devices/system/cpu/cpufreq/boost
uv run link-metrics host preflight
```

Advance the series for up to four hours. The command checks the budget before preparing each
Contender and before starting each Trial or cold-start repetition, so the current preparation
or atomic unit may finish after the deadline.
Run the exact same command on later days; completed create-only evidence is validated and
reused rather than rerun. `Ctrl-C` is also safe between sessions: an interrupted unit without
a completed evidence file is the only unit attempted again.

```sh
uv run link-metrics series run \
  --time-budget-hours 4 \
  --output-dir ../results/local-7800x3d \
  --root ..
```

Progress JSON names completed Scenarios and cold-start series. When `status` becomes
`complete`, the raw bundles, generated report, and `manifest.json` are ready. Verification
checks the complete raw/report inventory and checksums, revalidates raw provenance, regenerates
the reports, and compares them byte-for-byte without running a benchmark:

```sh
uv run link-metrics series verify \
  --output-dir ../results/local-7800x3d
```

Do not change code, the API Contract, protocol, Dataset, Contender manifests, or host profile
while a series is in progress. Existing evidence that no longer matches its schedule or
comparability key is rejected instead of silently discarded. After the final session, normal
desktop CPU behavior can be restored with:

```sh
echo 1 | sudo tee /sys/devices/system/cpu/cpufreq/boost
sudo cpupower frequency-set --governor powersave
```

End-to-end smoke coverage is opt-in: `LINK_METRICS_TEST_TRIAL=1 uv run pytest tests/test_trial_lifecycle.py`.

The lockfile is committed. Change dependencies with `uv add` or `uv remove`, and verify installation with `uv sync --locked`. Pytest and Schemathesis are pinned together as the conformance toolchain.

Container startup also requires dbmate `2.34.1`; the control plane invokes that exact
version as `link_metrics_control` and refuses to interpret migration files itself.
