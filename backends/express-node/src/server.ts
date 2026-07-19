import express from "express";
import { drizzle } from "drizzle-orm/node-postgres";
import { sql } from "drizzle-orm";
import { Pool } from "pg";

const databaseUrl = process.env.DATABASE_URL;
const expectedMigrationVersion = process.env.EXPECTED_MIGRATION_VERSION;
const port = Number(process.env.PORT ?? "3000");

if (!databaseUrl || !expectedMigrationVersion || !Number.isInteger(port)) {
  throw new Error("DATABASE_URL, EXPECTED_MIGRATION_VERSION, and a valid PORT are required");
}

const pool = new Pool({
  connectionString: databaseUrl,
  connectionTimeoutMillis: 2_000,
  max: 20,
  options: "-c statement_timeout=2000",
  query_timeout: 2_000,
});
pool.on("error", (error) => {
  console.error("Unexpected idle PostgreSQL pool error", error);
});
const database = drizzle(pool);
const app = express();

app.disable("x-powered-by");
app.get("/health", async (_request, response) => {
  try {
    const result = await database.execute<{ version: string }>(sql`
      SELECT version
      FROM schema_migrations
      ORDER BY version DESC
      LIMIT 1
    `);

    if (result.rows[0]?.version === expectedMigrationVersion) {
      response.status(204).end();
      return;
    }
  } catch {
    // Readiness failures are reported through the API Contract below.
  }

  response.status(503).json({ error: "unavailable" });
});

const server = app.listen(port, "0.0.0.0", () => {
  console.log(`Express Contender listening on port ${port}`);
});

async function shutdown(): Promise<void> {
  server.close(async () => {
    await pool.end();
    process.exit(0);
  });
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
