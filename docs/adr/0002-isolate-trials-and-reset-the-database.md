# Isolate trials and reset the database

Each trial measures one contender in isolation against the same controlled PostgreSQL server configuration, with the database restored to an identical seeded state beforehand. Contenders do not execute concurrently or inherit mutations from earlier trials, avoiding contention, dataset growth, and run-order history as sources of bias; repeated trials and randomized contender order may be used to expose remaining cache and ordering effects.
