# Stabilize the benchmark host

Official runs require a quiescent host using the performance governor with dynamic CPU boost disabled, no thermal throttling, and no unrelated workload. Preflight checks fail runs whose host load or frequency leaves tolerance, and every Trial records frequency and temperature counters, deliberately favoring repeatability over peak headline performance.
