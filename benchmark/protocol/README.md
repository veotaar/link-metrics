# Benchmark protocol version

`VERSION` semantically versions measurement methodology independently of the API Contract and Benchmark Dataset. A methodology change capable of affecting results increments the major version and starts a new Result Series.

`scenarios.yaml` is the scored operation allowlist. The readiness operation is
deliberately absent: `/health` gates a Trial but never contributes a performance sample.

Protocol 2.0 runs each accepted success-path operation as an isolated open-loop Scenario:
registration, login, Short Link creation, statistics, uniform resolution, and viral
resolution. Login selects seeded credentials. Protected Scenarios cycle the same seeded
10,000-User reference-token corpus, with statistics alternating evenly between owned
never-clicked and clicked Short Links. Creation submits byte-stable destinations and leaves
Short Code generation to PostgreSQL.

Uniform resolution uses a repetition-seeded full-population permutation so every seeded
Short Link is selected once before any repeats. Viral resolution assigns exactly 90% of
each 100-request block to one Short Code and uses a separate full-population permutation
for the remaining 10%. Every resolution checks the `Location` header byte-for-byte; JSON
response bodies use the independent seeded one-percent validation stream.
Login and registration have a 1,000 ms p99 budget. Creation, statistics, and both resolution
Scenarios have a 250 ms p99 budget.
