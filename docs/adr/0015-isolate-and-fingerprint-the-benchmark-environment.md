# Isolate and fingerprint the benchmark environment

Official results run in versioned containers with fixed memory limits and non-overlapping CPU sets for the contender, PostgreSQL, and load generator. Every result records container images, runtime versions, host CPU, kernel, memory, PostgreSQL configuration, and resource quotas as an environment fingerprint; results with different fingerprints are reported separately rather than merged.
