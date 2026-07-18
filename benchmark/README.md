# Benchmark control plane

This locked Python project operates Link Metrics through a language-neutral command seam. It is never imported into a measured Contender.

```sh
uv sync --locked
uv run link-metrics contenders discover --root ..
uv run link-metrics contract lint
uv run pytest
```

`contenders discover` scans `backends/*/contender.yaml`, validates each document against `schemas/contender.schema.json`, rejects duplicate identities, and emits deterministic JSON. `contract lint` performs OpenAPI validation and pins the accepted versioned six-operation surface.

The lockfile is committed. Change dependencies with `uv add` or `uv remove`, and verify installation with `uv sync --locked`.
