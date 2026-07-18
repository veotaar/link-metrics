# Version the benchmark protocol independently

The benchmark protocol has a semantic version independent of the API Contract and Benchmark Dataset. Any methodology change capable of affecting results increments the protocol major version and starts a new Result Series, while non-result-affecting tooling fixes may increment patch; direct comparability requires the exact tuple of contract version, protocol version, Dataset version, and environment fingerprint.
