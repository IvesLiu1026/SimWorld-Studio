"use strict";

const READINESS_SCHEMA = "simworld-readiness/v1";
const FEATURE_NAMES = Object.freeze([
  "artifact_journal",
  "review",
  "retrieval",
  "streaming",
  "timeline",
  "nlp_generation",
]);
const FEATURE_POLICIES = Object.freeze(["required", "optional", "disabled"]);
const PROBE_STATUSES = new Set(["ready", "degraded", "not_ready"]);
const MAX_PROBE_TIMEOUT_MS = 10_000;
const DEFAULT_PROBE_TIMEOUT_MS = 750;
const MAX_CAUSES = 16;
const MAX_PUBLIC_TEXT_LENGTH = 240;
const MAX_REVISION_DEPTH = 4;
const MAX_REVISION_ENTRIES = 32;
const SAFE_CODE = /^[A-Z][A-Z0-9_]{1,63}$/;
const SAFE_DEPENDENCY = /^[a-z0-9][a-z0-9_.-]{0,63}$/i;
const CONNECTION_URI = /\b(?:https?|postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|amqps?|grpc):\/\/[^\s,;]+/gi;
const AUTH_VALUE = /\b(?:bearer|basic)\s+[^\s,;]+/gi;
const SECRET_ASSIGNMENT = /\b(password|passwd|secret|token|api[_-]?key|authorization|cookie|dsn)\s*[:=]\s*[^\s,;]+/gi;

const PUBLIC_CAUSE_MESSAGES = Object.freeze({
  PROBE_NOT_CONFIGURED: "No readiness probe is configured for this feature.",
  PROBE_TIMEOUT: "The readiness probe exceeded its deadline.",
  PROBE_ABORTED: "The readiness probe was cancelled.",
  PROBE_FAILED: "The readiness probe failed.",
  PROBE_RESULT_INVALID: "The readiness probe returned an invalid result.",
  FEATURE_DEGRADED: "The feature reported degraded readiness.",
  FEATURE_NOT_READY: "The feature reported that it is not ready.",
});

class ProbeControlError extends Error {
  constructor(code) {
    super(code);
    this.name = "ProbeControlError";
    this.code = code;
  }
}

function assertPlainObject(value, field) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError(`${field} must be a plain object`);
  }
  const prototype = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null) {
    throw new TypeError(`${field} must be a plain object`);
  }
}

function readClock(now) {
  const value = now();
  const milliseconds = value instanceof Date ? value.getTime() : Number(value);
  if (!Number.isFinite(milliseconds)) throw new TypeError("now() must return a finite timestamp");
  return milliseconds;
}

function normalizeTimeout(value, field) {
  const timeoutMs = Number(value);
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > MAX_PROBE_TIMEOUT_MS) {
    throw new RangeError(`${field} must be an integer from 1 to ${MAX_PROBE_TIMEOUT_MS}`);
  }
  return timeoutMs;
}

function redactPublicText(value, fallback) {
  let text = typeof value === "string" ? value.trim() : "";
  if (!text) text = fallback;
  text = text
    .replace(CONNECTION_URI, "[redacted]")
    .replace(AUTH_VALUE, "[redacted]")
    .replace(SECRET_ASSIGNMENT, "$1=[redacted]")
    .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return text.slice(0, MAX_PUBLIC_TEXT_LENGTH) || fallback;
}

function revisionKeyIsSensitive(key) {
  const compact = String(key || "").toLowerCase().replace(/[^a-z0-9]/g, "");
  return [
    "authorization",
    "cookie",
    "credential",
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "privatekey",
    "dsn",
    "connectionstring",
  ].some((marker) => compact.includes(marker)) || compact.endsWith("url") || compact.endsWith("uri");
}

function sanitizeRevisionValue(value, depth = 0) {
  if (value == null) return null;
  if (typeof value === "string") {
    return redactPublicText(value, "[redacted]").slice(0, MAX_PUBLIC_TEXT_LENGTH);
  }
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (depth >= MAX_REVISION_DEPTH) return null;

  if (Array.isArray(value)) {
    return value
      .slice(0, MAX_REVISION_ENTRIES)
      .map((entry) => sanitizeRevisionValue(entry, depth + 1));
  }

  if (typeof value !== "object") return null;
  const prototype = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null) return null;

  const sanitized = {};
  for (const key of Object.keys(value).sort().slice(0, MAX_REVISION_ENTRIES)) {
    if (revisionKeyIsSensitive(key)) continue;
    const cleanKey = String(key).slice(0, 80);
    const cleanValue = sanitizeRevisionValue(value[key], depth + 1);
    if (cleanValue !== undefined) sanitized[cleanKey] = cleanValue;
  }
  return sanitized;
}

function sanitizeRevision(value) {
  if (value === undefined) return null;
  return sanitizeRevisionValue(value);
}

function normalizeCause(cause, fallbackCode, fallbackMessage) {
  const candidate = cause && typeof cause === "object" && !Array.isArray(cause) ? cause : {};
  const code = SAFE_CODE.test(String(candidate.code || ""))
    ? String(candidate.code)
    : fallbackCode;
  const normalized = {
    code,
    message: redactPublicText(candidate.message, fallbackMessage),
    retryable: candidate.retryable === true,
  };
  if (SAFE_DEPENDENCY.test(String(candidate.dependency || ""))) {
    normalized.dependency = String(candidate.dependency);
  }
  return normalized;
}

function defaultFeatureCause(status) {
  const code = status === "degraded" ? "FEATURE_DEGRADED" : "FEATURE_NOT_READY";
  return normalizeCause(null, code, PUBLIC_CAUSE_MESSAGES[code]);
}

function normalizeProbeResult(rawResult) {
  try {
    assertPlainObject(rawResult, "probe result");
  } catch (_error) {
    throw new ProbeControlError("PROBE_RESULT_INVALID");
  }
  if (!PROBE_STATUSES.has(rawResult.status)) {
    throw new ProbeControlError("PROBE_RESULT_INVALID");
  }
  if (rawResult.causes !== undefined && !Array.isArray(rawResult.causes)) {
    throw new ProbeControlError("PROBE_RESULT_INVALID");
  }
  if (rawResult.status === "ready" && rawResult.causes && rawResult.causes.length > 0) {
    throw new ProbeControlError("PROBE_RESULT_INVALID");
  }

  const fallback = defaultFeatureCause(rawResult.status);
  let causes = (rawResult.causes || [])
    .slice(0, MAX_CAUSES)
    .map((cause) => normalizeCause(cause, fallback.code, fallback.message));
  if (rawResult.status !== "ready" && causes.length === 0) causes = [fallback];

  return {
    status: rawResult.status,
    revision: sanitizeRevision(rawResult.revision),
    causes,
  };
}

function controlFailure(code) {
  return {
    status: "not_ready",
    revision: null,
    causes: [{
      code,
      message: PUBLIC_CAUSE_MESSAGES[code] || PUBLIC_CAUSE_MESSAGES.PROBE_FAILED,
      retryable: code === "PROBE_TIMEOUT" || code === "PROBE_ABORTED" || code === "PROBE_FAILED",
    }],
  };
}

function normalizeFeaturePolicy(featurePolicy) {
  if (featurePolicy === undefined) featurePolicy = {};
  assertPlainObject(featurePolicy, "featurePolicy");

  for (const name of Object.keys(featurePolicy)) {
    if (!FEATURE_NAMES.includes(name)) throw new TypeError(`Unknown readiness feature: ${name}`);
  }

  const policy = {};
  for (const name of FEATURE_NAMES) {
    const value = featurePolicy[name] === undefined
      ? (name === "nlp_generation" || name === "artifact_journal" ? "disabled" : "optional")
      : featurePolicy[name];
    if (!FEATURE_POLICIES.includes(value)) {
      throw new TypeError(`${name} readiness policy must be required, optional, or disabled`);
    }
    policy[name] = value;
  }
  return Object.freeze(policy);
}

function normalizeProbeConfig(probes, defaultTimeoutMs) {
  if (probes === undefined) probes = {};
  assertPlainObject(probes, "probes");
  for (const name of Object.keys(probes)) {
    if (!FEATURE_NAMES.includes(name)) throw new TypeError(`Unknown readiness probe: ${name}`);
  }

  const normalized = {};
  for (const name of FEATURE_NAMES) {
    const configured = probes[name];
    if (configured === undefined || configured === null) continue;
    if (typeof configured === "function") {
      normalized[name] = Object.freeze({ probe: configured, timeoutMs: defaultTimeoutMs });
      continue;
    }
    assertPlainObject(configured, `probes.${name}`);
    if (typeof configured.probe !== "function") {
      throw new TypeError(`probes.${name}.probe must be a function`);
    }
    normalized[name] = Object.freeze({
      probe: configured.probe,
      timeoutMs: normalizeTimeout(
        configured.timeoutMs === undefined ? defaultTimeoutMs : configured.timeoutMs,
        `probes.${name}.timeoutMs`,
      ),
    });
  }
  return Object.freeze(normalized);
}

class ReadinessRegistry {
  constructor({
    featurePolicy = {},
    probes = {},
    defaultTimeoutMs = DEFAULT_PROBE_TIMEOUT_MS,
    revision = null,
    now = () => Date.now(),
  } = {}) {
    if (typeof now !== "function") throw new TypeError("now must be a function");
    this._now = now;
    this._defaultTimeoutMs = normalizeTimeout(defaultTimeoutMs, "defaultTimeoutMs");
    this._featurePolicy = normalizeFeaturePolicy(featurePolicy);
    this._probes = normalizeProbeConfig(probes, this._defaultTimeoutMs);
    this._revision = sanitizeRevision(revision);
  }

  getLiveness() {
    const checkedAtMs = readClock(this._now);
    return {
      schema: READINESS_SCHEMA,
      kind: "liveness",
      status: "live",
      live: true,
      checked_at: new Date(checkedAtMs).toISOString(),
      revision: this._revision,
    };
  }

  async _runProbe(feature, policy, parentSignal) {
    if (policy === "disabled") {
      return {
        policy,
        status: "disabled",
        ready: true,
        blocking: false,
        latency_ms: 0,
        revision: null,
        causes: [],
      };
    }

    const configured = this._probes[feature];
    if (!configured) {
      return {
        policy,
        ...controlFailure("PROBE_NOT_CONFIGURED"),
        ready: false,
        blocking: policy === "required",
        latency_ms: 0,
      };
    }

    const startedAt = readClock(this._now);
    const controller = new AbortController();
    let timeout = null;
    let removeParentAbort = () => {};

    try {
      if (parentSignal && parentSignal.aborted) {
        throw new ProbeControlError("PROBE_ABORTED");
      }

      const timeoutPromise = new Promise((_resolve, reject) => {
        timeout = setTimeout(() => {
          const error = new ProbeControlError("PROBE_TIMEOUT");
          reject(error);
          controller.abort(error);
        }, configured.timeoutMs);
      });
      const abortPromise = new Promise((_resolve, reject) => {
        if (!parentSignal) return;
        const onAbort = () => {
          const error = new ProbeControlError("PROBE_ABORTED");
          reject(error);
          controller.abort(error);
        };
        parentSignal.addEventListener("abort", onAbort, { once: true });
        removeParentAbort = () => parentSignal.removeEventListener("abort", onAbort);
      });
      const probePromise = Promise.resolve().then(() => configured.probe({
        feature,
        signal: controller.signal,
        timeoutMs: configured.timeoutMs,
      }));

      const result = normalizeProbeResult(await Promise.race([
        probePromise,
        timeoutPromise,
        abortPromise,
      ]));
      const latencyMs = Math.max(0, Math.round(readClock(this._now) - startedAt));
      return {
        policy,
        ...result,
        ready: result.status === "ready",
        blocking: policy === "required" && result.status !== "ready",
        latency_ms: latencyMs,
      };
    } catch (error) {
      const code = error instanceof ProbeControlError ? error.code : "PROBE_FAILED";
      const failure = controlFailure(code);
      return {
        policy,
        ...failure,
        ready: false,
        blocking: policy === "required",
        latency_ms: Math.max(0, Math.round(readClock(this._now) - startedAt)),
      };
    } finally {
      if (timeout) clearTimeout(timeout);
      removeParentAbort();
    }
  }

  async getReadiness({ signal } = {}) {
    if (signal !== undefined &&
        (!signal || typeof signal.aborted !== "boolean" || typeof signal.addEventListener !== "function")) {
      throw new TypeError("signal must be an AbortSignal");
    }

    const startedAt = readClock(this._now);
    const entries = await Promise.all(FEATURE_NAMES.map(async (feature) => [
      feature,
      await this._runProbe(feature, this._featurePolicy[feature], signal),
    ]));
    const features = Object.fromEntries(entries);
    const causes = [];
    for (const feature of FEATURE_NAMES) {
      const result = features[feature];
      for (const cause of result.causes) {
        causes.push({
          feature,
          policy: result.policy,
          blocking: result.blocking,
          ...cause,
        });
      }
    }

    const blocked = FEATURE_NAMES.some((feature) => features[feature].blocking);
    const impaired = FEATURE_NAMES.some((feature) =>
      features[feature].status !== "ready" && features[feature].status !== "disabled");
    const status = blocked ? "not_ready" : impaired ? "degraded" : "ready";

    return {
      schema: READINESS_SCHEMA,
      kind: "readiness",
      status,
      ready: !blocked,
      checked_at: new Date(startedAt).toISOString(),
      duration_ms: Math.max(0, Math.round(readClock(this._now) - startedAt)),
      revision: this._revision,
      features,
      causes,
    };
  }
}

function createReadinessRegistry(options) {
  return new ReadinessRegistry(options);
}

function readinessHttpStatus(report) {
  return report && report.ready === true ? 200 : 503;
}

module.exports = {
  DEFAULT_PROBE_TIMEOUT_MS,
  FEATURE_NAMES,
  FEATURE_POLICIES,
  MAX_PROBE_TIMEOUT_MS,
  READINESS_SCHEMA,
  ReadinessRegistry,
  createReadinessRegistry,
  readinessHttpStatus,
};
