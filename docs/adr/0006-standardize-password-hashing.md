# Standardize password hashing

Every contender uses Argon2id v1.3 for registration and login with 64 MiB of memory, three iterations, four lanes, a fresh 16-byte cryptographically random salt, and a 32-byte tag stored in the standard encoded form. Fixing the RFC 9106 memory-constrained profile makes password work equivalent across languages while leaving each contender free to use an idiomatic Argon2 implementation.
