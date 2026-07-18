# Standardize drivers within each runtime

Both Node contenders use Drizzle's `node-postgres` adapter with `pg`, while both Bun contenders use Drizzle's native `bun-sql` adapter with `Bun.SQL`. This holds the database driver constant when comparing frameworks on the same runtime while preserving runtime-native integration across Node and Bun; driver behavior remains part of each complete stack.
