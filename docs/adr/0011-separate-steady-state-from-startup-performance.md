# Separate steady-state from startup performance

Primary trials measure warm steady-state request performance only, after database preparation, health readiness, connection-pool establishment, and a fixed warm-up workload. Process startup time and cold-request latency are measured as separate scenarios, preventing trial duration from diluting startup cost and keeping distinct operational qualities visible.
