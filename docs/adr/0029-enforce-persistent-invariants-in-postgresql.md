# Enforce persistent invariants in PostgreSQL

PostgreSQL constraints independently enforce canonical User email storage, exact Short Code shape, destination URL length and scheme rules, and nonnegative Click counts even though OpenAPI and contenders validate requests first. CI exercises equivalent acceptance and rejection through both boundaries, preserving OpenAPI as the HTTP authority while preventing any contender from persisting invalid state or silently drifting from migrations.
