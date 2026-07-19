# Express on Node.js Contender

This Contender is discovered from `contender.yaml` and built through its isolated OCI
context. The current vertical slice implements `GET /health`, `POST /api/auth/register`,
`POST /api/auth/login`, authenticated `POST /api/links`, and public `GET /{shortCode}`,
plus bearer authentication for the remaining protected API Contract paths.

Readiness returns `204` after the PostgreSQL pool can read the expected dbmate migration
version. A connection failure, pool timeout, query timeout, or migration mismatch returns
`503` with `{"error":"unavailable"}`. The operation is operational only and is absent
from `benchmark/protocol/scenarios.yaml`.

Registration enforces the shared closed JSON Credentials contract before hashing the
password with Node.js Argon2id v1.3 using the benchmark's fixed 64 MiB, three-iteration,
four-lane profile. It persists the canonical email and encoded hash through one
autocommit insert-and-return statement. Canonical email shape and uniqueness are also
enforced by PostgreSQL.

Login performs one canonical User lookup, verifies the stored Argon2id hash, and returns
a 15-minute HS256 JWT. Issuance and protected-request validation use the fixed 32-byte
key in `benchmark/fixtures/jwt-hs256.key`. That key is a public, reproducible benchmark
fixture and must never be used as an operational secret. The Contender does not issue
refresh tokens or persist or cache bearer tokens.

Short Link creation validates the closed destination request, preserves accepted URLs
byte-for-byte, and inserts through the generated Drizzle table definition. PostgreSQL
owns deterministic eight-character Base62 Short Code generation and independently
enforces Short Code, destination, and nonnegative Click-count invariants. Creation uses
one autocommit insert-and-return statement and returns only the contracted ownership,
Short Code, destination, and timestamp fields.

Short Link resolution uses one atomic autocommit update that increments the `BIGINT`
Click count, records `last_clicked_at` with PostgreSQL `clock_timestamp()`, and returns the
byte-preserved destination. The update commits before the Contender emits an empty `302`
response. Missing Short Codes return canonical `404`; database failures and timeouts return
canonical `503` without redirecting. Resolution has no cache, retry, batching, or explicit
transaction-control path.

The container requires `DATABASE_URL`, `EXPECTED_MIGRATION_VERSION`, and `PORT`. The
control plane supplies these inputs; the Contender neither applies migrations nor owns
database structure.

`pnpm db:introspect` regenerates `drizzle/` from a freshly migrated catalog using the
control-plane `DATABASE_URL`. These committed files are disposable Drizzle derivatives;
they are never edited by hand or executed as migrations. Dbmate remains the only migration
authority.
