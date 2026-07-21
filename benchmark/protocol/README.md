# Benchmark protocol version

`VERSION` semantically versions measurement methodology independently of the API Contract and Benchmark Dataset. A methodology change capable of affecting results increments the major version and starts a new Result Series.

`scenarios.yaml` is the scored operation allowlist. The readiness operation is
deliberately absent: `/health` gates a Trial but never contributes a performance sample.

Protocol 3.0 runs each accepted success-path operation as an isolated open-loop Scenario:
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

Official measurements use the named `local-7800x3d` profile. The Contender receives
physical cores 0–3 and 8 GiB, PostgreSQL receives physical cores 4–5 and 8 GiB, and k6
receives physical cores 6–7 and 4 GiB. Equal memory and memory-plus-swap limits prohibit
container swap. Preflight verifies the Ryzen 7 7800X3D topology, distinct SMT sibling
groups, a quiescent host, the performance governor, disabled boost, 3.99–4.41 GHz
frequency, a maximum 80 °C CPU temperature, no active thermal alarm, and throttle counters
for every assigned physical core. Measurements that leave the frequency or thermal
envelope, increment a throttle counter, or lack host
or mandatory resource telemetry remain in raw evidence but are invalid.

Every measured Trial records Contender and PostgreSQL cgroup-v2 CPU-time deltas,
sampled average and peak resident memory (`memory.stat` anonymous, mapped-file, and shared
memory), and
network bytes from before load starts until after it stops. PostgreSQL evidence also records
transaction deltas, sampled peak granted and waiting locks, block reads, cache hits, and
read/write operations. Runtime-specific diagnostics
may be attached to individual samples as explanatory evidence, but they are not universal
metrics and do not participate in qualification.

Cold startup is a separate Result Series of 20 repetitions against an already migrated,
seeded, healthy, and prewarmed PostgreSQL Dataset. Each repetition creates the stopped
container and resets the Dataset before timing, anchors timing to Docker's recorded process
`StartedAt`, records the first
`/health` 204 and the latency of the first registration request, and excludes image build,
pull, migrations, and seeding. Reports publish median and p95 startup values separately
from steady-state capacity and never combine them into a score.
