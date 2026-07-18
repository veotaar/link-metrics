# Canonicalize email identity

User email addresses are ASCII-only, contain no surrounding whitespace, contain at most 254 bytes, and are lowercased before storage and lookup; PostgreSQL enforces uniqueness on that canonical value. OpenAPI specifies an explicit accepted pattern instead of relying solely on implementation-dependent `email` format validation, intentionally treating case variants such as `User@Example.com` and `user@example.com` as the same User.
