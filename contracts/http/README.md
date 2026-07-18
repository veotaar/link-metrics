# HTTP API Contract

`openapi.yaml` is the sole human-authored authority for every Contender's HTTP behavior. It is pinned to OpenAPI 3.1.2 and carries its own semantic version in `info.version`.

From `benchmark/`, validate both the OpenAPI document and the accepted minimal operation/status surface:

```sh
uv run link-metrics contract lint
```

Generated DTOs, validators, clients, or route declarations are derivatives. Change this document first and regenerate them; do not maintain a second wire contract inside a Contender.
