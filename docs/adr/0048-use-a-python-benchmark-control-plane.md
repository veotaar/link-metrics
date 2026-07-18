# Use a Python benchmark control plane

One locked Python project under `benchmark/` owns container orchestration, host preflight, migration and reset control, fixture and reference-token generation, deterministic conformance workflows, result aggregation, and report generation. Pinned Schemathesis supplies generative conformance and k6 remains the load engine; control-plane Python never executes inside a measured contender path.
