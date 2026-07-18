# Measure cold startup separately

With PostgreSQL already migrated, seeded, and healthy, cold-start measurement begins when the contender process starts, ends readiness timing when `/health` first returns `204`, and then records the first real API request latency. Image build and pull, migrations, and seeding are excluded; 20 repetitions are reported as median and p95 separately from steady-state request results.
