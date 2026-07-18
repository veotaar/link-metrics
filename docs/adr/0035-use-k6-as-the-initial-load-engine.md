# Use k6 as the initial load engine

The first harness uses a pinned Grafana k6 container and its constant-arrival-rate executor, dropped-iteration metric, checks, and thresholds to implement the open-loop protocol. Any Trial in which the load generator saturates or cannot schedule the offered rate is invalid; k6 remains replaceable because the benchmark protocol, not the tool, defines results.
