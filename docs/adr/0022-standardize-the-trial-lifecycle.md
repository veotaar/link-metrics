# Standardize the Trial lifecycle

Each Trial starts a fresh contender container, waits for readiness, runs a fixed 30-second warm-up workload, restores and prewarms the Benchmark Dataset while keeping the contender process alive, re-establishes its database pool, measures one offered rate for 60 seconds, and then stops the container after collecting metrics. This warms runtime paths without allowing warm-up mutations or startup cost to contaminate steady-state measurement.
