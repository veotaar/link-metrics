# Validate responses during performance Trials

K6 checks every response's status, content type, and required headers, verifies every redirect destination, and fully parses and validates a deterministic 1% sample of response bodies. Any failed check consumes the unexpected-response error budget, and a Trial is invalid if load-generator CPU saturation shows that validation became the bottleneck, preserving correctness evidence without parsing every payload.
