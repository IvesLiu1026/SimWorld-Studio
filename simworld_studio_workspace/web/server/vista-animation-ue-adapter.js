"use strict";

const crypto = require("node:crypto");

const {
  ACTION_DEFINITIONS,
  ANIMATION_PREFLIGHT_REQUEST_SCHEMA,
  ANIMATION_PREFLIGHT_RESPONSE_SCHEMA,
  canonicalize,
  cloneJson,
  deepFreeze,
  digest,
  validateActionParameters,
  validateAnimationPreflightResponse,
  validateContentProfile,
} = require("./vista-animation-contract");
const {
  RELEASE_REQUEST_SCHEMA,
  RELEASE_RESPONSE_SCHEMA,
  RESTORE_REQUEST_SCHEMA,
  RESTORE_RESPONSE_SCHEMA,
  SNAPSHOT_REQUEST_SCHEMA,
  SNAPSHOT_RESPONSE_SCHEMA,
  START_REQUEST_SCHEMA,
  START_RESPONSE_SCHEMA,
  STOP_REQUEST_SCHEMA,
  STOP_RESPONSE_SCHEMA,
  WAIT_REQUEST_SCHEMA,
  WAIT_RESPONSE_SCHEMA,
} = require("./vista-animation-runtime");

const ANIMATION_UE_REQUEST_SCHEMA = "vista-animation-ue-request/v1";
const ANIMATION_UE_RESPONSE_SCHEMA = "vista-animation-ue-response/v1";
const ANIMATION_UE_MARKER_SCHEMA = "vista-animation-ue-nonce-marker/v1";
const ANIMATION_UE_CONTENT_PROOF_SCHEMA = "vista-animation-ue-content-proof/v1";
const ANIMATION_UE_OPERATION_CONTRACT_SCHEMA = "vista-animation-ue-operation-contract/v1";

const SAFE_ID_RE = /^[a-z][a-z0-9_-]{0,119}$/;
const OPAQUE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,159}$/;
const PREFLIGHT_ID_RE = /^vap-[a-f0-9]{24}$/;
const EVENT_ID_RE = /^beat-[0-9]{4}(?:-[a-z0-9_-]+)?$/;
const SHA256_RE = /^[a-f0-9]{64}$/;
const NONCE_RE = /^[a-f0-9]{32}$/;
const INVOCATION_ID_RE = /^vau-[a-z_]+-[a-f0-9]{16}-[a-f0-9]{12}$/;
const MAX_ENGINE_TIME_SEC = 315_360_000;
const MAX_RESPONSE_BYTES = 1_048_576;
const STOP_REASONS = new Set([
  "timeout",
  "cancel",
  "cleanup_failed",
  "cleanup_cancelled",
  "cleanup_timed_out",
]);

const RAW_OPERATION_CONTRACTS = Object.freeze({
  preflightAnimation: {
    operation_id: "vista.animation.preflight.v1",
    request_schema: ANIMATION_PREFLIGHT_REQUEST_SCHEMA,
    response_schema: ANIMATION_PREFLIGHT_RESPONSE_SCHEMA,
    mutation: false,
    max_attempts: 2,
    timeout_ms: 15_000,
  },
  snapshotAnimationState: {
    operation_id: "vista.animation.snapshot.v1",
    request_schema: SNAPSHOT_REQUEST_SCHEMA,
    response_schema: SNAPSHOT_RESPONSE_SCHEMA,
    mutation: false,
    max_attempts: 2,
    timeout_ms: 10_000,
  },
  startAnimationAction: {
    operation_id: "vista.animation.start.v1",
    request_schema: START_REQUEST_SCHEMA,
    response_schema: START_RESPONSE_SCHEMA,
    mutation: true,
    max_attempts: 1,
    timeout_ms: 10_000,
  },
  waitAnimationAction: {
    operation_id: "vista.animation.wait.v1",
    request_schema: WAIT_REQUEST_SCHEMA,
    response_schema: WAIT_RESPONSE_SCHEMA,
    mutation: false,
    max_attempts: 2,
    timeout_ms: 60_000,
  },
  stopAnimationAction: {
    operation_id: "vista.animation.stop.v1",
    request_schema: STOP_REQUEST_SCHEMA,
    response_schema: STOP_RESPONSE_SCHEMA,
    mutation: true,
    max_attempts: 1,
    timeout_ms: 10_000,
  },
  releaseAnimationAction: {
    operation_id: "vista.animation.release.v1",
    request_schema: RELEASE_REQUEST_SCHEMA,
    response_schema: RELEASE_RESPONSE_SCHEMA,
    mutation: true,
    max_attempts: 1,
    timeout_ms: 10_000,
  },
  restoreAnimationState: {
    operation_id: "vista.animation.restore.v1",
    request_schema: RESTORE_REQUEST_SCHEMA,
    response_schema: RESTORE_RESPONSE_SCHEMA,
    mutation: true,
    max_attempts: 1,
    timeout_ms: 15_000,
  },
});

const ANIMATION_UE_OPERATION_ALLOWLIST = deepFreeze(Object.fromEntries(
  Object.entries(RAW_OPERATION_CONTRACTS).map(([method, contract]) => {
    const identity = {
      schema: ANIMATION_UE_OPERATION_CONTRACT_SCHEMA,
      method,
      ...contract,
    };
    return [method, { ...identity, operation_fingerprint: digest(identity) }];
  }),
));

class VistaAnimationUeAdapterError extends Error {
  constructor(code, message, { status = 400, retryable = false, details = {} } = {}) {
    super(message);
    this.name = "VistaAnimationUeAdapterError";
    this.code = code;
    this.status = status;
    this.retryable = retryable;
    this.details = sanitizeDetails(details);
  }
}

function defaultNonceFactory() {
  return crypto.randomBytes(16).toString("hex");
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function sanitizeDetails(value, depth = 0) {
  if (depth > 4) return "[truncated]";
  if (value === null || typeof value === "boolean" || typeof value === "number") return value;
  if (typeof value === "string") return value.slice(0, 300);
  if (Array.isArray(value)) return value.slice(0, 32).map((entry) => sanitizeDetails(entry, depth + 1));
  if (!isPlainObject(value)) return undefined;
  const output = {};
  for (const key of Object.keys(value).sort().slice(0, 32)) {
    if (/token|secret|password|credential|authorization|cookie|payload|response/i.test(key)) continue;
    const normalized = sanitizeDetails(value[key], depth + 1);
    if (normalized !== undefined) output[key] = normalized;
  }
  return output;
}

function fail(code, message, options) {
  throw new VistaAnimationUeAdapterError(code, message, options);
}

function exactKeys(value, expected, pointer, code = "ANIMATION_UE_REQUEST_INVALID") {
  if (!isPlainObject(value)) {
    fail(code, `${pointer} must be an object`, { status: code.includes("PROTOCOL_INVALID") ? 502 : 400 });
  }
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  if (actual.length !== wanted.length || actual.some((key, index) => key !== wanted[index])) {
    fail(code, `${pointer} has an invalid shape`, {
      status: code.includes("PROTOCOL_INVALID") ? 502 : 400,
      details: {
        pointer,
        unknown: actual.filter((key) => !wanted.includes(key)),
        missing: wanted.filter((key) => !actual.includes(key)),
      },
    });
  }
}

function requireString(value, pointer, {
  pattern = null,
  max = 512,
  code = "ANIMATION_UE_REQUEST_INVALID",
} = {}) {
  if (typeof value !== "string" || value.length === 0 || value.length > max || (pattern && !pattern.test(value))) {
    fail(code, `${pointer} is invalid`, {
      status: code.includes("PROTOCOL_INVALID") ? 502 : 400,
      details: { pointer },
    });
  }
  return value;
}

function requireFinite(value, pointer, code) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > MAX_ENGINE_TIME_SEC) {
    fail(code, `${pointer} is invalid`, { status: 502, details: { pointer } });
  }
  return value;
}

function requireStringArray(value, pointer, { pattern = SAFE_ID_RE, allowEmpty = true } = {}) {
  if (!Array.isArray(value) || value.length > 128 || (!allowEmpty && value.length === 0)) {
    fail("ANIMATION_UE_REQUEST_INVALID", `${pointer} is invalid`, { details: { pointer } });
  }
  const normalized = value.map((entry, index) => requireString(entry, `${pointer}[${index}]`, {
    pattern,
    max: 160,
  }));
  if (new Set(normalized).size !== normalized.length || normalized.some((entry, index) => index > 0 && entry <= normalized[index - 1])) {
    fail("ANIMATION_UE_REQUEST_INVALID", `${pointer} must be unique and sorted`, { details: { pointer } });
  }
  return normalized;
}

function requestOptions(options) {
  if (options === undefined) return { signal: undefined };
  if (!isPlainObject(options)) fail("ANIMATION_UE_REQUEST_INVALID", "adapter options must be an object");
  const keys = Object.keys(options);
  if (keys.some((key) => key !== "signal")) {
    fail("ANIMATION_UE_REQUEST_INVALID", "adapter options contain an unsupported field");
  }
  const signal = options.signal;
  if (signal !== undefined && (!signal || typeof signal !== "object" || typeof signal.aborted !== "boolean")) {
    fail("ANIMATION_UE_REQUEST_INVALID", "adapter signal is invalid");
  }
  return { signal };
}

function throwIfAborted(signal) {
  if (!signal || !signal.aborted) return;
  fail("ANIMATION_UE_OPERATION_ABORTED", "Animation UE operation was aborted", { status: 499 });
}

function validatePreflightRequest(request, profile) {
  exactKeys(request, [
    "schema",
    "scene_revision",
    "profile_id",
    "profile_revision",
    "content_revision",
    "content_digest",
    "pawn_class_path",
    "skeleton_path",
    "requested_actions",
    "actor_binding_ids",
    "target_binding_ids",
  ], "preflight request");
  if (
    request.schema !== ANIMATION_PREFLIGHT_REQUEST_SCHEMA
    || request.profile_id !== profile.profile_id
    || request.profile_revision !== profile.revision
    || request.content_revision !== profile.content_revision
    || request.content_digest !== profile.content_digest
    || request.pawn_class_path !== profile.pawn_class_path
    || request.skeleton_path !== profile.skeleton_path
  ) {
    fail("ANIMATION_UE_CONTENT_PROFILE_MISMATCH", "Preflight request does not match the server-owned content profile", { status: 409 });
  }
  requireString(request.scene_revision, "preflight request.scene_revision", { pattern: OPAQUE_ID_RE, max: 160 });
  const requestedActions = requireStringArray(request.requested_actions, "preflight request.requested_actions", { allowEmpty: false });
  for (const action of requestedActions) {
    if (!ACTION_DEFINITIONS[action]) fail("ANIMATION_UE_OPERATION_FORBIDDEN", `Action '${action}' is not in the fixed allowlist`, { status: 403 });
  }
  const actorBindingIds = requireStringArray(request.actor_binding_ids, "preflight request.actor_binding_ids", { allowEmpty: false });
  const targetBindingIds = requireStringArray(request.target_binding_ids, "preflight request.target_binding_ids");
  return deepFreeze({
    schema: ANIMATION_PREFLIGHT_REQUEST_SCHEMA,
    scene_revision: request.scene_revision,
    profile_id: profile.profile_id,
    profile_revision: profile.revision,
    content_revision: profile.content_revision,
    content_digest: profile.content_digest,
    requested_actions: requestedActions,
    actor_binding_ids: actorBindingIds,
    target_binding_ids: targetBindingIds,
  });
}

function validateLifecycleIdentity(request, schema, expectedKeys, pointer) {
  exactKeys(request, expectedKeys, pointer);
  if (request.schema !== schema) fail("ANIMATION_UE_REQUEST_INVALID", `${pointer}.schema is invalid`);
  return {
    preflight_id: requireString(request.preflight_id, `${pointer}.preflight_id`, { pattern: PREFLIGHT_ID_RE, max: 28 }),
    run_id: requireString(request.run_id, `${pointer}.run_id`, { pattern: OPAQUE_ID_RE, max: 160 }),
    event_id: requireString(request.event_id, `${pointer}.event_id`, { pattern: EVENT_ID_RE, max: 120 }),
  };
}

function validateSnapshotRequest(request) {
  const identity = validateLifecycleIdentity(request, SNAPSHOT_REQUEST_SCHEMA, [
    "schema",
    "preflight_id",
    "run_id",
    "event_id",
    "action",
    "actor_binding_id",
    "target_binding_id",
  ], "snapshot request");
  const action = requireString(request.action, "snapshot request.action", { pattern: SAFE_ID_RE, max: 120 });
  if (!ACTION_DEFINITIONS[action]) fail("ANIMATION_UE_OPERATION_FORBIDDEN", `Action '${action}' is not in the fixed allowlist`, { status: 403 });
  const actorBindingId = requireString(request.actor_binding_id, "snapshot request.actor_binding_id", { pattern: SAFE_ID_RE, max: 120 });
  const targetBindingId = request.target_binding_id === null
    ? null
    : requireString(request.target_binding_id, "snapshot request.target_binding_id", { pattern: SAFE_ID_RE, max: 120 });
  const policy = ACTION_DEFINITIONS[action].target_policy;
  if ((policy === "required" && targetBindingId === null) || (policy === "forbidden" && targetBindingId !== null)) {
    fail("ANIMATION_UE_REQUEST_INVALID", `snapshot request target does not match '${action}' policy`);
  }
  return deepFreeze({
    schema: SNAPSHOT_REQUEST_SCHEMA,
    ...identity,
    action,
    actor_binding_id: actorBindingId,
    target_binding_id: targetBindingId,
  });
}

function validateStartRequest(request) {
  const identity = validateLifecycleIdentity(request, START_REQUEST_SCHEMA, [
    "schema",
    "preflight_id",
    "run_id",
    "event_id",
    "bridge_action_id",
    "actor_binding_id",
    "target_binding_id",
    "parameters",
  ], "start request");
  const actionEntry = Object.entries(ACTION_DEFINITIONS)
    .find(([, definition]) => definition.bridge_action_id === request.bridge_action_id);
  if (!actionEntry) fail("ANIMATION_UE_OPERATION_FORBIDDEN", "start request bridge action is not in the fixed allowlist", { status: 403 });
  const [action] = actionEntry;
  const actorBindingId = requireString(request.actor_binding_id, "start request.actor_binding_id", { pattern: SAFE_ID_RE, max: 120 });
  const targetBindingId = request.target_binding_id === null
    ? null
    : requireString(request.target_binding_id, "start request.target_binding_id", { pattern: SAFE_ID_RE, max: 120 });
  let parameters;
  try {
    parameters = validateActionParameters(action, request.parameters);
  } catch {
    fail("ANIMATION_UE_REQUEST_INVALID", "start request parameters are invalid");
  }
  if (canonicalize(parameters) !== canonicalize(request.parameters)) {
    fail("ANIMATION_UE_REQUEST_INVALID", "start request parameters must include the normalized fixed defaults");
  }
  return deepFreeze({
    schema: START_REQUEST_SCHEMA,
    ...identity,
    action,
    bridge_action_id: ACTION_DEFINITIONS[action].bridge_action_id,
    actor_binding_id: actorBindingId,
    target_binding_id: targetBindingId,
    parameters: cloneJson(parameters),
  });
}

function validateHandleRequest(request, schema, pointer, extraKeys = []) {
  const identity = validateLifecycleIdentity(request, schema, [
    "schema",
    "preflight_id",
    "run_id",
    "event_id",
    "action_handle",
    ...extraKeys,
  ], pointer);
  return {
    ...identity,
    action_handle: requireString(request.action_handle, `${pointer}.action_handle`, { pattern: OPAQUE_ID_RE, max: 160 }),
  };
}

function validateWaitRequest(request) {
  return deepFreeze({
    schema: WAIT_REQUEST_SCHEMA,
    ...validateHandleRequest(request, WAIT_REQUEST_SCHEMA, "wait request"),
  });
}

function validateStopRequest(request) {
  const normalized = validateHandleRequest(request, STOP_REQUEST_SCHEMA, "stop request", ["reason"]);
  if (!STOP_REASONS.has(request.reason)) fail("ANIMATION_UE_REQUEST_INVALID", "stop request.reason is invalid");
  return deepFreeze({ schema: STOP_REQUEST_SCHEMA, ...normalized, reason: request.reason });
}

function validateReleaseRequest(request) {
  return deepFreeze({
    schema: RELEASE_REQUEST_SCHEMA,
    ...validateHandleRequest(request, RELEASE_REQUEST_SCHEMA, "release request"),
  });
}

function validateRestoreRequest(request) {
  const identity = validateLifecycleIdentity(request, RESTORE_REQUEST_SCHEMA, [
    "schema",
    "preflight_id",
    "run_id",
    "event_id",
    "snapshot_id",
    "state_digest",
  ], "restore request");
  return deepFreeze({
    schema: RESTORE_REQUEST_SCHEMA,
    ...identity,
    snapshot_id: requireString(request.snapshot_id, "restore request.snapshot_id", { pattern: OPAQUE_ID_RE, max: 160 }),
    state_digest: requireString(request.state_digest, "restore request.state_digest", { pattern: SHA256_RE, max: 64 }),
  });
}

function validateSnapshotResponse(request, response) {
  const code = "ANIMATION_UE_SNAPSHOT_PROTOCOL_INVALID";
  exactKeys(response, [
    "schema",
    "status",
    "snapshot_id",
    "action",
    "actor_binding_id",
    "target_binding_id",
    "engine_time",
    "state_digest",
  ], "snapshot response", code);
  if (
    response.schema !== SNAPSHOT_RESPONSE_SCHEMA
    || response.status !== "captured"
    || response.action !== request.action
    || response.actor_binding_id !== request.actor_binding_id
    || response.target_binding_id !== request.target_binding_id
  ) fail(code, "Snapshot response does not match the exact request", { status: 502 });
  return deepFreeze({
    schema: SNAPSHOT_RESPONSE_SCHEMA,
    status: "captured",
    snapshot_id: requireString(response.snapshot_id, "snapshot response.snapshot_id", { pattern: OPAQUE_ID_RE, max: 160, code }),
    action: request.action,
    actor_binding_id: request.actor_binding_id,
    target_binding_id: request.target_binding_id,
    engine_time: requireFinite(response.engine_time, "snapshot response.engine_time", code),
    state_digest: requireString(response.state_digest, "snapshot response.state_digest", { pattern: SHA256_RE, max: 64, code }),
  });
}

function validateStartResponse(request, response) {
  const code = "ANIMATION_UE_START_PROTOCOL_INVALID";
  exactKeys(response, [
    "schema",
    "status",
    "action_handle",
    "bridge_action_id",
    "actor_binding_id",
    "target_binding_id",
    "engine_time",
  ], "start response", code);
  if (
    response.schema !== START_RESPONSE_SCHEMA
    || response.status !== "started"
    || response.bridge_action_id !== request.bridge_action_id
    || response.actor_binding_id !== request.actor_binding_id
    || response.target_binding_id !== request.target_binding_id
  ) fail(code, "Start response does not match the exact request", { status: 502 });
  return deepFreeze({
    schema: START_RESPONSE_SCHEMA,
    status: "started",
    action_handle: requireString(response.action_handle, "start response.action_handle", { pattern: OPAQUE_ID_RE, max: 160, code }),
    bridge_action_id: request.bridge_action_id,
    actor_binding_id: request.actor_binding_id,
    target_binding_id: request.target_binding_id,
    engine_time: requireFinite(response.engine_time, "start response.engine_time", code),
  });
}

function validateWaitResponse(request, response, completionSignal) {
  const code = "ANIMATION_UE_WAIT_PROTOCOL_INVALID";
  exactKeys(response, [
    "schema",
    "status",
    "action_handle",
    "completion_signal",
    "engine_time",
    "evidence_ids",
  ], "wait response", code);
  if (
    response.schema !== WAIT_RESPONSE_SCHEMA
    || response.status !== "completed"
    || response.action_handle !== request.action_handle
    || response.completion_signal !== completionSignal
  ) fail(code, "Wait response does not prove the verified completion signal", { status: 502 });
  if (!Array.isArray(response.evidence_ids) || response.evidence_ids.length > 32) {
    fail(code, "Wait response evidence ids are invalid", { status: 502 });
  }
  const evidenceIds = response.evidence_ids.map((entry, index) => requireString(
    entry,
    `wait response.evidence_ids[${index}]`,
    { pattern: OPAQUE_ID_RE, max: 160, code },
  ));
  if (new Set(evidenceIds).size !== evidenceIds.length) fail(code, "Wait response evidence ids must be unique", { status: 502 });
  return deepFreeze({
    schema: WAIT_RESPONSE_SCHEMA,
    status: "completed",
    action_handle: request.action_handle,
    completion_signal: completionSignal,
    engine_time: requireFinite(response.engine_time, "wait response.engine_time", code),
    evidence_ids: evidenceIds.sort(),
  });
}

function validateStopResponse(request, response) {
  const code = "ANIMATION_UE_STOP_PROTOCOL_INVALID";
  exactKeys(response, ["schema", "status", "action_handle", "engine_time"], "stop response", code);
  if (
    response.schema !== STOP_RESPONSE_SCHEMA
    || !new Set(["stopped", "already_stopped"]).has(response.status)
    || response.action_handle !== request.action_handle
  ) fail(code, "Stop response does not match the exact action handle", { status: 502 });
  return deepFreeze({
    schema: STOP_RESPONSE_SCHEMA,
    status: response.status,
    action_handle: request.action_handle,
    engine_time: requireFinite(response.engine_time, "stop response.engine_time", code),
  });
}

function validateReleaseResponse(request, response) {
  const code = "ANIMATION_UE_RELEASE_PROTOCOL_INVALID";
  exactKeys(response, ["schema", "status", "action_handle"], "release response", code);
  if (
    response.schema !== RELEASE_RESPONSE_SCHEMA
    || !new Set(["released", "already_released"]).has(response.status)
    || response.action_handle !== request.action_handle
  ) fail(code, "Release response does not match the exact action handle", { status: 502 });
  return deepFreeze({ schema: RELEASE_RESPONSE_SCHEMA, status: response.status, action_handle: request.action_handle });
}

function validateRestoreResponse(request, response) {
  const code = "ANIMATION_UE_RESTORE_PROTOCOL_INVALID";
  exactKeys(response, ["schema", "status", "snapshot_id", "state_digest", "engine_time"], "restore response", code);
  if (
    response.schema !== RESTORE_RESPONSE_SCHEMA
    || response.status !== "restored"
    || response.snapshot_id !== request.snapshot_id
    || response.state_digest !== request.state_digest
  ) fail(code, "Restore response does not prove the exact snapshot", { status: 502 });
  return deepFreeze({
    schema: RESTORE_RESPONSE_SCHEMA,
    status: "restored",
    snapshot_id: request.snapshot_id,
    state_digest: request.state_digest,
    engine_time: requireFinite(response.engine_time, "restore response.engine_time", code),
  });
}

function createVistaAnimationUeAdapter(options = {}) {
  if (!isPlainObject(options)) fail("ANIMATION_UE_CONFIG_INVALID", "Animation UE adapter options must be an object", { status: 500 });
  const optionKeys = Object.keys(options);
  if (optionKeys.some((key) => !new Set(["transport", "contentProfile", "nonceFactory"]).has(key))) {
    fail("ANIMATION_UE_CONFIG_INVALID", "Animation UE adapter options contain unsupported fields", { status: 500 });
  }
  const { transport } = options;
  if (!transport || typeof transport.invokeAnimationContentApi !== "function") {
    fail("ANIMATION_UE_CONFIG_INVALID", "A fixed animation content API transport must be injected", { status: 500 });
  }
  let profile;
  try {
    profile = validateContentProfile(options.contentProfile);
  } catch {
    fail("ANIMATION_UE_CONFIG_INVALID", "A verified animation content profile must be injected", { status: 500 });
  }
  const nonceFactory = options.nonceFactory === undefined ? defaultNonceFactory : options.nonceFactory;
  if (typeof nonceFactory !== "function") fail("ANIMATION_UE_CONFIG_INVALID", "nonceFactory must be a function", { status: 500 });

  const profileActions = new Map(profile.actions.map((entry) => [entry.action, entry]));
  const bridgeActions = new Map(profile.actions.map((entry) => [entry.bridge_action_id, entry]));
  const contentProof = deepFreeze({
    schema: ANIMATION_UE_CONTENT_PROOF_SCHEMA,
    profile_id: profile.profile_id,
    profile_revision: profile.revision,
    content_revision: profile.content_revision,
    content_digest: profile.content_digest,
    verification_receipt_id: profile.verification.receipt_id,
  });
  const usedNonces = new Set();
  const nonceOrder = [];
  const snapshotsByKey = new Map();
  const snapshotsById = new Map();
  const handles = new Map();
  let activePreflight = null;

  function nextNonce() {
    let nonce;
    try {
      nonce = nonceFactory();
    } catch {
      fail("ANIMATION_UE_NONCE_INVALID", "Animation UE nonce source failed policy", { status: 500 });
    }
    if (typeof nonce !== "string" || !NONCE_RE.test(nonce) || usedNonces.has(nonce)) {
      fail("ANIMATION_UE_NONCE_INVALID", "Animation UE nonce source failed policy", { status: 500 });
    }
    usedNonces.add(nonce);
    nonceOrder.push(nonce);
    if (nonceOrder.length > 4096) usedNonces.delete(nonceOrder.shift());
    return nonce;
  }

  function parseTransportResponse(raw, expected) {
    if (typeof raw !== "string" || Buffer.byteLength(raw, "utf8") > MAX_RESPONSE_BYTES) {
      fail("ANIMATION_UE_PROTOCOL_INVALID", "Animation content API returned an invalid JSON response", {
        status: 502,
        details: { operation_id: expected.operation.operation_id, outcome_unknown: expected.operation.mutation },
      });
    }
    let response;
    try {
      response = JSON.parse(raw);
    } catch {
      fail("ANIMATION_UE_PROTOCOL_INVALID", "Animation content API returned an invalid JSON response", {
        status: 502,
        details: { operation_id: expected.operation.operation_id, outcome_unknown: expected.operation.mutation },
      });
    }
    exactKeys(response, [
      "schema",
      "operation_id",
      "operation_fingerprint",
      "invocation_id",
      "request_digest",
      "nonce_marker",
      "payload",
    ], "animation content API response", "ANIMATION_UE_PROTOCOL_INVALID");
    exactKeys(response.nonce_marker, ["schema", "nonce"], "animation content API response.nonce_marker", "ANIMATION_UE_PROTOCOL_INVALID");
    if (
      response.schema !== ANIMATION_UE_RESPONSE_SCHEMA
      || response.operation_id !== expected.operation.operation_id
      || response.operation_fingerprint !== expected.operation.operation_fingerprint
      || response.invocation_id !== expected.invocationId
      || response.request_digest !== expected.requestDigest
      || response.nonce_marker.schema !== ANIMATION_UE_MARKER_SCHEMA
      || response.nonce_marker.nonce !== expected.nonce
    ) {
      fail("ANIMATION_UE_PROTOCOL_INVALID", "Animation content API response correlation is invalid", {
        status: 502,
        details: { operation_id: expected.operation.operation_id, outcome_unknown: expected.operation.mutation },
      });
    }
    return response.payload;
  }

  async function invoke(method, wireRequest, signal, validatePayload) {
    const operation = ANIMATION_UE_OPERATION_ALLOWLIST[method];
    if (!operation) fail("ANIMATION_UE_OPERATION_FORBIDDEN", "Animation UE operation is not allowlisted", { status: 403 });
    throwIfAborted(signal);
    const nonce = nextNonce();
    const requestDigest = digest({ content_proof: contentProof, request: wireRequest });
    const operationName = operation.operation_id.split(".")[2];
    const invocationId = `vau-${operationName}-${nonce.slice(0, 16)}-${requestDigest.slice(0, 12)}`;
    if (!INVOCATION_ID_RE.test(invocationId)) fail("ANIMATION_UE_CONFIG_INVALID", "Fixed operation identity is invalid", { status: 500 });
    const envelope = deepFreeze({
      schema: ANIMATION_UE_REQUEST_SCHEMA,
      operation_id: operation.operation_id,
      operation_fingerprint: operation.operation_fingerprint,
      invocation_id: invocationId,
      request_digest: requestDigest,
      content_proof: contentProof,
      nonce_marker: { schema: ANIMATION_UE_MARKER_SCHEMA, nonce },
      request: cloneJson(wireRequest),
    });
    const brokerOptions = Object.freeze({
      operationId: operation.operation_id,
      operationFingerprint: operation.operation_fingerprint,
      invocationId,
      requestDigest,
      mutation: operation.mutation,
      maxAttempts: operation.max_attempts,
      timeoutMs: operation.timeout_ms,
      queueDeadlineMs: operation.timeout_ms * 2,
      ...(signal === undefined ? {} : { signal }),
    });
    let raw;
    try {
      raw = await transport.invokeAnimationContentApi(JSON.stringify(envelope), brokerOptions);
    } catch (error) {
      if (error && (error.name === "AbortError" || error.code === "ABORT_ERR" || error.code === "UE_COMMAND_ABORTED")) {
        fail("ANIMATION_UE_OPERATION_ABORTED", "Animation UE operation was aborted", { status: 499 });
      }
      fail(
        operation.mutation ? "ANIMATION_UE_MUTATION_OUTCOME_UNKNOWN" : "ANIMATION_UE_TRANSPORT_UNAVAILABLE",
        operation.mutation
          ? "Animation UE mutation outcome is unknown"
          : "Animation UE content API is unavailable",
        {
          status: operation.mutation ? 502 : 503,
          retryable: !operation.mutation,
          details: { operation_id: operation.operation_id },
        },
      );
    }
    const payload = parseTransportResponse(raw, {
      operation,
      invocationId,
      requestDigest,
      nonce,
    });
    try {
      return validatePayload(payload);
    } catch (error) {
      if (error instanceof VistaAnimationUeAdapterError) {
        if (!operation.mutation) throw error;
        throw new VistaAnimationUeAdapterError(error.code, error.message, {
          status: error.status >= 500 ? error.status : 502,
          retryable: false,
          details: {
            ...error.details,
            operation_id: operation.operation_id,
            outcome_unknown: true,
          },
        });
      }
      fail("ANIMATION_UE_PROTOCOL_INVALID", "Animation content API payload failed exact validation", {
        status: 502,
        details: { operation_id: operation.operation_id, outcome_unknown: operation.mutation },
      });
    }
  }

  function eventKey(request) {
    return `${request.preflight_id}:${request.run_id}:${request.event_id}`;
  }

  function bindActivePreflight(preflightId) {
    if (!activePreflight || !activePreflight.ready) {
      fail("ANIMATION_UE_PREFLIGHT_REQUIRED", "A successful live animation preflight is required", { status: 409 });
    }
    if (activePreflight.preflightId === null) activePreflight.preflightId = preflightId;
    else if (activePreflight.preflightId !== preflightId) {
      fail("ANIMATION_UE_PREFLIGHT_MISMATCH", "Animation request does not match the active preflight", { status: 409 });
    }
  }

  function requireHandle(request, allowedStates) {
    bindActivePreflight(request.preflight_id);
    const handle = handles.get(request.action_handle);
    if (!handle || handle.key !== eventKey(request) || !allowedStates.has(handle.state)) {
      fail("ANIMATION_UE_HANDLE_INVALID", "Animation action handle is unknown or in an invalid state", { status: 409 });
    }
    return handle;
  }

  async function preflightAnimation(request, optionsArg) {
    const { signal } = requestOptions(optionsArg);
    if (snapshotsById.size > 0) {
      fail("ANIMATION_UE_PREFLIGHT_BUSY", "Animation preflight cannot change while an event snapshot is active", { status: 409 });
    }
    const normalized = validatePreflightRequest(request, profile);
    const response = await invoke("preflightAnimation", normalized, signal, (payload) => {
      try {
        return validateAnimationPreflightResponse(request, payload);
      } catch {
        fail("ANIMATION_UE_PREFLIGHT_PROTOCOL_INVALID", "Preflight response failed exact validation", { status: 502 });
      }
    });
    handles.clear();
    activePreflight = response.ready
      ? {
        ready: true,
        preflightId: null,
        requestedActions: new Set(normalized.requested_actions),
        actors: new Map(response.actors.map((entry) => [entry.binding_id, entry])),
        targets: new Map(response.targets.map((entry) => [entry.binding_id, entry])),
        actions: new Map(response.actions.map((entry) => [entry.action, entry])),
      }
      : null;
    return response;
  }

  async function snapshotAnimationState(request, optionsArg) {
    const { signal } = requestOptions(optionsArg);
    const normalized = validateSnapshotRequest(request);
    bindActivePreflight(normalized.preflight_id);
    const action = activePreflight.actions.get(normalized.action);
    const actor = activePreflight.actors.get(normalized.actor_binding_id);
    const target = normalized.target_binding_id === null ? null : activePreflight.targets.get(normalized.target_binding_id);
    if (
      !profileActions.has(normalized.action)
      || !activePreflight.requestedActions.has(normalized.action)
      || !action
      || !action.available
      || !action.implementation_matches
      || !action.completion_signal_available
      || !actor
      || !actor.available
      || !actor.class_matches
      || !actor.skeleton_matches
      || (normalized.target_binding_id !== null && (!target || !target.available || !target.class_matches))
    ) fail("ANIMATION_UE_PREFLIGHT_MISMATCH", "Snapshot request is not authorized by the active preflight", { status: 409 });
    const key = eventKey(normalized);
    if (snapshotsByKey.has(key)) fail("ANIMATION_UE_STATE_CONFLICT", "Animation event already has an active snapshot", { status: 409 });
    const response = await invoke(
      "snapshotAnimationState",
      normalized,
      signal,
      (payload) => validateSnapshotResponse(normalized, payload),
    );
    if (snapshotsById.has(response.snapshot_id)) fail("ANIMATION_UE_SNAPSHOT_PROTOCOL_INVALID", "Snapshot id was reused", { status: 502 });
    const state = {
      key,
      request: normalized,
      snapshotId: response.snapshot_id,
      stateDigest: response.state_digest,
      startAttempted: false,
      handle: null,
      restoreAttempted: false,
    };
    snapshotsByKey.set(key, state);
    snapshotsById.set(response.snapshot_id, state);
    return response;
  }

  async function startAnimationAction(request, optionsArg) {
    const { signal } = requestOptions(optionsArg);
    const normalized = validateStartRequest(request);
    bindActivePreflight(normalized.preflight_id);
    const key = eventKey(normalized);
    const snapshot = snapshotsByKey.get(key);
    const actionProfile = bridgeActions.get(normalized.bridge_action_id);
    if (
      !snapshot
      || snapshot.startAttempted
      || !actionProfile
      || snapshot.request.action !== normalized.action
      || snapshot.request.actor_binding_id !== normalized.actor_binding_id
      || snapshot.request.target_binding_id !== normalized.target_binding_id
    ) fail("ANIMATION_UE_SNAPSHOT_REQUIRED", "Start request does not match one unused event snapshot", { status: 409 });
    snapshot.startAttempted = true;
    const wireRequest = deepFreeze({
      schema: START_REQUEST_SCHEMA,
      preflight_id: normalized.preflight_id,
      run_id: normalized.run_id,
      event_id: normalized.event_id,
      bridge_action_id: normalized.bridge_action_id,
      actor_binding_id: normalized.actor_binding_id,
      target_binding_id: normalized.target_binding_id,
      parameters: normalized.parameters,
    });
    const response = await invoke(
      "startAnimationAction",
      wireRequest,
      signal,
      (payload) => validateStartResponse(wireRequest, payload),
    );
    if (handles.has(response.action_handle)) fail("ANIMATION_UE_START_PROTOCOL_INVALID", "Action handle was reused", { status: 502 });
    const handle = {
      key,
      action: normalized.action,
      profile: actionProfile,
      snapshot,
      state: "started",
      stopAttempted: false,
      releaseAttempted: false,
    };
    handles.set(response.action_handle, handle);
    snapshot.handle = response.action_handle;
    return response;
  }

  async function waitAnimationAction(request, optionsArg) {
    const { signal } = requestOptions(optionsArg);
    const normalized = validateWaitRequest(request);
    const handle = requireHandle(normalized, new Set(["started"]));
    const response = await invoke(
      "waitAnimationAction",
      normalized,
      signal,
      (payload) => validateWaitResponse(normalized, payload, handle.profile.completion_signal),
    );
    handle.state = "completed";
    return response;
  }

  async function stopAnimationAction(request, optionsArg) {
    const { signal } = requestOptions(optionsArg);
    const normalized = validateStopRequest(request);
    const handle = requireHandle(normalized, new Set(["started", "completed"]));
    if (handle.stopAttempted) fail("ANIMATION_UE_STATE_CONFLICT", "Stop mutation was already attempted", { status: 409 });
    handle.stopAttempted = true;
    const response = await invoke(
      "stopAnimationAction",
      normalized,
      signal,
      (payload) => validateStopResponse(normalized, payload),
    );
    handle.state = "stopped";
    return response;
  }

  async function releaseAnimationAction(request, optionsArg) {
    const { signal } = requestOptions(optionsArg);
    const normalized = validateReleaseRequest(request);
    const handle = requireHandle(normalized, new Set(["started", "completed", "stopped"]));
    if (handle.releaseAttempted) fail("ANIMATION_UE_STATE_CONFLICT", "Release mutation was already attempted", { status: 409 });
    if (handle.state === "started" && !handle.stopAttempted) {
      fail("ANIMATION_UE_STOP_REQUIRED", "A non-completed action must be stopped before transient controls are released", { status: 409 });
    }
    handle.releaseAttempted = true;
    const previousState = handle.state;
    const response = await invoke(
      "releaseAnimationAction",
      normalized,
      signal,
      (payload) => validateReleaseResponse(normalized, payload),
    );
    handle.state = "released";
    if (previousState === "completed") {
      snapshotsByKey.delete(handle.snapshot.key);
      snapshotsById.delete(handle.snapshot.snapshotId);
    }
    return response;
  }

  async function restoreAnimationState(request, optionsArg) {
    const { signal } = requestOptions(optionsArg);
    const normalized = validateRestoreRequest(request);
    bindActivePreflight(normalized.preflight_id);
    const snapshot = snapshotsById.get(normalized.snapshot_id);
    if (
      !snapshot
      || snapshot.key !== eventKey(normalized)
      || snapshot.stateDigest !== normalized.state_digest
      || snapshot.restoreAttempted
    ) fail("ANIMATION_UE_SNAPSHOT_INVALID", "Restore request does not match one active snapshot", { status: 409 });
    if (snapshot.handle) {
      const handle = handles.get(snapshot.handle);
      if (handle && !handle.releaseAttempted) {
        fail("ANIMATION_UE_RELEASE_REQUIRED", "Known transient controls must be released before restore", { status: 409 });
      }
    }
    snapshot.restoreAttempted = true;
    const response = await invoke(
      "restoreAnimationState",
      normalized,
      signal,
      (payload) => validateRestoreResponse(normalized, payload),
    );
    snapshotsByKey.delete(snapshot.key);
    snapshotsById.delete(snapshot.snapshotId);
    if (snapshot.handle && handles.has(snapshot.handle)) handles.get(snapshot.handle).state = "restored";
    return response;
  }

  return Object.freeze({
    preflightAnimation,
    snapshotAnimationState,
    startAnimationAction,
    waitAnimationAction,
    stopAnimationAction,
    releaseAnimationAction,
    restoreAnimationState,
  });
}

module.exports = {
  ANIMATION_UE_CONTENT_PROOF_SCHEMA,
  ANIMATION_UE_MARKER_SCHEMA,
  ANIMATION_UE_OPERATION_ALLOWLIST,
  ANIMATION_UE_OPERATION_CONTRACT_SCHEMA,
  ANIMATION_UE_REQUEST_SCHEMA,
  ANIMATION_UE_RESPONSE_SCHEMA,
  VistaAnimationUeAdapterError,
  createVistaAnimationUeAdapter,
};
