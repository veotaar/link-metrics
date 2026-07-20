# Link Metrics

Link Metrics is a local-first benchmark of complete, production-plausible backend stacks. Every **Contender** implements the same URL-shortening **API Contract**, uses the same PostgreSQL authority, and is measured against the same versioned **Benchmark Dataset** and protocol.

The project reports each **Scenario** separately. It does not turn Node.js, Bun, Express, Elysia, or any other component into a synthetic overall winner.

## Authority seams

The repository has three independently testable authority seams:

1. `contracts/http/openapi.yaml` is the sole human-authored HTTP authority. A Contender is observed as an opaque container through this API Contract.
2. `database/migrations/` is the sole human-authored database authority. `database/schema.sql` is a generated review snapshot of a freshly migrated PostgreSQL catalog.
3. `benchmark/` is the locked Python control plane. Its command interface discovers Contenders, enforces conformance, constructs and resets the Benchmark Dataset, and owns Trial orchestration and result bundles.

Reusable JavaScript and TypeScript code belongs in `packages/`. Deployable stacks belong in `backends/`, regardless of language. Turborepo only orchestrates directories that are actual pnpm packages; the language-neutral authorities keep their native tooling.

## Repository map

```text
backends/       deployable Contenders and their local manifests
benchmark/      Python control plane, protocol, Dataset, and tests
contracts/http/ versioned OpenAPI 3.1.2 API Contract
database/       dbmate migrations and generated schema snapshot
packages/       reusable JavaScript/TypeScript packages and configuration
```

## Get started

Install JavaScript tooling and run its existing quality commands:

```sh
pnpm install --frozen-lockfile
pnpm lint
pnpm format
pnpm check-types
```

Install the Python control plane reproducibly and exercise its public command seam:

```sh
cd benchmark
uv sync --locked
uv run link-metrics contenders discover --root ..
uv run link-metrics contenders conform express-node --root ..
uv run link-metrics dataset describe --root ..
uv run link-metrics contract lint
uv run pytest
```

Run the first containerized vertical slice from `benchmark/`:

```sh
uv run link-metrics contenders start express-node --root ..
uv run link-metrics contenders inspect express-node --root ..
uv run link-metrics dataset build express-node --root ..
uv run link-metrics dataset reset express-node --root ..
uv run link-metrics trial smoke express-node --output /tmp/registration-smoke.json --root ..
uv run link-metrics capacity run express-node --scenario registration \
  --output /tmp/registration-series.json --root ..
uv run link-metrics report generate /tmp/registration-series.json \
  --output-dir /tmp/registration-report
uv run link-metrics contenders stop express-node --root ..
```

The control plane treats Express as opaque: it builds from `contender.yaml`, supplies the
restricted PostgreSQL connection, and observes only the standard `/health` operation.

The discovery command validates every `backends/<id>/contender.yaml` against `benchmark/schemas/contender.schema.json`. Stable identity, pinned language/runtime/framework versions, an existing container build context and Dockerfile, port, worker topology, resource profile, and API Contract version are required. Invalid, unknown, directory-mismatched, or duplicate identities fail with a diagnostic naming the manifest. An incomplete backend skeleton becomes discoverable only when its container seam exists.

## Versions

The initial comparability identifiers are independent:

- API Contract: `1.0.1` in `contracts/http/openapi.yaml`
- benchmark protocol: `1.0.0` in `benchmark/protocol/VERSION`
- Benchmark Dataset: `1.2.0` in `benchmark/dataset/VERSION`

A **Result Series** may compare results only when its API Contract version, protocol version, Benchmark Dataset version, and environment fingerprint all match exactly.

See [CONTEXT.md](CONTEXT.md) for the normative project vocabulary and [docs/adr](docs/adr) for accepted design decisions.
