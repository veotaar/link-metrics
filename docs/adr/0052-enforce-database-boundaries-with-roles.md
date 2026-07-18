# Enforce database boundaries with roles

The control-plane owner role alone applies migrations, builds and clones template databases, seeds data, and invokes `pg_prewarm`. Contenders connect through a restricted role with only the exact table reads, inserts, updates, sequence use, and read-only migration-version access needed by API and health operations; it has no DDL, migration writes, extension execution, or destructive table privileges, mechanically preventing ORM ownership drift.
