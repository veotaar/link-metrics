# Benchmark control plane

This locked Python project operates Link Metrics through a language-neutral command seam. It is never imported into a measured Contender.

```sh
uv sync --locked
uv run link-metrics contenders discover --root ..
uv run link-metrics contenders conform express-node --root ..
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

`contenders discover` scans `backends/*/contender.yaml`, validates each document against `schemas/contender.schema.json`, rejects duplicate identities, and emits deterministic JSON. `contract lint` validates the OpenAPI 3.1.2 document, its semantic version, and unique operation identifiers; the API Contract itself remains the sole authority for its operation surface.

The lockfile is committed. Change dependencies with `uv add` or `uv remove`, and verify installation with `uv sync --locked`. Pytest and Schemathesis are pinned together as the conformance toolchain.

Container startup also requires dbmate `2.34.1`; the control plane invokes that exact
version as `link_metrics_control` and refuses to interpret migration files itself.
