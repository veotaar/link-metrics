# Cap database connections per contender

Each contender may configure its database pool and queuing behavior idiomatically but may open at most 20 PostgreSQL connections and may not use an external connection pooler. The common cap prevents contenders from purchasing throughput with unequal database concurrency while leaving connection management within the complete stack comparison.
