import http from 'k6/http';
import { check } from 'k6';
import { SharedArray } from 'k6/data';
import exec from 'k6/execution';
import { Trend, Counter } from 'k6/metrics';

/**
 * Registration Scenario for Link Metrics Trials.
 *
 * Env:
 *   BASE_URL                 Contender origin, e.g. http://contender:3000
 *   OFFERED_RATE             constant-arrival-rate requests per second
 *   DURATION                 measure duration, e.g. 60s
 *   REPETITION               dataset repetition index (1..5)
 *   PASSWORD                 standardized benchmark password
 *   PRE_ALLOCATED_VUS        preAllocatedVUs for the executor
 *   MAX_VUS                  maxVUs for the executor
 *   VALIDATION_FLAGS_JSON    JSON boolean array; true => fully validate body
 */

const unexpectedResponses = new Counter('unexpected_responses');
const transportFailures = new Counter('transport_failures');
const bodyValidationFailures = new Counter('body_validation_failures');
const registrationDuration = new Trend('registration_duration', true);

const offeredRate = Number(__ENV.OFFERED_RATE || '1');
const duration = __ENV.DURATION || '5s';
const repetition = Number(__ENV.REPETITION || '1');
const password = __ENV.PASSWORD || 'link-metrics-benchmark-only';
const preAllocatedVUs = Number(__ENV.PRE_ALLOCATED_VUS || '20');
const maxVUs = Number(__ENV.MAX_VUS || '256');
const baseUrl = (__ENV.BASE_URL || 'http://127.0.0.1:3000').replace(/\/$/, '');

const validationFlags = new SharedArray('validationFlags', () => {
  if (__ENV.VALIDATION_FLAGS_PATH) {
    return JSON.parse(open(__ENV.VALIDATION_FLAGS_PATH));
  }
  if (__ENV.VALIDATION_FLAGS_JSON) {
    return JSON.parse(__ENV.VALIDATION_FLAGS_JSON);
  }
  return [];
});

export const options = {
  discardResponseBodies: false,
  // Explicit HTTP/1.1 keep-alive: do not disable connection reuse.
  noConnectionReuse: false,
  noVUConnectionReuse: false,
  scenarios: {
    registration: {
      executor: 'constant-arrival-rate',
      rate: offeredRate,
      timeUnit: '1s',
      duration,
      preAllocatedVUs,
      maxVUs,
    },
  },
  // Each request sets timeout: '5s' (httpTimeoutSeconds).
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

function registrationEmail(iteration) {
  const padded = String(iteration).padStart(12, '0');
  const rep = String(repetition).padStart(2, '0');
  return `reg-${rep}-${padded}@trial.invalid`;
}

function shouldValidateBody(iteration) {
  if (validationFlags.length > 0) {
    return Boolean(validationFlags[iteration]);
  }
  // Deterministic fallback used only when flags are omitted (script archive checks).
  return iteration % 100 === 0;
}

function isIso8601Z(value) {
  return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(value);
}

function isUuid(value) {
  return (
    typeof value === 'string' &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
      value,
    )
  );
}

function isJsonContentType(value) {
  const token = "[!#$%&'*+.^_`|~0-9A-Za-z-]+";
  const quoted = '"(?:[\\t !#-\\[\\]-~]|\\\\[\\t -~])*"';
  const parameter = new RegExp(`^${token}\\s*=\\s*(?:${token}|${quoted})$`);
  const parts = value.split(';');
  return (
    parts.shift().trim().toLowerCase() === 'application/json' &&
    parts.every((part) => parameter.test(part.trim()))
  );
}

export default function registration() {
  const iteration = exec.scenario.iterationInTest;
  const email = registrationEmail(iteration);
  const payload = JSON.stringify({ email, password });

  const response = http.post(`${baseUrl}/api/auth/register`, payload, {
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    timeout: '5s',
    tags: { scenario: 'registration' },
  });

  registrationDuration.add(response.timings.duration);

  if (response.status === 0) {
    transportFailures.add(1);
    return;
  }

  const contentType = String(
    response.headers['Content-Type'] || response.headers['content-type'] || '',
  );
  const statusOk = response.status === 201;
  const contentTypeOk = isJsonContentType(contentType);
  // Registration success has no contract-required response headers beyond Content-Type.
  const requiredHeadersOk = contentTypeOk;

  const lightweight = check(response, {
    'status is 201': () => statusOk,
    'content-type is application/json': () => contentTypeOk,
    'required headers present': () => requiredHeadersOk,
  });

  let unexpected = !lightweight;

  if (shouldValidateBody(iteration)) {
    let bodyOk = false;
    try {
      const body = response.json();
      bodyOk =
        statusOk &&
        contentTypeOk &&
        body !== null &&
        typeof body === 'object' &&
        Object.keys(body).sort().join(',') === 'createdAt,email,id' &&
        isUuid(body.id) &&
        body.email === email.toLowerCase() &&
        isIso8601Z(body.createdAt);
    } catch (error) {
      bodyOk = false;
    }
    const validated = check(response, {
      'body sample matches UserResponse': () => bodyOk,
    });
    if (!validated) {
      unexpected = true;
      bodyValidationFailures.add(1);
    }
  }

  if (unexpected) {
    unexpectedResponses.add(1);
  }
}

export function handleSummary(data) {
  const httpReq = data.metrics.http_reqs || { values: {} };
  const dropped = data.metrics.dropped_iterations || { values: { count: 0 } };
  const checks = data.metrics.checks || { values: {} };
  const durationMetric =
    data.metrics.registration_duration || data.metrics.http_req_duration || { values: {} };
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
      p90Ms: durationMetric.values['p(90)'] || 0,
      p95Ms: durationMetric.values['p(95)'] || 0,
      p99Ms: durationMetric.values['p(99)'] || 0,
      maxMs: durationMetric.values.max || 0,
    },
  };

  const summaryPath = __ENV.SUMMARY_PATH || 'summary.json';
  return {
    [summaryPath]: JSON.stringify(summary, null, 2),
  };
}
