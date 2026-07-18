# Allow idiomatic worker topologies

Each contender may choose and commit its production-plausible process, worker, and thread topology while remaining within the same CPU and memory quota. External proxies, caches, and connection poolers are excluded unless supplied uniformly to every contender, so runtime scaling strategy remains part of the complete stack without granting unequal host resources.
