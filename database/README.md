# Database authority

Dbmate files in `migrations/` are the sole human-authored authority for PostgreSQL structure. Applied migrations produce the runtime catalog; `schema.sql` is a generated, committed review snapshot and must not be edited by hand.

From the repository root, with `DATABASE_URL` set for a control-plane owner connection:

```sh
dbmate --migrations-dir database/migrations --schema-file database/schema.sql up
dbmate --migrations-dir database/migrations --schema-file database/schema.sql dump
```

Once merged or used for a versioned Benchmark Dataset, a migration is immutable. Append a corrective migration and regenerate the snapshot instead of rewriting history. Contenders never run migrations or own DDL.
