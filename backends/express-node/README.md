# Express on Node.js Contender

This Contender is discovered from `contender.yaml` and built through its isolated OCI
context. Its only implemented operation in the first vertical slice is `GET /health`.

Readiness returns `204` after the PostgreSQL pool can read the expected dbmate migration
version. A connection failure, pool timeout, query timeout, or migration mismatch returns
`503` with `{"error":"unavailable"}`. The operation is operational only and is absent
from `benchmark/protocol/scenarios.yaml`.

The container requires `DATABASE_URL`, `EXPECTED_MIGRATION_VERSION`, and `PORT`. The
control plane supplies these inputs; the Contender neither applies migrations nor owns
database structure.

`pnpm db:introspect` regenerates `drizzle/` from a freshly migrated catalog using the
control-plane `DATABASE_URL`. These committed files are disposable Drizzle derivatives;
they are never edited by hand or executed as migrations. Dbmate remains the only migration
authority.
