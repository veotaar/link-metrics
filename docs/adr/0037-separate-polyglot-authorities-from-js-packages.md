# Separate polyglot authorities from JS packages

The repository uses `backends/` for deployable contenders in every language, `contracts/http/` for the OpenAPI authority, `database/` for dbmate migrations and the generated schema snapshot, `benchmark/` for protocol, workloads, orchestration, and reports, and reserves `packages/` for reusable JavaScript/TypeScript libraries and configuration. This prevents polyglot repository-wide authorities from masquerading as pnpm/Turborepo packages.
