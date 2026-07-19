# Benchmark protocol version

`VERSION` semantically versions measurement methodology independently of the API Contract and Benchmark Dataset. A methodology change capable of affecting results increments the major version and starts a new Result Series.

`scenarios.yaml` is the scored operation allowlist. The readiness operation is
deliberately absent: `/health` gates a Trial but never contributes a performance sample.
