# Elysia on Bun Contender

This independent Contender uses Elysia, Bun, Drizzle's `bun-sql` adapter, and
`Bun.SQL`. It is discovered through `contender.yaml` and built from its isolated,
reproducibly locked OCI context.

It implements the complete Link Metrics API Contract: readiness, User registration and
login, authenticated Short Link creation and statistics, and public Short Link resolution
with atomic Click accounting. Its validation, authentication, SQL, handlers, and domain
runtime are local to this Contender and import no runtime implementation from another
Contender.

The native Bun SQL pool is capped at 20 connections. Pool acquisition, connection
establishment, and PostgreSQL statements use the common two-second timeouts, with one
statement attempt and no retries or explicit transactions. Success-path access logging,
response compression, application caching, and nonstandard transports are not enabled.

Registration and login use Argon2id v1.3 with 64 MiB, three iterations, four lanes, a
fresh 16-byte salt, and a 32-byte tag. JWT issuance and validation use the fixed public
benchmark key and the standard 15-minute HS256 profile. The key is reproducible benchmark
input and must never be used as an operational secret.

The container requires `DATABASE_URL`, `EXPECTED_MIGRATION_VERSION`, and `PORT`. It never
applies migrations or creates database objects. `pnpm db:introspect` regenerates the
committed `drizzle/` derivative from a freshly migrated catalog using `DATABASE_URL`;
dbmate migrations remain the sole database authority.
