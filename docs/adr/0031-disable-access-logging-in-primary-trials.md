# Disable access logging in primary Trials

Primary performance Trials disable success-path access logging; contenders emit only startup, readiness, and unexpected-error information while resource telemetry is collected externally. Structured logging and tracing overhead may be measured only in a separately reported observability-enabled scenario family, preventing framework logging defaults from becoming an implicit workload.
