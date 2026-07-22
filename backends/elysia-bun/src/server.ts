import { hash, verify } from "@node-rs/argon2";
import { SQL } from "bun";
import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { sql } from "drizzle-orm";
import { drizzle } from "drizzle-orm/bun-sql";
import { Elysia } from "elysia";
import { links } from "../drizzle/schema.ts";

const databaseUrl = process.env.DATABASE_URL;
const expectedMigrationVersion = process.env.EXPECTED_MIGRATION_VERSION;
const port = Number(process.env.PORT ?? "3000");

if (!databaseUrl || !expectedMigrationVersion || !Number.isInteger(port)) {
  throw new Error("DATABASE_URL, EXPECTED_MIGRATION_VERSION, and a valid PORT are required");
}

// Public benchmark fixture from benchmark/fixtures/jwt-hs256.key. This fixed
// key is reproducible test input, not an operational secret.
const publicBenchmarkJwtKey = Buffer.from("PUBLIC-LINK-METRICS-JWT-KEY-v1!!", "ascii");
const emailPattern =
  /^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$/;
const passwordPattern = /^[\x20-\x7e]{8,128}$/;
const destinationPattern =
  /^https?:\/\/(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?(?:[/?#][\x21-\x7e]*)?$/;
const shortCodePattern = /^[0-9A-Za-z]{8}$/;
const jsonContentTypePattern = /^application\/json(?:\s*;\s*charset\s*=\s*(?:utf-8|"utf-8"))?\s*$/i;
const argon2idAlgorithm = 2 as const;

type Credentials = { email: string; password: string };
type ErrorDetail = {
  code: "invalid" | "required" | "unknown";
  field: "body" | "email" | "password" | "url";
};

const connectionUrl = new URL(databaseUrl);
connectionUrl.searchParams.set("options", "-c statement_timeout=2000");
const client = new SQL({
  connectionTimeout: 2,
  idleTimeout: 2,
  max: 20,
  maxLifetime: 0,
  url: connectionUrl.toString(),
});
const database = drizzle({ client });

function json(value: unknown, status: number): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function postgresErrorCode(error: unknown): string | undefined {
  let current = error;
  while (current instanceof Error) {
    if ("errno" in current && typeof current.errno === "string") return current.errno;
    if ("code" in current && typeof current.code === "string") return current.code;
    current = current.cause;
  }
  return undefined;
}

function requestRecord(body: unknown): Record<string, unknown> | undefined {
  return typeof body === "object" && body !== null && !Array.isArray(body)
    ? (body as Record<string, unknown>)
    : undefined;
}

function sortedDetails(details: ErrorDetail[]): ErrorDetail[] {
  return details.sort((left, right) => left.field.localeCompare(right.field));
}

function credentialsFrom(body: unknown): { details: ErrorDetail[] } | { value: Credentials } {
  const record = requestRecord(body);
  if (!record) return { details: [{ field: "body", code: "invalid" }] };

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
    if (!valid) details.push({ field, code: "invalid" });
  }
  return details.length > 0
    ? { details: sortedDetails(details) }
    : { value: record as Credentials };
}

function destinationFrom(body: unknown): { details: ErrorDetail[] } | { value: string } {
  const record = requestRecord(body);
  if (!record) return { details: [{ field: "body", code: "invalid" }] };

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
  return details.length > 0 ? { details: sortedDetails(details) } : { value: record.url as string };
}

async function jsonBody(request: Request): Promise<Response | unknown> {
  const contentType = request.headers.get("content-type");
  if (!contentType || !jsonContentTypePattern.test(contentType)) {
    return json({ error: "unsupported_media_type" }, 415);
  }

  const contentLength = Number(request.headers.get("content-length"));
  if (Number.isFinite(contentLength) && contentLength > 4_096) {
    return json({ error: "payload_too_large" }, 413);
  }

  const bytes = await request.arrayBuffer();
  if (bytes.byteLength > 4_096) return json({ error: "payload_too_large" }, 413);
  try {
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)) as unknown;
  } catch {
    return json({ error: "invalid_json" }, 400);
  }
}

function canonicalBase64HasLength(value: string, byteLength: number): boolean {
  const bytes = Buffer.from(value, "base64");
  return bytes.length === byteLength && bytes.toString("base64").replace(/=+$/, "") === value;
}

async function passwordMatches(password: string, encodedHash: string): Promise<boolean> {
  const match = /^\$argon2id\$v=19\$m=65536,t=3,p=4\$([A-Za-z0-9+/]+)\$([A-Za-z0-9+/]+)$/.exec(
    encodedHash,
  );
  if (
    !match ||
    !canonicalBase64HasLength(match[1]!, 16) ||
    !canonicalBase64HasLength(match[2]!, 32)
  ) {
    return false;
  }
  try {
    return await verify(encodedHash, password);
  } catch {
    return false;
  }
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

function jwtObject(encoded: string): Record<string, unknown> | undefined {
  try {
    const bytes = Buffer.from(encoded, "base64url");
    const value: unknown = JSON.parse(bytes.toString("utf8"));
    return bytes.toString("base64url") === encoded &&
      typeof value === "object" &&
      value !== null &&
      !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : undefined;
  } catch {
    return undefined;
  }
}

function authenticatedSubject(token: string): string | undefined {
  const parts = token.split(".");
  if (parts.length !== 3) return undefined;
  const [encodedHeader, encodedClaims, encodedSignature] = parts;
  if (!encodedHeader || !encodedClaims || !encodedSignature) return undefined;
  const header = jwtObject(encodedHeader);
  const claims = jwtObject(encodedClaims);
  if (header?.alg !== "HS256" || header.typ !== "JWT" || !claims) return undefined;

  const signature = Buffer.from(encodedSignature, "base64url");
  const expected = createHmac("sha256", publicBenchmarkJwtKey)
    .update(`${encodedHeader}.${encodedClaims}`)
    .digest();
  if (
    signature.toString("base64url") !== encodedSignature ||
    signature.length !== 32 ||
    !timingSafeEqual(signature, expected)
  ) {
    return undefined;
  }

  const now = Math.floor(Date.now() / 1_000);
  return claims.iss === "link-metrics" &&
    claims.aud === "link-metrics-api" &&
    typeof claims.iat === "number" &&
    Number.isInteger(claims.iat) &&
    typeof claims.exp === "number" &&
    Number.isInteger(claims.exp) &&
    claims.exp > now &&
    typeof claims.sub === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(claims.sub)
    ? claims.sub
    : undefined;
}

function authenticate(request: Request): Response | string {
  const authorization = request.headers.get("authorization");
  const match = authorization ? /^Bearer ([^\s]+)$/i.exec(authorization) : null;
  const subject = match ? authenticatedSubject(match[1]!) : undefined;
  return subject ?? json({ error: "unauthorized" }, 401);
}

const app = new Elysia()
  .get("/health", async () => {
    try {
      const result = await database.execute<{ version: string }>(sql`
        SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1
      `);
      if (result[0]?.version === expectedMigrationVersion)
        return new Response(null, { status: 204 });
    } catch {
      // Readiness failures use the canonical response below.
    }
    return json({ error: "unavailable" }, 503);
  })
  .post(
    "/api/auth/register",
    async ({ request }) => {
      const parsed = await jsonBody(request);
      if (parsed instanceof Response) return parsed;
      const validated = credentialsFrom(parsed);
      if ("details" in validated) {
        return json({ error: "invalid_request", details: validated.details }, 400);
      }

      try {
        const passwordHash = await hash(validated.value.password, {
          algorithm: argon2idAlgorithm,
          memoryCost: 65_536,
          outputLen: 32,
          parallelism: 4,
          salt: randomBytes(16),
          timeCost: 3,
        });
        const result = await database.execute<{
          created_at: string;
          email: string;
          id: string;
        }>(sql`
          INSERT INTO users (email, password_hash)
          VALUES (${validated.value.email.toLowerCase()}, ${passwordHash})
          RETURNING id, email, to_char(
            created_at AT TIME ZONE 'UTC',
            'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'
          ) AS created_at
        `);
        const user = result[0];
        if (!user) return json({ error: "unavailable" }, 503);
        return json({ id: user.id, email: user.email, createdAt: user.created_at }, 201);
      } catch (error) {
        if (postgresErrorCode(error) === "23505") return json({ error: "conflict" }, 409);
        return json({ error: "unavailable" }, 503);
      }
    },
    { parse: "none" },
  )
  .post(
    "/api/auth/login",
    async ({ request }) => {
      const parsed = await jsonBody(request);
      if (parsed instanceof Response) return parsed;
      const validated = credentialsFrom(parsed);
      if ("details" in validated) {
        return json({ error: "invalid_request", details: validated.details }, 400);
      }

      try {
        const result = await database.execute<{ id: string; password_hash: string }>(sql`
          SELECT id, password_hash FROM users
          WHERE email = ${validated.value.email.toLowerCase()}
          LIMIT 1
        `);
        const user = result[0];
        if (!user || !(await passwordMatches(validated.value.password, user.password_hash))) {
          return json({ error: "unauthorized" }, 401);
        }
        return json({ token: issueToken(user.id) }, 200);
      } catch {
        return json({ error: "unavailable" }, 503);
      }
    },
    { parse: "none" },
  )
  .post(
    "/api/links",
    async ({ request }) => {
      const subject = authenticate(request);
      if (subject instanceof Response) return subject;
      const parsed = await jsonBody(request);
      if (parsed instanceof Response) return parsed;
      const validated = destinationFrom(parsed);
      if ("details" in validated) {
        return json({ error: "invalid_request", details: validated.details }, 400);
      }

      try {
        const result = await database
          .insert(links)
          .values({ originalUrl: validated.value, userId: subject })
          .returning({
            userId: links.userId,
            shortCode: links.shortCode,
            originalUrl: links.originalUrl,
            createdAt: sql<string>`to_char(
              ${links.createdAt} AT TIME ZONE 'UTC',
              'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'
            )`,
          });
        if (!result[0]) return json({ error: "unavailable" }, 503);
        return json(result[0], 201);
      } catch {
        return json({ error: "unavailable" }, 503);
      }
    },
    { parse: "none" },
  )
  .get("/api/links/:shortCode/stats", async ({ request, params }) => {
    const subject = authenticate(request);
    if (subject instanceof Response) return subject;
    if (!shortCodePattern.test(params.shortCode)) return json({ error: "not_found" }, 404);

    try {
      const result = await database.execute<{
        click_count: string;
        last_clicked_at: string | null;
        original_url: string;
        short_code: string;
      }>(sql`
        SELECT short_code, original_url, click_count, to_char(
          last_clicked_at AT TIME ZONE 'UTC',
          'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'
        ) AS last_clicked_at
        FROM links
        WHERE short_code = ${params.shortCode}
          AND user_id = ${subject}
        LIMIT 1
      `);
      const shortLink = result[0];
      if (!shortLink) return json({ error: "not_found" }, 404);
      return new Response(
        `{"shortCode":${JSON.stringify(shortLink.short_code)},` +
          `"originalUrl":${JSON.stringify(shortLink.original_url)},` +
          `"clickCount":${shortLink.click_count},` +
          `"lastClickedAt":${JSON.stringify(shortLink.last_clicked_at)}}`,
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    } catch {
      return json({ error: "unavailable" }, 503);
    }
  })
  .get("/:shortCode", async ({ params }) => {
    if (!shortCodePattern.test(params.shortCode)) return json({ error: "not_found" }, 404);

    try {
      const result = await database.execute<{ original_url: string }>(sql`
        UPDATE links
        SET click_count = click_count + 1,
            last_clicked_at = clock_timestamp()
        WHERE short_code = ${params.shortCode}
        RETURNING original_url
      `);
      const shortLink = result[0];
      if (!shortLink) return json({ error: "not_found" }, 404);
      return new Response(null, {
        status: 302,
        headers: { Location: shortLink.original_url },
      });
    } catch {
      return json({ error: "unavailable" }, 503);
    }
  })
  .onError(({ code, error }) => {
    if (code === "NOT_FOUND") return json({ error: "not_found" }, 404);
    console.error("Unexpected Elysia request error", error);
    return json({ error: "unavailable" }, 503);
  })
  .listen(port);

console.log(`Elysia on Bun listening on port ${app.server?.port ?? port}`);
