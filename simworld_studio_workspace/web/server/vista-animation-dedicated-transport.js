"use strict";

const TRANSPORT_METHODS = Object.freeze([
  "captureAnimationEvidence",
  "invokeAnimationContentApi",
  "probeAnimationContentApi",
  "sampleAnimationEngineTime",
]);

const COMMAND_PROFILES = Object.freeze({
  captureAnimationEvidence: Object.freeze({
    commandType: "vista_animation_evidence_capture",
    maxRequestBytes: 1_048_576,
    maxResultBytes: 262_144,
    maxOuterBytes: 524_288,
    defaultTimeoutMs: 10_000,
    maxTimeoutMs: 15_000,
    defaultQueueDeadlineMs: 15_000,
    maxQueueDeadlineMs: 30_000,
    maxReadAttempts: 1,
  }),
  invokeAnimationContentApi: Object.freeze({
    commandType: "vista_animation_content_api",
    maxRequestBytes: 1_048_576,
    maxResultBytes: 1_048_576,
    maxOuterBytes: 2_097_152,
    defaultTimeoutMs: 30_000,
    maxTimeoutMs: 60_000,
    defaultQueueDeadlineMs: 60_000,
    maxQueueDeadlineMs: 120_000,
    maxReadAttempts: 2,
  }),
  probeAnimationContentApi: Object.freeze({
    commandType: "vista_animation_capabilities",
    maxRequestBytes: 131_072,
    maxResultBytes: 131_072,
    maxOuterBytes: 262_144,
    defaultTimeoutMs: 3_000,
    maxTimeoutMs: 10_000,
    defaultQueueDeadlineMs: 3_000,
    maxQueueDeadlineMs: 10_000,
    maxReadAttempts: 1,
  }),
  sampleAnimationEngineTime: Object.freeze({
    commandType: "vista_animation_engine_time",
    maxRequestBytes: 131_072,
    maxResultBytes: 262_144,
    maxOuterBytes: 524_288,
    defaultTimeoutMs: 3_000,
    maxTimeoutMs: 10_000,
    defaultQueueDeadlineMs: 5_000,
    maxQueueDeadlineMs: 15_000,
    maxReadAttempts: 1,
  }),
});

const IDENTITY_KEYS = Object.freeze([
  "leaseId",
  "mcpPort",
  "ownerId",
  "planId",
  "sceneRevision",
  "sessionId",
  "slotId",
]);
const OPAQUE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,159}$/;
const PLAN_ID_RE = /^vsp-[a-f0-9]{24}$/;

class VistaAnimationDedicatedTransportError extends Error {
  constructor(code, message, { status = 500, retryable = false } = {}) {
    super(message);
    this.name = "VistaAnimationDedicatedTransportError";
    this.code = code;
    this.status = status;
    this.retryable = retryable;
  }
}

function fail(code, message, options) {
  throw new VistaAnimationDedicatedTransportError(code, message, options);
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function hasExactKeys(value, expected) {
  if (!isPlainObject(value)) return false;
  const actual = Object.keys(value).sort();
  return actual.length === expected.length
    && actual.every((key, index) => key === expected[index]);
}

function normalizeIdentity(identity) {
  if (!hasExactKeys(identity, IDENTITY_KEYS)) {
    fail("ANIMATION_UE_TRANSPORT_IDENTITY_INVALID", "Animation transport identity is invalid", { status: 409 });
  }
  for (const key of ["ownerId", "sessionId", "leaseId", "sceneRevision"]) {
    if (typeof identity[key] !== "string" || !OPAQUE_ID_RE.test(identity[key])) {
      fail("ANIMATION_UE_TRANSPORT_IDENTITY_INVALID", "Animation transport identity is invalid", { status: 409 });
    }
  }
  if (typeof identity.planId !== "string" || !PLAN_ID_RE.test(identity.planId)
      || !Number.isSafeInteger(identity.slotId) || identity.slotId < 0 || identity.slotId > 1023
      || !Number.isSafeInteger(identity.mcpPort) || identity.mcpPort < 1 || identity.mcpPort > 65535) {
    fail("ANIMATION_UE_TRANSPORT_IDENTITY_INVALID", "Animation transport identity is invalid", { status: 409 });
  }
  return Object.freeze({ ...identity });
}

function validateRequestJson(requestJson, profile) {
  if (typeof requestJson !== "string") {
    fail("ANIMATION_UE_TRANSPORT_REQUEST_INVALID", "Animation transport requires JSON text", { status: 400 });
  }
  const size = Buffer.byteLength(requestJson, "utf8");
  if (size < 2 || size > profile.maxRequestBytes) {
    fail("ANIMATION_UE_TRANSPORT_REQUEST_INVALID", "Animation transport request exceeds its fixed bound", { status: 413 });
  }
  try {
    if (!isPlainObject(JSON.parse(requestJson))) throw new Error("request must be an object");
  } catch {
    fail("ANIMATION_UE_TRANSPORT_REQUEST_INVALID", "Animation transport requires a JSON object", { status: 400 });
  }
  return requestJson;
}

function normalizeSignal(signal) {
  if (signal === undefined) return undefined;
  if (!signal || typeof signal.aborted !== "boolean"
      || typeof signal.addEventListener !== "function"
      || typeof signal.removeEventListener !== "function") {
    fail("ANIMATION_UE_TRANSPORT_OPTIONS_INVALID", "Animation transport signal is invalid", { status: 500 });
  }
  return signal;
}

function boundedInteger(value, fallback, minimum, maximum, label) {
  const normalized = value === undefined ? fallback : value;
  if (!Number.isSafeInteger(normalized) || normalized < minimum || normalized > maximum) {
    fail("ANIMATION_UE_TRANSPORT_OPTIONS_INVALID", `${label} is outside the fixed transport bound`, { status: 500 });
  }
  return normalized;
}

function normalizeBrokerOptions(method, rawOptions, profile) {
  if (!isPlainObject(rawOptions)) {
    fail("ANIMATION_UE_TRANSPORT_OPTIONS_INVALID", "Animation transport options are invalid", { status: 500 });
  }
  if (typeof rawOptions.mutation !== "boolean") {
    fail("ANIMATION_UE_TRANSPORT_OPTIONS_INVALID", "Animation transport mutation policy is missing", { status: 500 });
  }
  if (method !== "invokeAnimationContentApi" && rawOptions.mutation !== false) {
    fail("ANIMATION_UE_TRANSPORT_OPTIONS_INVALID", "Read-only animation transport cannot mutate", { status: 500 });
  }

  const maxAttempts = boundedInteger(
    rawOptions.maxAttempts,
    1,
    1,
    rawOptions.mutation ? 1 : profile.maxReadAttempts,
    "Animation transport maxAttempts",
  );
  if (rawOptions.mutation && maxAttempts !== 1) {
    fail("ANIMATION_UE_TRANSPORT_OPTIONS_INVALID", "Animation mutation transport cannot retry", { status: 500 });
  }
  const timeoutMs = boundedInteger(
    rawOptions.timeoutMs,
    profile.defaultTimeoutMs,
    1,
    profile.maxTimeoutMs,
    "Animation transport timeoutMs",
  );
  const queueDeadlineMs = boundedInteger(
    rawOptions.queueDeadlineMs,
    profile.defaultQueueDeadlineMs,
    1,
    profile.maxQueueDeadlineMs,
    "Animation transport queueDeadlineMs",
  );
  const signal = normalizeSignal(rawOptions.signal);
  return Object.freeze({
    maxAttempts,
    timeoutMs,
    queueDeadlineMs,
    ...(signal === undefined ? {} : { signal }),
  });
}

function parseOuterResponse(raw, profile) {
  if (!hasExactKeys(raw, ["result", "status"])) {
    fail("ANIMATION_UE_TRANSPORT_PROTOCOL_INVALID", "Animation UE response has an invalid outer shape", { status: 502 });
  }
  let outerSize;
  try {
    outerSize = Buffer.byteLength(JSON.stringify(raw), "utf8");
  } catch {
    fail("ANIMATION_UE_TRANSPORT_PROTOCOL_INVALID", "Animation UE response is not bounded JSON", { status: 502 });
  }
  if (outerSize > profile.maxOuterBytes || raw.status !== "success" || typeof raw.result !== "string") {
    fail("ANIMATION_UE_TRANSPORT_PROTOCOL_INVALID", "Animation UE response failed the fixed envelope contract", { status: 502 });
  }
  const resultSize = Buffer.byteLength(raw.result, "utf8");
  if (resultSize < 2 || resultSize > profile.maxResultBytes) {
    fail("ANIMATION_UE_TRANSPORT_PROTOCOL_INVALID", "Animation UE JSON result exceeds its fixed bound", { status: 502 });
  }
  try {
    if (!isPlainObject(JSON.parse(raw.result))) throw new Error("result must be an object");
  } catch {
    fail("ANIMATION_UE_TRANSPORT_PROTOCOL_INVALID", "Animation UE result is not a JSON object", { status: 502 });
  }
  return raw.result;
}

function createTransport(resolveBoundBroker) {
  const entries = TRANSPORT_METHODS.map((method) => {
    const profile = COMMAND_PROFILES[method];
    return [method, async (requestJson, rawOptions) => {
      const request = validateRequestJson(requestJson, profile);
      const options = normalizeBrokerOptions(method, rawOptions, profile);
      const broker = await resolveBoundBroker();
      if (broker === null) {
        fail("ANIMATION_UE_TRANSPORT_UNAVAILABLE", "Dedicated animation UE slot is unavailable", {
          status: 503,
          retryable: !rawOptions.mutation,
        });
      }
      let response;
      try {
        response = await broker.send(
          profile.commandType,
          Object.freeze({ request_json: request }),
          options,
        );
      } catch (error) {
        if (error instanceof VistaAnimationDedicatedTransportError) throw error;
        fail("ANIMATION_UE_TRANSPORT_UNAVAILABLE", "Dedicated animation UE command is unavailable", {
          status: 503,
          retryable: !rawOptions.mutation,
        });
      }
      return parseOuterResponse(response, profile);
    }];
  });
  return Object.freeze(Object.fromEntries(entries));
}

function createVistaAnimationDedicatedTransportResolver({ resolveUeBroker } = {}) {
  if (typeof resolveUeBroker !== "function") {
    throw new TypeError("resolveUeBroker is required");
  }
  return async function resolveVistaAnimationDedicatedTransport(rawIdentity) {
    const identity = normalizeIdentity(rawIdentity);
    const resolveBoundBroker = async () => {
      let broker;
      try {
        broker = await resolveUeBroker(identity);
      } catch (error) {
        if (error instanceof VistaAnimationDedicatedTransportError) throw error;
        fail("ANIMATION_UE_TRANSPORT_UNAVAILABLE", "Dedicated animation UE slot is unavailable", {
          status: 503,
          retryable: true,
        });
      }
      if (broker === null || broker === undefined) return null;
      if ((typeof broker !== "object" && typeof broker !== "function")
          || typeof broker.send !== "function"
          || broker.port !== identity.mcpPort) {
        fail("ANIMATION_UE_TRANSPORT_BINDING_MISMATCH", "Animation UE broker does not match the active slot", {
          status: 409,
        });
      }
      return broker;
    };
    if (await resolveBoundBroker() === null) return null;
    return createTransport(resolveBoundBroker);
  };
}

module.exports = {
  COMMAND_PROFILES,
  TRANSPORT_METHODS,
  VistaAnimationDedicatedTransportError,
  createVistaAnimationDedicatedTransportResolver,
};
