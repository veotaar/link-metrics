# Use reference tokens for protected scenarios

Before each Trial, a reference fixture tool generates an identical, deterministically serialized corpus of fresh valid HS256 tokens for 10,000 uniformly selected seeded users using the benchmark secret and fixed claims. Protected Scenarios use the corpus round-robin, pairing each token with Short Links owned by its subject where ownership matters, while the separate login Scenario measures contender-specific token issuance. This prevents identity caching, login setup, and serialization differences from contaminating JWT-verification results.
