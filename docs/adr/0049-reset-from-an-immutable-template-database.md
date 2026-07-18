# Reset from an immutable template database

For each dataset version, the control plane applies migrations and deterministic seeding once to build an immutable PostgreSQL template database. Before every Trial it terminates contender sessions, recreates the fixed trial database from that template, prewarms it, and lets the contender pool reconnect; the result bundle records the template checksum, avoiding repeated million-row seeding and reset drift.
