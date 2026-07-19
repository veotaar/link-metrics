import express from "express";
import { argon2, randomBytes } from "node:crypto";
import { drizzle } from "drizzle-orm/node-postgres";
import { sql } from "drizzle-orm";
import { Pool } from "pg";
import type { ErrorRequestHandler, Request, RequestHandler, Response } from "express";

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

function postgresErrorCode(error: unknown): string | undefined {
  let current = error;
  while (current instanceof Error) {
    if ("code" in current && typeof current.code === "string") {
      return current.code;
    }
    current = current.cause;
  }
  return undefined;
}

type Credentials = { email: string; password: string };
type ErrorDetail = {
  code: "invalid" | "required" | "unknown";
  field: "body" | "email" | "password";
};

const emailPattern =
  /^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$/;
const passwordPattern = /^[\x20-\x7e]{8,128}$/;

function validateCredentials(
  body: unknown,
): { details: ErrorDetail[] } | { credentials: Credentials } {
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return { details: [{ field: "body", code: "invalid" }] };
  }

  const record = body as Record<string, unknown>;
  const details: ErrorDetail[] = [];
  if (Object.keys(record).some((field) => field !== "email" && field !== "password")) {
    details.push({ field: "body", code: "unknown" });
  }

  for (const field of ["email", "password"] as const) {
    if (!Object.hasOwn(record, field)) {
      details.push({ field, code: "required" });
      continue;
    }
    const value = record[field];
    const valid =
      typeof value === "string" &&
      (field === "email"
        ? value.length <= 254 && emailPattern.test(value)
        : passwordPattern.test(value));
    if (!valid) {
      details.push({ field, code: "invalid" });
    }
  }

  if (details.length > 0) {
    details.sort((left, right) =>
      left.field < right.field ? -1 : left.field > right.field ? 1 : 0,
    );
    return { details };
  }
  return { credentials: record as Credentials };
}

const requireJsonContentType: RequestHandler = (request, response, next) => {
  const contentType = request.headers["content-type"];
  const accepted =
    typeof contentType === "string" &&
    /^application\/json(?:\s*;\s*charset\s*=\s*(?:utf-8|"utf-8"))?\s*$/i.test(contentType);
  if (!accepted) {
    response.status(415).json({ error: "unsupported_media_type" });
    return;
  }
  next();
};

const parseRegistrationJson = express.json({
  limit: 4_096,
  strict: false,
  type: () => true,
});

const handleRegistrationJsonError: ErrorRequestHandler = (error, _request, response, next) => {
  const errorType =
    typeof error === "object" && error !== null && "type" in error ? error.type : undefined;
  if (errorType === "entity.too.large") {
    response.status(413).json({ error: "payload_too_large" });
    return;
  }
  if (errorType === "entity.parse.failed") {
    response.status(400).json({ error: "invalid_json" });
    return;
  }
  next(error);
};

app.disable("x-powered-by");
app.post(
  "/api/auth/register",
  requireJsonContentType,
  parseRegistrationJson,
  handleRegistrationJsonError,
  async (request: Request, response: Response) => {
    const validation = validateCredentials(request.body);
    if ("details" in validation) {
      response.status(400).json({ error: "invalid_request", details: validation.details });
      return;
    }
    const { credentials } = validation;
    const email = credentials.email.toLowerCase();
    const salt = randomBytes(16);
    const tag = await new Promise<Buffer>((resolve, reject) => {
      argon2(
        "argon2id",
        {
          memory: 65_536,
          message: Buffer.from(credentials.password, "ascii"),
          nonce: salt,
          parallelism: 4,
          passes: 3,
          tagLength: 32,
        },
        (error, derivedKey) => {
          if (error) {
            reject(error);
            return;
          }
          resolve(derivedKey);
        },
      );
    });
    const passwordHash = [
      "$argon2id$v=19$m=65536,t=3,p=4",
      salt.toString("base64").replace(/=+$/, ""),
      tag.toString("base64").replace(/=+$/, ""),
    ].join("$");

    try {
      const result = await database.execute<{
        created_at: string;
        email: string;
        id: string;
      }>(sql`
        INSERT INTO users (email, password_hash)
        VALUES (${email}, ${passwordHash})
        RETURNING
          id,
          email,
          to_char(
            created_at AT TIME ZONE 'UTC',
            'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'
          ) AS created_at
      `);
      const user = result.rows[0];
      if (!user) {
        response.status(503).json({ error: "unavailable" });
        return;
      }

      response.status(201).json({
        id: user.id,
        email: user.email,
        createdAt: user.created_at,
      });
    } catch (error) {
      if (postgresErrorCode(error) === "23505") {
        response.status(409).json({ error: "conflict" });
        return;
      }
      response.status(503).json({ error: "unavailable" });
    }
  },
);
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
