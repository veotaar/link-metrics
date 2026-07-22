import http from "k6/http";
import { check } from "k6";
import { SharedArray } from "k6/data";
import exec from "k6/execution";
import encoding from "k6/encoding";
import { Counter, Trend } from "k6/metrics";

const unexpectedResponses = new Counter("unexpected_responses");
const transportFailures = new Counter("transport_failures");
const bodyValidationFailures = new Counter("body_validation_failures");
const scenarioDuration = new Trend("scenario_duration", true);

const offeredRate = Number(__ENV.OFFERED_RATE || "1");
const rateScale = Number.isInteger(offeredRate) ? 1 : 100;
const duration = __ENV.DURATION || "5s";
const repetition = Number(__ENV.REPETITION || "1");
const password = __ENV.PASSWORD || "link-metrics-benchmark-only";
const preAllocatedVUs = Number(__ENV.PRE_ALLOCATED_VUS || "20");
const maxVUs = Number(__ENV.MAX_VUS || "256");
const baseUrl = (__ENV.BASE_URL || "http://127.0.0.1:3000").replace(/\/$/, "");
const scenario = __ENV.SCENARIO || "registration";

function loadWorkloadSamples() {
  if (__ENV.WORKLOAD_PATH) {
    return JSON.parse(open(__ENV.WORKLOAD_PATH)).samples;
  }
  return {
    access: { uniform: ["00000001"], viral: ["00000001"] },
    users: [0],
    validation: [true],
  };
}

const validationSamples = new SharedArray(
  "validationSamples",
  () => loadWorkloadSamples().validation,
);
const userSamples = new SharedArray("userSamples", () => loadWorkloadSamples().users);
const uniformAccessSamples = new SharedArray(
  "uniformAccessSamples",
  () => loadWorkloadSamples().access.uniform,
);
const viralAccessSamples = new SharedArray(
  "viralAccessSamples",
  () => loadWorkloadSamples().access.viral,
);

const tokenCorpus = new SharedArray("referenceTokens", () => {
  if (__ENV.TOKENS_PATH) {
    return JSON.parse(open(__ENV.TOKENS_PATH)).tokens;
  }
  return [];
});

export const options = {
  discardResponseBodies: false,
  noConnectionReuse: false,
  noVUConnectionReuse: false,
  scenarios: {
    [scenario]: {
      executor: "constant-arrival-rate",
      rate: Math.round(offeredRate * rateScale),
      timeUnit: `${rateScale}s`,
      duration,
      preAllocatedVUs,
      maxVUs,
    },
  },
  summaryTrendStats: ["avg", "min", "med", "p(90)", "p(95)", "p(99)", "max"],
};

function registrationEmail(iteration) {
  const padded = String(iteration).padStart(12, "0");
  const rep = String(repetition).padStart(2, "0");
  return `reg-${rep}-${padded}@trial.invalid`;
}

function benchmarkEmail(userIndex) {
  return `benchmark-user-${String(userIndex).padStart(6, "0")}@example.invalid`;
}

function shouldValidateBody(iteration) {
  return validationSamples.length > 0
    ? Boolean(validationSamples[iteration % validationSamples.length])
    : iteration % 100 === 0;
}

function isJsonContentType(value) {
  const token = "[!#$%&'*+.^_`|~0-9A-Za-z-]+";
  const quoted = '"(?:[\\t !#-\\[\\]-~]|\\\\[\\t -~])*"';
  const parameter = new RegExp(`^${token}\\s*=\\s*(?:${token}|${quoted})$`);
  const parts = value.split(";");
  return (
    parts.shift().trim().toLowerCase() === "application/json" &&
    parts.every((part) => parameter.test(part.trim()))
  );
}

function isUuid(value) {
  return (
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)
  );
}

function isApiTimestamp(value) {
  return typeof value === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(value);
}

function isShortCode(value) {
  return typeof value === "string" && /^[0-9A-Za-z]{8}$/.test(value);
}

function contentType(response) {
  return String(response.headers["Content-Type"] || response.headers["content-type"] || "");
}

function responseStarted(response) {
  if (response.status !== 0) {
    return true;
  }
  transportFailures.add(1);
  return false;
}

function jsonSuccess(response, expectedStatus) {
  return check(response, {
    [`status is ${expectedStatus}`]: () => response.status === expectedStatus,
    "content-type is application/json": () => isJsonContentType(contentType(response)),
  });
}

function parseJson(response) {
  try {
    return response.json();
  } catch {
    return null;
  }
}

function tokenClaims(token) {
  try {
    const parts = token.split(".");
    if (parts.length !== 3 || parts.some((part) => part.length === 0)) {
      return null;
    }
    return JSON.parse(encoding.b64decode(parts[1], "rawurl", "s"));
  } catch {
    return null;
  }
}

function shortCodeIndex(shortCode) {
  const alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
  let value = 0;
  for (const character of shortCode) {
    value = value * alphabet.length + alphabet.indexOf(character);
  }
  return value - 1;
}

function expectedDestination(shortCode) {
  const index = shortCodeIndex(shortCode);
  const userIndex = Math.floor(index / 10);
  const ownedIndex = index % 10;
  return `https://benchmark.invalid/users/${userIndex}/links/${ownedIndex}`;
}

function registration(iteration) {
  const email = registrationEmail(iteration);
  const response = http.post(`${baseUrl}/api/auth/register`, JSON.stringify({ email, password }), {
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    timeout: "5s",
    tags: { scenario },
  });
  scenarioDuration.add(response.timings.duration);
  if (!responseStarted(response)) return true;
  let unexpected = !jsonSuccess(response, 201);
  if (shouldValidateBody(iteration)) {
    const body = parseJson(response);
    const valid = check(response, {
      "body sample matches UserResponse": () =>
        body !== null &&
        Object.keys(body).sort().join(",") === "createdAt,email,id" &&
        isUuid(body.id) &&
        body.email === email &&
        isApiTimestamp(body.createdAt),
    });
    if (!valid) bodyValidationFailures.add(1);
    unexpected ||= !valid;
  }
  return unexpected;
}

function login(iteration) {
  const email = benchmarkEmail(userSamples[iteration % userSamples.length]);
  const response = http.post(`${baseUrl}/api/auth/login`, JSON.stringify({ email, password }), {
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    timeout: "5s",
    tags: { scenario },
  });
  scenarioDuration.add(response.timings.duration);
  if (!responseStarted(response)) return true;
  let unexpected = !jsonSuccess(response, 200);
  if (shouldValidateBody(iteration)) {
    const body = parseJson(response);
    const claims = body && typeof body.token === "string" ? tokenClaims(body.token) : null;
    const valid = check(response, {
      "body sample matches TokenResponse": () =>
        body !== null &&
        Object.keys(body).join(",") === "token" &&
        claims !== null &&
        claims.iss === "link-metrics" &&
        claims.aud === "link-metrics-api" &&
        Number.isInteger(claims.iat) &&
        claims.exp === claims.iat + 900 &&
        typeof claims.sub === "string",
    });
    if (!valid) bodyValidationFailures.add(1);
    unexpected ||= !valid;
  }
  return unexpected;
}

function referenceEntry(iteration) {
  if (tokenCorpus.length === 0) {
    throw new Error(`${scenario} requires a reference-token corpus`);
  }
  return tokenCorpus[iteration % tokenCorpus.length];
}

function shortLinkCreation(iteration) {
  const entry = referenceEntry(iteration);
  const destination = `https://benchmark.invalid/created/${repetition}/${iteration}?source=link-metrics#trial`;
  const response = http.post(`${baseUrl}/api/links`, JSON.stringify({ url: destination }), {
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
      Authorization: `Bearer ${entry.token}`,
    },
    timeout: "5s",
    tags: { scenario },
  });
  scenarioDuration.add(response.timings.duration);
  if (!responseStarted(response)) return true;
  let unexpected = !jsonSuccess(response, 201);
  if (shouldValidateBody(iteration)) {
    const body = parseJson(response);
    const valid = check(response, {
      "body sample matches ShortLinkResponse": () =>
        body !== null &&
        Object.keys(body).sort().join(",") === "createdAt,originalUrl,shortCode,userId" &&
        body.userId === entry.userId &&
        isShortCode(body.shortCode) &&
        body.originalUrl === destination &&
        isApiTimestamp(body.createdAt),
    });
    if (!valid) bodyValidationFailures.add(1);
    unexpected ||= !valid;
  }
  return unexpected;
}

function resolution(iteration, accessPattern) {
  const shortCodes = accessPattern === "uniform" ? uniformAccessSamples : viralAccessSamples;
  const shortCode = shortCodes[iteration % shortCodes.length];
  const destination = expectedDestination(shortCode);
  const response = http.get(`${baseUrl}/${shortCode}`, {
    redirects: 0,
    timeout: "5s",
    tags: { name: `${baseUrl}/:shortCode`, scenario },
  });
  scenarioDuration.add(response.timings.duration);
  if (!responseStarted(response)) return true;
  return !check(response, {
    "status is 302": () => response.status === 302,
    "Location matches seeded destination": () =>
      String(response.headers.Location || response.headers.location || "") === destination,
  });
}

function statistics(iteration) {
  const entry = referenceEntry(iteration);
  const response = http.get(`${baseUrl}/api/links/${entry.shortCode}/stats`, {
    headers: {
      Accept: "application/json",
      Authorization: `Bearer ${entry.token}`,
    },
    timeout: "5s",
    tags: { name: `${baseUrl}/api/links/:shortCode/stats`, scenario },
  });
  scenarioDuration.add(response.timings.duration);
  if (!responseStarted(response)) return true;
  let unexpected = !jsonSuccess(response, 200);
  if (shouldValidateBody(iteration)) {
    const body = parseJson(response);
    const linkIndex = shortCodeIndex(entry.shortCode);
    const userIndex = Math.floor(linkIndex / 10);
    const ownedIndex = linkIndex % 10;
    const neverClicked = ownedIndex < 5;
    const expectedLastClickedAt = neverClicked
      ? null
      : new Date(Date.UTC(2026, 0, 2) + linkIndex * 1000).toISOString();
    const valid = check(response, {
      "body sample matches ShortLinkStatsResponse": () =>
        body !== null &&
        Object.keys(body).sort().join(",") === "clickCount,lastClickedAt,originalUrl,shortCode" &&
        body.shortCode === entry.shortCode &&
        body.originalUrl === expectedDestination(entry.shortCode) &&
        body.clickCount === (neverClicked ? 0 : (userIndex + 1) * (ownedIndex - 4)) &&
        body.lastClickedAt === expectedLastClickedAt,
    });
    if (!valid) bodyValidationFailures.add(1);
    unexpected ||= !valid;
  }
  return unexpected;
}

const handlers = {
  registration,
  login,
  "short-link-creation": shortLinkCreation,
  "uniform-resolution": (iteration) => resolution(iteration, "uniform"),
  "viral-resolution": (iteration) => resolution(iteration, "viral"),
  statistics,
};

export default function runScenario() {
  const handler = handlers[scenario];
  if (!handler) {
    throw new Error(`unknown Scenario: ${scenario}`);
  }
  const iteration = exec.scenario.iterationInTest;
  if (handler(iteration)) {
    unexpectedResponses.add(1);
  }
}

export function handleSummary(data) {
  const httpReq = data.metrics.http_reqs || { values: {} };
  const dropped = data.metrics.dropped_iterations || { values: { count: 0 } };
  const checks = data.metrics.checks || { values: {} };
  const durationMetric = data.metrics.scenario_duration ||
    data.metrics.http_req_duration || { values: {} };
  const unexpected = data.metrics.unexpected_responses || { values: { count: 0 } };
  const transport = data.metrics.transport_failures || { values: { count: 0 } };
  const summary = {
    droppedIterations: dropped.values.count || 0,
    httpReqs: httpReq.values.count || 0,
    checksPassed: checks.values.passes || 0,
    checksFailed: checks.values.fails || 0,
    unexpectedResponses: unexpected.values.count || 0,
    transportFailures: transport.values.count || 0,
    latency: {
      avgMs: durationMetric.values.avg || 0,
      medMs: durationMetric.values.med || 0,
      p90Ms: durationMetric.values["p(90)"] || 0,
      p95Ms: durationMetric.values["p(95)"] || 0,
      p99Ms: durationMetric.values["p(99)"] || 0,
      maxMs: durationMetric.values.max || 0,
    },
  };
  return {
    [__ENV.SUMMARY_PATH || "summary.json"]: JSON.stringify(summary, null, 2),
  };
}
