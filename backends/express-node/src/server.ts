import express from "express";
import { argon2, createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { drizzle } from "drizzle-orm/node-postgres";
import { sql } from "drizzle-orm";
import { Pool } from "pg";
import type { ErrorRequestHandler, Request, RequestHandler, Response } from "express";
import { links } from "../drizzle/schema.js";

const databaseUrl = process.env.DATABASE_URL;
const expectedMigrationVersion = process.env.EXPECTED_MIGRATION_VERSION;
const port = Number(process.env.PORT ?? "3000");

// Public benchmark fixture from benchmark/fixtures/jwt-hs256.key. This fixed key
// is reproducible test input, not an operational secret.
const publicBenchmarkJwtKey = Buffer.from("PUBLIC-LINK-METRICS-JWT-KEY-v1!!", "ascii");

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
  field: "body" | "email" | "password" | "url";
};

const emailPattern =
  /^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$/;
const passwordPattern = /^[\x20-\x7e]{8,128}$/;
const destinationPattern =
  /^https?:\/\/(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?(?:[/?#][\x21-\x7e]*)?$/;
const shortCodePattern = /^[0-9A-Za-z]{8}$/;

function requestRecord(body: unknown): Record<string, unknown> | undefined {
  return typeof body === "object" && body !== null && !Array.isArray(body)
    ? (body as Record<string, unknown>)
    : undefined;
}

function sortedErrorDetails(details: ErrorDetail[]): ErrorDetail[] {
  return details.sort((left, right) =>
    left.field < right.field ? -1 : left.field > right.field ? 1 : 0,
  );
}

function validateCredentials(
  body: unknown,
): { details: ErrorDetail[] } | { credentials: Credentials } {
  const record = requestRecord(body);
  if (!record) {
    return { details: [{ field: "body", code: "invalid" }] };
  }

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
    return { details: sortedErrorDetails(details) };
  }
  return { credentials: record as Credentials };
}

function validateShortLink(body: unknown): { details: ErrorDetail[] } | { destination: string } {
  const record = requestRecord(body);
  if (!record) {
    return { details: [{ field: "body", code: "invalid" }] };
  }

  const details: ErrorDetail[] = [];
  if (Object.keys(record).some((field) => field !== "url")) {
    details.push({ field: "body", code: "unknown" });
  }
  if (!Object.hasOwn(record, "url")) {
    details.push({ field: "url", code: "required" });
  } else if (
    typeof record.url !== "string" ||
    record.url.length > 2_048 ||
    !destinationPattern.test(record.url)
  ) {
    details.push({ field: "url", code: "invalid" });
  }

  if (details.length > 0) {
    return { details: sortedErrorDetails(details) };
  }
  return { destination: record.url as string };
}

async function verifyPassword(password: string, encodedHash: string): Promise<boolean> {
  const match = /^\$argon2id\$v=19\$m=65536,t=3,p=4\$([A-Za-z0-9+/]+)\$([A-Za-z0-9+/]+)$/.exec(
    encodedHash,
  );
  if (!match) {
    return false;
  }

  const nonce = Buffer.from(match[1]!, "base64");
  const expectedTag = Buffer.from(match[2]!, "base64");
  if (nonce.length !== 16 || expectedTag.length !== 32) {
    return false;
  }

  const actualTag = await new Promise<Buffer>((resolve, reject) => {
    argon2(
      "argon2id",
      {
        memory: 65_536,
        message: Buffer.from(password, "ascii"),
        nonce,
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
  return timingSafeEqual(actualTag, expectedTag);
}

function issueToken(userId: string): string {
  const issuedAt = Math.floor(Date.now() / 1_000);
  const encodedHeader = Buffer.from(JSON.stringify({ alg: "HS256", typ: "JWT" })).toString(
    "base64url",
  );
  const encodedClaims = Buffer.from(
    JSON.stringify({
      sub: userId,
      iss: "link-metrics",
      aud: "link-metrics-api",
      iat: issuedAt,
      exp: issuedAt + 900,
    }),
  ).toString("base64url");
  const signingInput = `${encodedHeader}.${encodedClaims}`;
  const signature = createHmac("sha256", publicBenchmarkJwtKey)
    .update(signingInput)
    .digest("base64url");
  return `${signingInput}.${signature}`;
}

function decodeJwtObject(encoded: string): Record<string, unknown> | undefined {
  try {
    const bytes = Buffer.from(encoded, "base64url");
    if (bytes.toString("base64url") !== encoded) {
      return undefined;
    }
    const value: unknown = JSON.parse(bytes.toString("utf8"));
    if (typeof value !== "object" || value === null || Array.isArray(value)) {
      return undefined;
    }
    return value as Record<string, unknown>;
  } catch {
    return undefined;
  }
}

function authenticatedSubject(token: string): string | undefined {
  const parts = token.split(".");
  if (parts.length !== 3) {
    return undefined;
  }
  const [encodedHeader, encodedClaims, encodedSignature] = parts;
  if (!encodedHeader || !encodedClaims || !encodedSignature) {
    return undefined;
  }

  const header = decodeJwtObject(encodedHeader);
  const claims = decodeJwtObject(encodedClaims);
  if (header?.alg !== "HS256" || header.typ !== "JWT" || !claims) {
    return undefined;
  }

  const signature = Buffer.from(encodedSignature, "base64url");
  if (signature.toString("base64url") !== encodedSignature || signature.length !== 32) {
    return undefined;
  }
  const expectedSignature = createHmac("sha256", publicBenchmarkJwtKey)
    .update(`${encodedHeader}.${encodedClaims}`)
    .digest();
  if (!timingSafeEqual(signature, expectedSignature)) {
    return undefined;
  }

  const now = Math.floor(Date.now() / 1_000);
  if (
    claims.iss !== "link-metrics" ||
    claims.aud !== "link-metrics-api" ||
    typeof claims.iat !== "number" ||
    !Number.isInteger(claims.iat) ||
    typeof claims.exp !== "number" ||
    !Number.isInteger(claims.exp) ||
    claims.exp <= now ||
    typeof claims.sub !== "string" ||
    !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(claims.sub)
  ) {
    return undefined;
  }
  return claims.sub;
}

const authenticateBearer: RequestHandler = (request, response, next) => {
  const authorization = request.headers.authorization;
  const match = typeof authorization === "string" ? /^Bearer ([^\s]+)$/i.exec(authorization) : null;
  const subject = match ? authenticatedSubject(match[1]!) : undefined;
  if (!subject) {
    response.status(401).json({ error: "unauthorized" });
    return;
  }
  response.locals.userId = subject;
  next();
};

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

const parseJsonBody = express.json({
  limit: 4_096,
  strict: false,
  type: () => true,
});

const handleJsonBodyError: ErrorRequestHandler = (error, _request, response, next) => {
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
app.use("/api/links", authenticateBearer);
app.post(
  "/api/links",
  requireJsonContentType,
  parseJsonBody,
  handleJsonBodyError,
  async (request: Request, response: Response) => {
    const validation = validateShortLink(request.body);
    if ("details" in validation) {
      response.status(400).json({ error: "invalid_request", details: validation.details });
      return;
    }

    try {
      const shortLinks = await database
        .insert(links)
        .values({
          originalUrl: validation.destination,
          userId: response.locals.userId as string,
        })
        .returning({
          userId: links.userId,
          shortCode: links.shortCode,
          originalUrl: links.originalUrl,
          createdAt: sql<string>`to_char(
            ${links.createdAt} AT TIME ZONE 'UTC',
            'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'
          )`,
        });
      const shortLink = shortLinks[0];
      if (!shortLink) {
        response.status(503).json({ error: "unavailable" });
        return;
      }

      response.status(201).json(shortLink);
    } catch {
      response.status(503).json({ error: "unavailable" });
    }
  },
);
app.get("/api/links/:shortCode/stats", async (request, response) => {
  const shortCode = request.params.shortCode;
  if (!shortCode || !shortCodePattern.test(shortCode)) {
    response.status(404).json({ error: "not_found" });
    return;
  }

  try {
    const result = await database.execute<{
      click_count: string;
      last_clicked_at: string | null;
      original_url: string;
      short_code: string;
    }>(sql`
      SELECT
        short_code,
        original_url,
        click_count,
        to_char(
          last_clicked_at AT TIME ZONE 'UTC',
          'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'
        ) AS last_clicked_at
      FROM links
      WHERE short_code = ${shortCode}
        AND user_id = ${response.locals.userId as string}
      LIMIT 1
    `);
    const shortLink = result.rows[0];
    if (!shortLink) {
      response.status(404).json({ error: "not_found" });
      return;
    }

    response
      .status(200)
      .type("application/json")
      .send(
        `{"shortCode":${JSON.stringify(shortLink.short_code)},` +
          `"originalUrl":${JSON.stringify(shortLink.original_url)},` +
          `"clickCount":${shortLink.click_count},` +
          `"lastClickedAt":${JSON.stringify(shortLink.last_clicked_at)}}`,
      );
  } catch {
    response.status(503).json({ error: "unavailable" });
  }
});
app.post(
  "/api/auth/login",
  requireJsonContentType,
  parseJsonBody,
  handleJsonBodyError,
  async (request: Request, response: Response) => {
    const validation = validateCredentials(request.body);
    if ("details" in validation) {
      response.status(400).json({ error: "invalid_request", details: validation.details });
      return;
    }

    try {
      const result = await database.execute<{ id: string; password_hash: string }>(sql`
        SELECT id, password_hash
        FROM users
        WHERE email = ${validation.credentials.email.toLowerCase()}
        LIMIT 1
      `);
      const user = result.rows[0];
      if (!user || !(await verifyPassword(validation.credentials.password, user.password_hash))) {
        response.status(401).json({ error: "unauthorized" });
        return;
      }

      response.status(200).json({ token: issueToken(user.id) });
    } catch {
      response.status(503).json({ error: "unavailable" });
    }
  },
);
app.post(
  "/api/auth/register",
  requireJsonContentType,
  parseJsonBody,
  handleJsonBodyError,
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
app.get("/:shortCode", async (request, response) => {
  const shortCode = request.params.shortCode;
  if (!shortCode || !shortCodePattern.test(shortCode)) {
    response.status(404).json({ error: "not_found" });
    return;
  }

  try {
    const result = await database.execute<{ original_url: string }>(sql`
      UPDATE links
      SET
        click_count = click_count + 1,
        last_clicked_at = clock_timestamp()
      WHERE short_code = ${shortCode}
      RETURNING original_url
    `);
    const shortLink = result.rows[0];
    if (!shortLink) {
      response.status(404).json({ error: "not_found" });
      return;
    }

    response.status(302).set("Location", shortLink.original_url).end();
  } catch {
    response.status(503).json({ error: "unavailable" });
  }
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
