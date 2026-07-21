import "reflect-metadata";

import {
  Body,
  CanActivate,
  Controller,
  ExecutionContext,
  Get,
  HttpCode,
  HttpException,
  Injectable,
  Module,
  OnModuleDestroy,
  Param,
  Post,
  Res,
  UseGuards,
} from "@nestjs/common";
import { NestFactory } from "@nestjs/core";
import { ExpressAdapter } from "@nestjs/platform-express";
import { argon2, createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { sql } from "drizzle-orm";
import { drizzle } from "drizzle-orm/node-postgres";
import express from "express";
import type { ErrorRequestHandler, Request, RequestHandler, Response } from "express";
import { Pool } from "pg";
import { links } from "../drizzle/schema.js";

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

type Credentials = { email: string; password: string };
type ErrorDetail = {
  code: "invalid" | "required" | "unknown";
  field: "body" | "email" | "password" | "url";
};

function fail(status: number, error: string, details?: ErrorDetail[]): never {
  throw new HttpException(details ? { error, details } : { error }, status);
}

function requestRecord(body: unknown): Record<string, unknown> | undefined {
  return typeof body === "object" && body !== null && !Array.isArray(body)
    ? (body as Record<string, unknown>)
    : undefined;
}

function sortedDetails(details: ErrorDetail[]): ErrorDetail[] {
  return details.sort((left, right) => left.field.localeCompare(right.field));
}

function credentialsFrom(body: unknown): Credentials {
  const record = requestRecord(body);
  if (!record) {
    return fail(400, "invalid_request", [{ field: "body", code: "invalid" }]);
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
    return fail(400, "invalid_request", sortedDetails(details));
  }
  return record as Credentials;
}

function destinationFrom(body: unknown): string {
  const record = requestRecord(body);
  if (!record) {
    return fail(400, "invalid_request", [{ field: "body", code: "invalid" }]);
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
    return fail(400, "invalid_request", sortedDetails(details));
  }
  return record.url as string;
}

async function derivePassword(password: string, nonce: Buffer): Promise<Buffer> {
  return new Promise((resolve, reject) => {
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
      (error, tag) => (error ? reject(error) : resolve(tag)),
    );
  });
}

async function passwordMatches(password: string, encodedHash: string): Promise<boolean> {
  const match = /^\$argon2id\$v=19\$m=65536,t=3,p=4\$([A-Za-z0-9+/]+)\$([A-Za-z0-9+/]+)$/.exec(
    encodedHash,
  );
  if (!match) return false;
  const nonce = Buffer.from(match[1]!, "base64");
  const expectedTag = Buffer.from(match[2]!, "base64");
  if (nonce.length !== 16 || expectedTag.length !== 32) return false;
  return timingSafeEqual(await derivePassword(password, nonce), expectedTag);
}

function issueToken(userId: string): string {
  const issuedAt = Math.floor(Date.now() / 1_000);
  const header = Buffer.from(JSON.stringify({ alg: "HS256", typ: "JWT" })).toString("base64url");
  const claims = Buffer.from(
    JSON.stringify({
      sub: userId,
      iss: "link-metrics",
      aud: "link-metrics-api",
      iat: issuedAt,
      exp: issuedAt + 900,
    }),
  ).toString("base64url");
  const input = `${header}.${claims}`;
  const signature = createHmac("sha256", publicBenchmarkJwtKey).update(input).digest("base64url");
  return `${input}.${signature}`;
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

function requestSubject(request: Request): string | undefined {
  const authorization = request.headers.authorization;
  const match = typeof authorization === "string" ? /^Bearer ([^\s]+)$/i.exec(authorization) : null;
  return match ? authenticatedSubject(match[1]!) : undefined;
}

function postgresErrorCode(error: unknown): string | undefined {
  let current = error;
  while (current instanceof Error) {
    if ("code" in current && typeof current.code === "string") return current.code;
    current = current.cause;
  }
  return undefined;
}

function unavailableUnlessHttpException(error: unknown): never {
  if (error instanceof HttpException) throw error;
  return fail(503, "unavailable");
}

@Injectable()
class Database implements OnModuleDestroy {
  private readonly pool = new Pool({
    connectionString: databaseUrl,
    connectionTimeoutMillis: 2_000,
    max: 20,
    options: "-c statement_timeout=2000",
    query_timeout: 2_000,
  });
  readonly db = drizzle(this.pool);

  constructor() {
    this.pool.on("error", (error) => console.error("Unexpected idle PostgreSQL pool error", error));
  }

  async onModuleDestroy(): Promise<void> {
    await this.pool.end();
  }
}

@Injectable()
class BearerGuard implements CanActivate {
  canActivate(context: ExecutionContext): boolean {
    const request = context.switchToHttp().getRequest<Request>();
    const establishedSubject = request.res?.locals.userId;
    const subject =
      typeof establishedSubject === "string" ? establishedSubject : requestSubject(request);
    if (!subject) fail(401, "unauthorized");
    request.res!.locals.userId = subject;
    return true;
  }
}

@Controller("api/auth")
class AuthenticationController {
  constructor(private readonly database: Database) {}

  @Post("register")
  async register(@Body() body: unknown): Promise<Record<string, unknown>> {
    const credentials = credentialsFrom(body);
    try {
      const salt = randomBytes(16);
      const tag = await derivePassword(credentials.password, salt);
      const passwordHash = [
        "$argon2id$v=19$m=65536,t=3,p=4",
        salt.toString("base64").replace(/=+$/, ""),
        tag.toString("base64").replace(/=+$/, ""),
      ].join("$");
      const result = await this.database.db.execute<{
        created_at: string;
        email: string;
        id: string;
      }>(sql`
        INSERT INTO users (email, password_hash)
        VALUES (${credentials.email.toLowerCase()}, ${passwordHash})
        RETURNING id, email, to_char(
          created_at AT TIME ZONE 'UTC',
          'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'
        ) AS created_at
      `);
      const user = result.rows[0];
      if (!user) return fail(503, "unavailable");
      return { id: user.id, email: user.email, createdAt: user.created_at };
    } catch (error) {
      if (postgresErrorCode(error) === "23505") return fail(409, "conflict");
      return fail(503, "unavailable");
    }
  }

  @Post("login")
  @HttpCode(200)
  async login(@Body() body: unknown): Promise<{ token: string }> {
    const credentials = credentialsFrom(body);
    try {
      const result = await this.database.db.execute<{ id: string; password_hash: string }>(sql`
        SELECT id, password_hash
        FROM users
        WHERE email = ${credentials.email.toLowerCase()}
        LIMIT 1
      `);
      const user = result.rows[0];
      if (!user || !(await passwordMatches(credentials.password, user.password_hash))) {
        return fail(401, "unauthorized");
      }
      return { token: issueToken(user.id) };
    } catch (error) {
      return unavailableUnlessHttpException(error);
    }
  }
}

@Controller("api/links")
@UseGuards(BearerGuard)
class ShortLinksController {
  constructor(private readonly database: Database) {}

  @Post()
  async create(@Body() body: unknown, @Res() response: Response): Promise<void> {
    const destination = destinationFrom(body);
    try {
      const rows = await this.database.db
        .insert(links)
        .values({ originalUrl: destination, userId: response.locals.userId as string })
        .returning({
          userId: links.userId,
          shortCode: links.shortCode,
          originalUrl: links.originalUrl,
          createdAt: sql<string>`to_char(
            ${links.createdAt} AT TIME ZONE 'UTC',
            'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'
          )`,
        });
      if (!rows[0]) return fail(503, "unavailable");
      response.status(201).json(rows[0]);
    } catch (error) {
      return unavailableUnlessHttpException(error);
    }
  }

  @Get(":shortCode/stats")
  async statistics(
    @Param("shortCode") shortCode: string,
    @Res() response: Response,
  ): Promise<void> {
    if (!shortCodePattern.test(shortCode)) return fail(404, "not_found");
    try {
      const result = await this.database.db.execute<{
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
        WHERE short_code = ${shortCode}
          AND user_id = ${response.locals.userId as string}
        LIMIT 1
      `);
      const shortLink = result.rows[0];
      if (!shortLink) return fail(404, "not_found");
      response
        .status(200)
        .type("application/json")
        .send(
          `{"shortCode":${JSON.stringify(shortLink.short_code)},` +
            `"originalUrl":${JSON.stringify(shortLink.original_url)},` +
            `"clickCount":${shortLink.click_count},` +
            `"lastClickedAt":${JSON.stringify(shortLink.last_clicked_at)}}`,
        );
    } catch (error) {
      return unavailableUnlessHttpException(error);
    }
  }
}

@Controller()
class OperationsController {
  constructor(private readonly database: Database) {}

  @Get("health")
  async health(@Res() response: Response): Promise<void> {
    try {
      const result = await this.database.db.execute<{ version: string }>(sql`
        SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1
      `);
      if (result.rows[0]?.version === expectedMigrationVersion) {
        response.status(204).end();
        return;
      }
    } catch {
      // Readiness failures use the canonical response below.
    }
    response.status(503).json({ error: "unavailable" });
  }

  @Get(":shortCode")
  async resolve(@Param("shortCode") shortCode: string, @Res() response: Response): Promise<void> {
    if (!shortCodePattern.test(shortCode)) return fail(404, "not_found");
    try {
      const result = await this.database.db.execute<{ original_url: string }>(sql`
        UPDATE links
        SET click_count = click_count + 1, last_clicked_at = clock_timestamp()
        WHERE short_code = ${shortCode}
        RETURNING original_url
      `);
      const shortLink = result.rows[0];
      if (!shortLink) return fail(404, "not_found");
      response.status(302).set("Location", shortLink.original_url).end();
    } catch (error) {
      return unavailableUnlessHttpException(error);
    }
  }
}

@Module({
  controllers: [AuthenticationController, ShortLinksController, OperationsController],
  providers: [BearerGuard, Database],
})
class AppModule {}

const requireJson: RequestHandler = (request, response, next) => {
  const contentType = request.headers["content-type"];
  if (
    typeof contentType !== "string" ||
    !/^application\/json(?:\s*;\s*charset\s*=\s*(?:utf-8|"utf-8"))?\s*$/i.test(contentType)
  ) {
    response.status(415).json({ error: "unsupported_media_type" });
    return;
  }
  next();
};

const authenticateBeforeBody: RequestHandler = (request, response, next) => {
  const subject = requestSubject(request);
  if (!subject) {
    response.status(401).json({ error: "unauthorized" });
    return;
  }
  response.locals.userId = subject;
  next();
};

const jsonParser = express.json({ limit: 4_096, strict: false, type: () => true });
const jsonError: ErrorRequestHandler = (error, _request, response, next) => {
  const type =
    typeof error === "object" && error !== null && "type" in error ? error.type : undefined;
  if (type === "entity.too.large") {
    response.status(413).json({ error: "payload_too_large" });
    return;
  }
  if (type === "entity.parse.failed") {
    response.status(400).json({ error: "invalid_json" });
    return;
  }
  next(error);
};

async function bootstrap(): Promise<void> {
  const server = express();
  server.disable("x-powered-by");
  server.post("/api/auth/register", requireJson, jsonParser);
  server.post("/api/auth/login", requireJson, jsonParser);
  server.post("/api/links", authenticateBeforeBody, requireJson, jsonParser);
  server.use(jsonError);

  const app = await NestFactory.create(AppModule, new ExpressAdapter(server), {
    bodyParser: false,
    logger: ["error", "warn"],
  });
  app.enableShutdownHooks();
  await app.listen(port, "0.0.0.0");
  console.log(`NestJS Express Contender listening on port ${port}`);
}

await bootstrap();
