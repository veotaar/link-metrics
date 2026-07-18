# Use containers as the polyglot runtime seam

Turborepo orchestrates only pnpm workspace packages; Go, Python, and other ecosystems keep their native build and test tooling. Every contender exposes a uniform OCI container interface with the standard port, readiness operation, and environment inputs, and the language-neutral benchmark harness builds and runs those images directly rather than introducing fake JavaScript package wrappers.
