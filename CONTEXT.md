# Link Metrics

Link Metrics is a URL-shortening benchmark used to compare complete backend stacks under equivalent externally observable behavior and workloads.

## Language

**Short Link**:
An association owned by a user that maps a short code to a destination URL and accumulates click statistics.
_Avoid_: Link, shortened URL, redirect

**Short Code**:
The unique eight-character Base62 identifier of a short link.
_Avoid_: Slug, alias, link ID

**User**:
A registered identity that owns Short Links and is uniquely identified by a canonical email address.
_Avoid_: Account, customer

**Contender**:
A production-plausible backend stack participating in the benchmark, including its runtime, framework, database integration, validation, password hashing, and JWT implementation.
_Avoid_: Backend, implementation, adapter

**Trial**:
An isolated measurement of one contender against a freshly restored copy of the benchmark dataset.
_Avoid_: Run, test

**Scenario**:
A benchmark workload that exercises one externally observable service operation; mixed traffic is a separate supplementary scenario.
_Avoid_: Endpoint benchmark, test case

**Benchmark Dataset**:
The canonical seeded database state restored before each Trial.
_Avoid_: Fixtures, seed data, test database

**Result Series**:
A collection of benchmark results sharing the same contract, protocol, Dataset, and environment versions and therefore eligible for direct comparison.
_Avoid_: Leaderboard, benchmark run

**Conforming Contender**:
A contender that passes the benchmark's mandatory black-box API conformance suite and is therefore eligible to publish performance results.
_Avoid_: Valid backend, compliant implementation

**Click**:
A successful resolution of a Short Link that has been durably included in that Short Link's statistics.
_Avoid_: Hit, visit, redirect request

**API Contract**:
The normative description of the service's externally observable HTTP behavior that every contender must satisfy.
_Avoid_: API schema, route definitions, framework contract
