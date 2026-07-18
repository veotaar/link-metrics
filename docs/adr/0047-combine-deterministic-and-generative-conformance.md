# Combine deterministic and generative conformance

The mandatory black-box conformance gate combines deterministic pytest workflows for registration, login, ownership, Click accounting, and exact error semantics with pinned Schemathesis property-based tests generated from OpenAPI 3.1. Schemathesis exercises boundary, malformed, authentication, schema, header, and stateful cases, complementing rather than replacing explicit domain workflows.
