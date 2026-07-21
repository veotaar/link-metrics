# NestJS with Express on Node.js Contender

This independent Contender uses NestJS with its default Express adapter, Drizzle,
`node-postgres`, and Node.js. It is discovered through `contender.yaml` and built from
its isolated OCI context.

It implements the complete Link Metrics API Contract: readiness, User registration and
login, authenticated Short Link creation and statistics, and public Short Link resolution
with atomic Click accounting. Its validation, authentication, SQL, handlers, and domain
runtime are local to this Contender and import no runtime implementation from another
Contender.

The PostgreSQL pool is capped at 20 connections. Pool acquisition and statements use the
common two-second timeouts, with one statement attempt and no retries or explicit
transactions. Success-path access logging, response compression, application caching,
and nonstandard transports are not enabled.

The container requires `DATABASE_URL`, `EXPECTED_MIGRATION_VERSION`, and `PORT`. It never
applies migrations or creates database objects. `pnpm db:introspect` regenerates the
committed `drizzle/` derivative from a freshly migrated catalog using `DATABASE_URL`;
dbmate migrations remain the sole database authority.
