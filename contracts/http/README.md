# HTTP API Contract

`openapi.yaml` is the sole human-authored authority for every Contender's HTTP behavior. It is pinned to OpenAPI 3.1.2 and carries its own semantic version in `info.version`.

From `benchmark/`, validate the OpenAPI document, dialect, semantic version, and operation identifiers:

```sh
uv run link-metrics contract lint
```

Generated DTOs, validators, clients, or route declarations are derivatives. Change this document first and regenerate them; do not maintain a second wire contract inside a Contender.

The Short Link creation response links its generated Short Code to owned statistics so
the mandatory Schemathesis gate can derive stateful API sequences from this authority.
