# Require resource telemetry

Every Trial records contender and PostgreSQL CPU time, average and peak resident memory, network bytes, PostgreSQL transaction and lock activity, and database reads and writes alongside request results. These metrics remain separate rather than forming a composite score; runtime-specific diagnostics such as garbage-collection pauses may be attached only as explanatory data.
