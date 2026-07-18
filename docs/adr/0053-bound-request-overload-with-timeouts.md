# Bound request overload with timeouts

Contender sessions use a two-second database pool-acquisition timeout and two-second PostgreSQL `statement_timeout`, while k6 uses a five-second HTTP timeout. Pool and statement timeouts return canonical `503` with `{"error":"unavailable"}` and are counted against the error budget, bounding queues and exposing overload without retries or requests hanging beyond the measurement window.
