# Define the local resource profile

On the Ryzen 7 7800X3D development host, the initial manual-only performance profile assigns four physical cores and 8 GiB to the contender, two physical cores and 8 GiB to PostgreSQL, and two physical cores and 4 GiB to the load generator. Benchmark containers do not use sibling SMT threads and cannot swap; exact CPU IDs and limits are included in the `local-7800x3d` environment fingerprint. Hosted CI runs correctness checks but no performance Trials, and future server hosts require separately named and reported profiles.
