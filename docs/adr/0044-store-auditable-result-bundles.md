# Store auditable result bundles

Every official run produces an immutable machine-readable bundle containing raw per-Trial metrics, environment fingerprint, Git commit, contender image digest and manifest, contract and migration versions, workload seed, calibration data, and validity checks. Compact summary JSON and generated Markdown or HTML reports are committed, large telemetry is retained as checksummed CI artifacts, and reports are always regenerated rather than edited manually.
