# Database authority

Dbmate files in `migrations/` are the sole human-authored authority for PostgreSQL structure. Applied migrations produce the runtime catalog; `schema.sql` is a generated, committed review snapshot and must not be edited by hand.

From the repository root, with `DATABASE_URL` set for a control-plane owner connection:

```sh
dbmate --migrations-dir database/migrations --schema-file database/schema.sql up
dbmate --migrations-dir database/migrations --schema-file database/schema.sql dump
```

Once merged or used for a versioned Benchmark Dataset, a migration is immutable. Append a corrective migration and regenerate the snapshot instead of rewriting history. Contenders never run migrations or own DDL.

`postgresql.conf` is the committed PostgreSQL 18.4 configuration used by the control
plane. Durability and autovacuum remain enabled. `shared_preload_libraries` stays empty,
so `pg_prewarm` has no background worker; the control-plane role is the only role allowed
to invoke its functions.

The control plane creates two database roles for each disposable environment:

- `link_metrics_control` owns migrations, extensions, and lifecycle operations.
- `link_metrics_contender` can read the migration version and perform only the table and
  sequence operations needed by a Contender. It has no DDL, migration-write, destructive,
  or extension privileges.
