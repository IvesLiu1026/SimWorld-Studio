"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const {
  ACTION_DEFINITIONS,
  cloneJson,
  deepFreeze,
  digest,
  validateContentProfile,
} = require("./vista-animation-contract");
const { createVistaAnimationRuntime } = require("./vista-animation-runtime");
const { createVistaAnimationTimelineService } = require("./vista-animation-timeline-service");
const { createVistaAnimationUeAdapter } = require("./vista-animation-ue-adapter");
const {
  ANIMATION_UE_PLUGIN_API_SCHEMA,
  ANIMATION_UE_PLUGIN_ARTIFACT_SCHEMA,
  ANIMATION_UE_PLUGIN_NAME,
  createVistaAnimationUeReadinessProbe,
} = require("./vista-animation-ue-readiness");
const { validateVistaSceneBuildPlan } = require("./vista-scene-build-plan");
const { BINDINGS_SCHEMA, validateBindings } = require("./vista-timeline-compiler");

const ENGINE_TIME_REQUEST_SCHEMA = "vista-animation-engine-time-request/v1";
const ENGINE_TIME_RESPONSE_SCHEMA = "vista-animation-engine-time-response/v1";
const EVIDENCE_CAPTURE_REQUEST_SCHEMA = "vista-animation-evidence-capture-request/v1";
const EVIDENCE_CAPTURE_RESPONSE_SCHEMA = "vista-animation-evidence-capture-response/v1";
const MAX_CONTENT_PROFILE_BYTES = 2 * 1024 * 1024;
const MAX_PLUGIN_ARTIFACT_BYTES = 256 * 1024;
const MAX_TRANSPORT_RESPONSE_BYTES = 256 * 1024;
const DEFAULT_PROBE_TIMEOUT_MS = 3_000;
const DEFAULT_READINESS_MAX_AGE_MS = 30_000;
const MAX_READINESS_PROOFS = 128;
const GLOBAL_READINESS_SCHEMA = "vista-animation-global-readiness/v1";

const SHA256_RE = /^[a-f0-9]{64}$/;
const SAFE_ID_RE = /^[a-z][a-z0-9_-]{0,119}$/;
const OPAQUE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,159}$/;
const PLAN_ID_RE = /^vsp-[a-f0-9]{24}$/;
const VERSION_RE = /^\d+\.\d+\.\d+(?:-[A-Za-z0-9][A-Za-z0-9.-]{0,63})?$/;
const PLATFORM_RE = /^[a-z0-9][a-z0-9_.-]{0,63}$/;
const PRODUCTION_FIXTURE_RE = /(?:^|[._:@/\-])(?:test|fixture|demo|golden)(?:$|[._:@/\-])/i;
const EVIDENCE_KINDS = Object.freeze([
  "pose_snapshot",
  "interaction_state",
  "screenshot",
  "scene_validation",
]);
const DEDICATED_TRANSPORT_METHODS = Object.freeze([
  "captureAnimationEvidence",
  "invokeAnimationContentApi",
  "probeAnimationContentApi",
  "sampleAnimationEngineTime",
]);

const CONFIG_KEYS = Object.freeze([
  "VISTA_ANIMATION_CONTENT_PROFILE_FILE",
  "VISTA_ANIMATION_CONTENT_PROFILE_SHA256",
  "VISTA_ANIMATION_UE_PLUGIN_ARTIFACT_FILE",
  "VISTA_ANIMATION_UE_PLUGIN_ARTIFACT_SHA256",
  "VISTA_ANIMATION_RECORD_ROOT",
]);

const READINESS_CAUSES = Object.freeze({
  ANIMATION_UE_READINESS_DISABLED: ["VISTA animation timeline runtime is disabled.", false],
  ANIMATION_UE_READINESS_NOT_CONFIGURED: ["Pinned VISTA animation runtime receipts are not configured.", false],
  ANIMATION_UE_PLUGIN_TRANSPORT_MISSING: ["Dedicated VISTA animation UE transport is not configured.", false],
  ANIMATION_UE_SESSION_REVALIDATION_MISSING: ["Active Studio lease revalidation is not configured.", false],
  ANIMATION_UE_READINESS_PROOF_MISSING: ["No current session-bound live animation plugin proof is available.", true],
  ANIMATION_UE_READINESS_PROOF_STALE: ["The session-bound live animation plugin proof expired.", true],
  ANIMATION_UE_SESSION_BINDING_REVOKED: ["The Studio lease that produced the animation plugin proof is no longer active.", true],
  ANIMATION_UE_READINESS_PROOF_MISMATCH: ["The live animation plugin proof does not match the pinned runtime and Studio slot.", false],
  ANIMATION_UE_PLUGIN_PROBE_ABORTED: ["The live animation plugin readiness check was cancelled.", true],
});

class VistaAnimationTimelineRuntimeError extends Error {
  constructor(code, message, { status = 500, retryable = false } = {}) {
    super(message);
    this.name = "VistaAnimationTimelineRuntimeError";
    this.code = code;
    this.status = status;
    this.retryable = retryable;
  }
}

function fail(code, message, options) {
  throw new VistaAnimationTimelineRuntimeError(code, message, options);
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function exactKeys(value, allowed, required, pointer, code = "ANIMATION_RUNTIME_CONFIG_INVALID") {
  if (!isPlainObject(value)) fail(code, `${pointer} must be an object`);
  const allowedSet = new Set(allowed);
  const unknown = Object.keys(value).filter((key) => !allowedSet.has(key));
  const missing = required.filter((key) => !Object.prototype.hasOwnProperty.call(value, key));
  if (unknown.length || missing.length) fail(code, `${pointer} has an invalid shape`);
}

function flag(value) {
  return /^(?:1|true|yes|on)$/i.test(String(value || "").trim());
}

function envText(env, key) {
  return typeof env[key] === "string" ? env[key].trim() : "";
}

function safeAbsolutePath(value, label) {
  const text = typeof value === "string" ? value.trim() : "";
  if (!text || !path.isAbsolute(text)) fail("ANIMATION_RUNTIME_CONFIG_INVALID", `${label} must be an absolute path`);
  const resolved = path.resolve(text);
  if (resolved === path.parse(resolved).root) fail("ANIMATION_RUNTIME_CONFIG_INVALID", `${label} cannot be a filesystem root`);
  return resolved;
}

function sameStat(left, right) {
  return left.dev === right.dev
    && left.ino === right.ino
    && left.size === right.size
    && left.mtimeMs === right.mtimeMs
    && left.ctimeMs === right.ctimeMs;
}

function readPinnedRegularJson(file, expectedSha256, label, maxBytes, fsImpl = fs) {
  if (!SHA256_RE.test(expectedSha256)) {
    fail("ANIMATION_RUNTIME_CONFIG_INVALID", `${label} SHA256 must be a lowercase SHA-256 digest`);
  }
  let descriptor;
  try {
    descriptor = fsImpl.openSync(file, fs.constants.O_RDONLY | Number(fs.constants.O_NOFOLLOW || 0));
    const before = fsImpl.fstatSync(descriptor);
    if (!before.isFile() || before.size < 2 || before.size > maxBytes) {
      fail("ANIMATION_RUNTIME_CONFIG_INVALID", `${label} must be a bounded regular file`);
    }
    const raw = fsImpl.readFileSync(descriptor, "utf8");
    const after = fsImpl.fstatSync(descriptor);
    if (!sameStat(before, after) || Buffer.byteLength(raw, "utf8") !== after.size) {
      fail("ANIMATION_RUNTIME_CONFIG_INVALID", `${label} changed while it was being verified`);
    }
    const actualSha256 = crypto.createHash("sha256").update(raw, "utf8").digest("hex");
    if (actualSha256 !== expectedSha256) {
      fail("ANIMATION_RUNTIME_CONFIG_INVALID", `${label} checksum does not match its immutable pin`);
    }
    let value;
    try {
      value = JSON.parse(raw);
    } catch {
      fail("ANIMATION_RUNTIME_CONFIG_INVALID", `${label} must contain valid JSON`);
    }
    if (!isPlainObject(value)) fail("ANIMATION_RUNTIME_CONFIG_INVALID", `${label} must contain a JSON object`);
    return value;
  } catch (error) {
    if (error instanceof VistaAnimationTimelineRuntimeError) throw error;
    fail("ANIMATION_RUNTIME_CONFIG_INVALID", `${label} could not be opened as a pinned regular file`);
  } finally {
    if (descriptor !== undefined) {
      try { fsImpl.closeSync(descriptor); } catch { /* best effort */ }
    }
  }
}

function requireString(value, pointer, pattern, max = 160) {
  if (typeof value !== "string" || value.length === 0 || value.length > max || !pattern.test(value)) {
    fail("ANIMATION_RUNTIME_CONFIG_INVALID", `${pointer} is invalid`);
  }
  return value;
}

function validatePluginArtifact(value) {
  const keys = [
    "schema",
    "plugin_name",
    "plugin_version",
    "plugin_build_id",
    "binary_sha256",
    "engine_version",
    "target_platform",
    "api_schema",
  ];
  exactKeys(value, keys, keys, "animation UE plugin artifact");
  if (
    value.schema !== ANIMATION_UE_PLUGIN_ARTIFACT_SCHEMA
    || value.plugin_name !== ANIMATION_UE_PLUGIN_NAME
    || value.api_schema !== ANIMATION_UE_PLUGIN_API_SCHEMA
  ) fail("ANIMATION_RUNTIME_CONFIG_INVALID", "Animation UE plugin artifact identity is invalid");
  return deepFreeze({
    schema: ANIMATION_UE_PLUGIN_ARTIFACT_SCHEMA,
    plugin_name: ANIMATION_UE_PLUGIN_NAME,
    plugin_version: requireString(value.plugin_version, "plugin artifact.plugin_version", VERSION_RE, 80),
    plugin_build_id: requireString(value.plugin_build_id, "plugin artifact.plugin_build_id", OPAQUE_ID_RE),
    binary_sha256: requireString(value.binary_sha256, "plugin artifact.binary_sha256", SHA256_RE, 64),
    engine_version: requireString(value.engine_version, "plugin artifact.engine_version", OPAQUE_ID_RE),
    target_platform: requireString(value.target_platform, "plugin artifact.target_platform", PLATFORM_RE, 64),
    api_schema: ANIMATION_UE_PLUGIN_API_SCHEMA,
  });
}

function rejectProductionFixture(contentProfile, pluginArtifact, files) {
  const values = [
    ...files,
    contentProfile.profile_id,
    contentProfile.revision,
    contentProfile.content_revision,
    contentProfile.verification.receipt_id,
    contentProfile.pawn_class_path,
    contentProfile.skeleton_path,
    ...contentProfile.actions.map((entry) => entry.implementation_asset),
    pluginArtifact.plugin_build_id,
  ];
  if (values.some((value) => PRODUCTION_FIXTURE_RE.test(value))) {
    fail("ANIMATION_RUNTIME_CONFIG_INVALID", "Test/demo animation receipts are not allowed in production");
  }
}

function normalizeReadinessMaxAge(value) {
  const normalized = value === undefined ? DEFAULT_READINESS_MAX_AGE_MS : Number(value);
  if (!Number.isInteger(normalized) || normalized < 1_000 || normalized > 300_000) {
    fail("ANIMATION_RUNTIME_CONFIG_INVALID", "readinessMaxAgeMs must be an integer in [1000, 300000]");
  }
  return normalized;
}

function resolveVistaAnimationTimelineConfig(env = process.env, options = {}) {
  const production = envText(env, "NODE_ENV").toLowerCase() === "production";
  const enabled = flag(env.VISTA_ANIMATION_TIMELINE_ENABLED);
  const baseDir = path.resolve(options.baseDir || __dirname);
  const readinessMaxAgeMs = normalizeReadinessMaxAge(options.readinessMaxAgeMs);
  const configuredKeys = CONFIG_KEYS.filter((key) => envText(env, key));
  if (configuredKeys.length > 0 && configuredKeys.length !== CONFIG_KEYS.length) {
    fail("ANIMATION_RUNTIME_CONFIG_INVALID", `${CONFIG_KEYS.join(", ")} must be configured together`);
  }
  const fallbackRecordRoot = path.resolve(
    options.defaultRecordRoot || path.join(baseDir, "..", ".runtime", "vista-animation-timeline"),
  );
  if (!enabled || configuredKeys.length === 0) {
    return deepFreeze({
      enabled,
      configured: false,
      production,
      contentProfile: null,
      pluginArtifact: null,
      recordRoot: fallbackRecordRoot,
      probeTimeoutMs: DEFAULT_PROBE_TIMEOUT_MS,
      readinessMaxAgeMs,
    });
  }

  const contentProfileFile = safeAbsolutePath(
    envText(env, "VISTA_ANIMATION_CONTENT_PROFILE_FILE"),
    "VISTA_ANIMATION_CONTENT_PROFILE_FILE",
  );
  const pluginArtifactFile = safeAbsolutePath(
    envText(env, "VISTA_ANIMATION_UE_PLUGIN_ARTIFACT_FILE"),
    "VISTA_ANIMATION_UE_PLUGIN_ARTIFACT_FILE",
  );
  const contentProfileRaw = readPinnedRegularJson(
    contentProfileFile,
    envText(env, "VISTA_ANIMATION_CONTENT_PROFILE_SHA256"),
    "VISTA animation content profile",
    MAX_CONTENT_PROFILE_BYTES,
    options.fsImpl || fs,
  );
  let contentProfile;
  try {
    contentProfile = validateContentProfile(contentProfileRaw);
  } catch {
    fail("ANIMATION_RUNTIME_CONFIG_INVALID", "VISTA animation content profile failed exact validation");
  }
  const pluginArtifact = validatePluginArtifact(readPinnedRegularJson(
    pluginArtifactFile,
    envText(env, "VISTA_ANIMATION_UE_PLUGIN_ARTIFACT_SHA256"),
    "VISTA animation UE plugin artifact",
    MAX_PLUGIN_ARTIFACT_BYTES,
    options.fsImpl || fs,
  ));
  if (production) rejectProductionFixture(contentProfile, pluginArtifact, [contentProfileFile, pluginArtifactFile]);
  const probeTimeoutText = envText(env, "VISTA_ANIMATION_UE_PROBE_TIMEOUT_MS");
  const probeTimeoutMs = probeTimeoutText ? Number(probeTimeoutText) : DEFAULT_PROBE_TIMEOUT_MS;
  if (!Number.isInteger(probeTimeoutMs) || probeTimeoutMs < 1 || probeTimeoutMs > 10_000) {
    fail("ANIMATION_RUNTIME_CONFIG_INVALID", "VISTA_ANIMATION_UE_PROBE_TIMEOUT_MS must be an integer in [1, 10000]");
  }
  return deepFreeze({
    enabled: true,
    configured: true,
    production,
    contentProfile,
    pluginArtifact,
    recordRoot: safeAbsolutePath(envText(env, "VISTA_ANIMATION_RECORD_ROOT"), "VISTA_ANIMATION_RECORD_ROOT"),
    probeTimeoutMs,
    readinessMaxAgeMs,
  });
}

function normalizeRuntimeIdentity(value) {
  const keys = ["ownerId", "sessionId", "slotId", "leaseId", "mcpPort", "sceneRevision", "planId"];
  exactKeys(value, keys, keys, "animation runtime identity", "ANIMATION_RUNTIME_IDENTITY_INVALID");
  const ownerId = requireRuntimeIdentityString(value.ownerId, "ownerId", OPAQUE_ID_RE);
  const sessionId = requireRuntimeIdentityString(value.sessionId, "sessionId", OPAQUE_ID_RE);
  const leaseId = requireRuntimeIdentityString(value.leaseId, "leaseId", OPAQUE_ID_RE);
  const sceneRevision = requireRuntimeIdentityString(value.sceneRevision, "sceneRevision", OPAQUE_ID_RE);
  const planId = requireRuntimeIdentityString(value.planId, "planId", PLAN_ID_RE);
  if (!Number.isSafeInteger(value.slotId) || value.slotId < 0 || value.slotId > 1023
      || !Number.isSafeInteger(value.mcpPort) || value.mcpPort < 1 || value.mcpPort > 65535) {
    fail("ANIMATION_RUNTIME_IDENTITY_INVALID", "Animation runtime slot/port identity is invalid", { status: 409 });
  }
  return deepFreeze({ ownerId, sessionId, slotId: value.slotId, leaseId, mcpPort: value.mcpPort, sceneRevision, planId });
}

function requireRuntimeIdentityString(value, field, pattern) {
  if (typeof value !== "string" || !pattern.test(value)) {
    fail("ANIMATION_RUNTIME_IDENTITY_INVALID", `Animation runtime ${field} is invalid`, { status: 409 });
  }
  return value;
}

function normalizeDedicatedTransport(value) {
  if (!isPlainObject(value)) {
    fail("ANIMATION_UE_TRANSPORT_INVALID", "Dedicated VISTA animation transport is unavailable", { status: 503, retryable: true });
  }
  const keys = Object.keys(value).sort();
  if (keys.length !== DEDICATED_TRANSPORT_METHODS.length
      || keys.some((key, index) => key !== DEDICATED_TRANSPORT_METHODS[index])) {
    fail("ANIMATION_UE_TRANSPORT_INVALID", "Animation transport must expose only the fixed dedicated APIs", { status: 503 });
  }
  for (const method of DEDICATED_TRANSPORT_METHODS) {
    if (typeof value[method] !== "function") {
      fail("ANIMATION_UE_TRANSPORT_INVALID", `Animation transport.${method} must be a function`, { status: 503 });
    }
  }
  return Object.freeze(Object.fromEntries(DEDICATED_TRANSPORT_METHODS.map((method) => [
    method,
    (...args) => value[method](...args),
  ])));
}

function parseTransportJson(raw, label) {
  if (typeof raw !== "string" || Buffer.byteLength(raw, "utf8") > MAX_TRANSPORT_RESPONSE_BYTES) {
    fail("ANIMATION_UE_TRANSPORT_PROTOCOL_INVALID", `${label} must return bounded JSON text`, { status: 502 });
  }
  try {
    const parsed = JSON.parse(raw);
    if (!isPlainObject(parsed)) throw new Error("not object");
    return parsed;
  } catch {
    fail("ANIMATION_UE_TRANSPORT_PROTOCOL_INVALID", `${label} returned invalid JSON`, { status: 502 });
  }
}

function makeSlotBinding(identity) {
  return deepFreeze({
    owner_id: identity.ownerId,
    session_id: identity.sessionId,
    slot_id: String(identity.slotId),
    scene_revision: identity.sceneRevision,
  });
}

function leaseBinding(identity) {
  return deepFreeze({
    ownerId: identity.ownerId,
    sessionId: identity.sessionId,
    slotId: identity.slotId,
    leaseId: identity.leaseId,
    mcpPort: identity.mcpPort,
  });
}

function clockMilliseconds(clock) {
  let value;
  try {
    value = clock();
  } catch {
    fail("ANIMATION_RUNTIME_CLOCK_INVALID", "Animation runtime clock failed");
  }
  const date = value instanceof Date ? value : new Date(value);
  if (!Number.isFinite(date.getTime())) fail("ANIMATION_RUNTIME_CLOCK_INVALID", "Animation runtime clock is invalid");
  return date.getTime();
}

function readinessNotReady(code, revision = {}) {
  const cause = READINESS_CAUSES[code] || READINESS_CAUSES.ANIMATION_UE_READINESS_PROOF_MISSING;
  return deepFreeze({
    status: "not_ready",
    revision: {
      schema: GLOBAL_READINESS_SCHEMA,
      animation_content_api: "not_ready",
      ...cloneJson(revision),
    },
    causes: [{
      code: READINESS_CAUSES[code] ? code : "ANIMATION_UE_READINESS_PROOF_MISSING",
      message: cause[0],
      retryable: cause[1],
      dependency: "vista_animation_ue_plugin",
    }],
  });
}

function sanitizedLiveProof(result, identity, config, nowMs) {
  if (!isPlainObject(result) || result.status !== "ready"
      || !Array.isArray(result.causes) || result.causes.length !== 0
      || !isPlainObject(result.revision)) return null;
  const revision = result.revision;
  const keys = [
    "plugin_name", "plugin_version", "plugin_build_id", "binary_sha256",
    "engine_version", "target_platform", "api_schema", "profile_id",
    "profile_revision", "content_revision", "content_digest", "slot_binding_digest",
    "operation_allowlist_digest", "process_instance_id", "checked_at", "verification",
  ];
  if (Object.keys(revision).length !== keys.length
      || keys.some((key) => !Object.prototype.hasOwnProperty.call(revision, key))) return null;
  const expectedSlotBindingDigest = digest({
    schema: "vista-animation-ue-slot-binding/v1",
    ...makeSlotBinding(identity),
  });
  const artifact = config.pluginArtifact;
  const profile = config.contentProfile;
  const checkedAtMs = Date.parse(revision.checked_at);
  if (
    revision.verification !== "live_plugin_challenge"
    || revision.plugin_name !== artifact.plugin_name
    || revision.plugin_version !== artifact.plugin_version
    || revision.plugin_build_id !== artifact.plugin_build_id
    || revision.binary_sha256 !== artifact.binary_sha256
    || revision.engine_version !== artifact.engine_version
    || revision.target_platform !== artifact.target_platform
    || revision.api_schema !== artifact.api_schema
    || revision.profile_id !== profile.profile_id
    || revision.profile_revision !== profile.revision
    || revision.content_revision !== profile.content_revision
    || revision.content_digest !== profile.content_digest
    || revision.slot_binding_digest !== expectedSlotBindingDigest
    || !SHA256_RE.test(revision.operation_allowlist_digest)
    || !OPAQUE_ID_RE.test(revision.process_instance_id)
    || !Number.isFinite(checkedAtMs)
    || checkedAtMs > nowMs + 5_000
    || nowMs - checkedAtMs > config.readinessMaxAgeMs
  ) return null;
  return deepFreeze({
    revision: cloneJson(revision),
    checkedAtMs,
    expiresAtMs: checkedAtMs + config.readinessMaxAgeMs,
    identity: leaseBinding(identity),
    key: digest({
      ...leaseBinding(identity),
      sceneRevision: identity.sceneRevision,
      planId: identity.planId,
    }),
  });
}

function createGlobalAnimationReadiness({
  config,
  clock,
  isActiveSessionBinding,
  unavailableCode = null,
} = {}) {
  const proofs = new Map();

  function prune(nowMs) {
    let expired = false;
    for (const [key, proof] of proofs) {
      if (nowMs > proof.expiresAtMs) {
        proofs.delete(key);
        expired = true;
      }
    }
    return expired;
  }

  function record(result, identity) {
    const nowMs = clockMilliseconds(clock);
    const proof = sanitizedLiveProof(result, identity, config, nowMs);
    if (!proof) return false;
    prune(nowMs);
    proofs.delete(proof.key);
    proofs.set(proof.key, proof);
    while (proofs.size > MAX_READINESS_PROOFS) proofs.delete(proofs.keys().next().value);
    return true;
  }

  async function revalidate(identity) {
    if (typeof isActiveSessionBinding !== "function") return false;
    try {
      return await isActiveSessionBinding(identity) === true;
    } catch {
      return false;
    }
  }

  async function animationUeProbe({ signal } = {}) {
    if (signal && signal.aborted) return readinessNotReady("ANIMATION_UE_PLUGIN_PROBE_ABORTED");
    if (unavailableCode) return readinessNotReady(unavailableCode);
    if (typeof isActiveSessionBinding !== "function") {
      return readinessNotReady("ANIMATION_UE_SESSION_REVALIDATION_MISSING");
    }
    const nowMs = clockMilliseconds(clock);
    const expired = prune(nowMs);
    if (proofs.size === 0) {
      return readinessNotReady(expired
        ? "ANIMATION_UE_READINESS_PROOF_STALE"
        : "ANIMATION_UE_READINESS_PROOF_MISSING");
    }
    const candidates = [...proofs.values()].sort((left, right) => right.checkedAtMs - left.checkedAtMs);
    for (const proof of candidates) {
      if (signal && signal.aborted) return readinessNotReady("ANIMATION_UE_PLUGIN_PROBE_ABORTED");
      if (!await revalidate(proof.identity)) {
        proofs.delete(proof.key);
        continue;
      }
      if (signal && signal.aborted) return readinessNotReady("ANIMATION_UE_PLUGIN_PROBE_ABORTED");
      return deepFreeze({
        status: "ready",
        revision: {
          ...cloneJson(proof.revision),
          proof_expires_at: new Date(proof.expiresAtMs).toISOString(),
        },
        causes: [],
      });
    }
    return readinessNotReady("ANIMATION_UE_SESSION_BINDING_REVOKED");
  }

  return Object.freeze({ animationUeProbe, record, revalidate });
}

function createEngineTimeSampler(transport, identity) {
  return async function sampleEngineTime(context) {
    const allowed = [
      "run_id", "timeline_id", "event_id", "signal", "owner_id", "session_id",
      "slot_id", "lease_id", "mcp_port", "scene_revision",
    ];
    exactKeys(context, allowed, allowed, "engine time context", "ANIMATION_ENGINE_TIME_CONTEXT_INVALID");
    if (
      context.owner_id !== identity.ownerId
      || context.session_id !== identity.sessionId
      || context.slot_id !== String(identity.slotId)
      || context.lease_id !== identity.leaseId
      || context.mcp_port !== identity.mcpPort
      || context.scene_revision !== identity.sceneRevision
    ) fail("ANIMATION_ENGINE_TIME_IDENTITY_MISMATCH", "Engine time request does not match the active Studio slot", { status: 409 });
    const requestBody = {
      schema: ENGINE_TIME_REQUEST_SCHEMA,
      run_id: context.run_id,
      timeline_id: context.timeline_id,
      event_id: context.event_id,
      slot_binding: makeSlotBinding(identity),
    };
    const request = deepFreeze({ ...requestBody, request_digest: digest(requestBody) });
    let raw;
    try {
      raw = await transport.sampleAnimationEngineTime(JSON.stringify(request), Object.freeze({
        requestDigest: request.request_digest,
        mutation: false,
        maxAttempts: 1,
        signal: context.signal,
      }));
    } catch (error) {
      if (error instanceof VistaAnimationTimelineRuntimeError) throw error;
      fail("ANIMATION_ENGINE_TIME_UNAVAILABLE", "Trusted UE engine time API is unavailable", { status: 503, retryable: true });
    }
    const response = parseTransportJson(raw, "Trusted UE engine time API");
    exactKeys(
      response,
      ["schema", "run_id", "timeline_id", "event_id", "slot_binding", "request_digest", "engine_time_sec"],
      ["schema", "run_id", "timeline_id", "event_id", "slot_binding", "request_digest", "engine_time_sec"],
      "engine time response",
      "ANIMATION_UE_TRANSPORT_PROTOCOL_INVALID",
    );
    if (
      response.schema !== ENGINE_TIME_RESPONSE_SCHEMA
      || response.run_id !== request.run_id
      || response.timeline_id !== request.timeline_id
      || response.event_id !== request.event_id
      || digest(response.slot_binding) !== digest(request.slot_binding)
      || response.request_digest !== request.request_digest
      || typeof response.engine_time_sec !== "number"
      || !Number.isFinite(response.engine_time_sec)
      || response.engine_time_sec < 0
      || response.engine_time_sec > 315_360_000
    ) fail("ANIMATION_UE_TRANSPORT_PROTOCOL_INVALID", "Trusted UE engine time response correlation is invalid", { status: 502 });
    return response.engine_time_sec;
  };
}

function createEvidenceHooks(transport, identity) {
  const hooks = {};
  for (const kind of EVIDENCE_KINDS) {
    hooks[kind] = async (context) => {
      const requestBase = {
        schema: EVIDENCE_CAPTURE_REQUEST_SCHEMA,
        kind,
        slot_binding: makeSlotBinding(identity),
        context: cloneJson(context),
      };
      if (requestBase.context.scene_revision !== identity.sceneRevision) {
        fail("ANIMATION_EVIDENCE_IDENTITY_MISMATCH", "Evidence request does not match the active scene", { status: 409 });
      }
      const request = deepFreeze({ ...requestBase, context_digest: digest(requestBase) });
      let raw;
      try {
        raw = await transport.captureAnimationEvidence(JSON.stringify(request), Object.freeze({
          kind,
          contextDigest: request.context_digest,
          mutation: false,
          maxAttempts: 1,
        }));
      } catch (error) {
        if (error instanceof VistaAnimationTimelineRuntimeError) throw error;
        fail("ANIMATION_EVIDENCE_TRANSPORT_UNAVAILABLE", "Trusted UE animation evidence API is unavailable", { status: 503, retryable: true });
      }
      const response = parseTransportJson(raw, "Trusted UE animation evidence API");
      exactKeys(
        response,
        ["schema", "kind", "context_digest", "evidence"],
        ["schema", "kind", "context_digest", "evidence"],
        "evidence capture response",
        "ANIMATION_UE_TRANSPORT_PROTOCOL_INVALID",
      );
      if (
        response.schema !== EVIDENCE_CAPTURE_RESPONSE_SCHEMA
        || response.kind !== kind
        || response.context_digest !== request.context_digest
        || !isPlainObject(response.evidence)
      ) fail("ANIMATION_UE_TRANSPORT_PROTOCOL_INVALID", "Animation evidence response correlation is invalid", { status: 502 });
      return response.evidence;
    };
  }
  return Object.freeze(hooks);
}

function validateManifestAgainstPlan(plan, result) {
  if (!isPlainObject(result)
      || result.schema !== "vista-scene-build-result/v1"
      || result.build_id !== `vsb-${plan.plan_id.slice(4)}`
      || result.plan_id !== plan.plan_id
      || result.scene_id !== plan.scene_id
      || !new Set(["succeeded", "already_applied"]).has(result.status)
      || !Array.isArray(result.actor_manifest)
      || result.actor_manifest.length !== plan.actors.length) {
    fail("ANIMATION_BINDINGS_MISMATCH", "Scene build result does not match the exact verified BuildPlan", { status: 409 });
  }
  const manifests = new Map();
  for (const entry of result.actor_manifest) {
    if (!isPlainObject(entry) || typeof entry.actor_id !== "string" || manifests.has(entry.actor_id)) {
      fail("ANIMATION_BINDINGS_MISMATCH", "Scene build actor manifest identities are invalid", { status: 409 });
    }
    manifests.set(entry.actor_id, entry);
  }
  for (const actor of plan.actors) {
    const entry = manifests.get(actor.actor_id);
    const expectedOperationId = `vso-${crypto.createHash("sha256")
      .update(`${plan.plan_id}\0${actor.actor_name}\0${actor.fingerprint}`, "utf8")
      .digest("hex")
      .slice(0, 24)}`;
    if (!entry
        || entry.actor_name !== actor.actor_name
        || entry.runtime_actor_name !== actor.actor_name
        || entry.fingerprint !== actor.fingerprint
        || entry.source_entity_id !== actor.source_entity_id
        || entry.component_id !== actor.component_id
        || entry.operation_id !== expectedOperationId
        || !new Set(["spawned", "reused"]).has(entry.disposition)
        || typeof entry.object_guid !== "string" || !OPAQUE_ID_RE.test(entry.object_guid)
        || !isPlainObject(entry.asset)
        || entry.asset.asset_id !== actor.asset.asset_id
        || entry.asset.ue_path !== actor.asset.ue_path
        || entry.asset.class_path !== actor.asset.class_path
        || entry.asset.content_revision !== actor.asset.content_revision
        || entry.asset.verification_revision !== actor.asset.verification_revision) {
      fail("ANIMATION_BINDINGS_MISMATCH", `Scene build actor '${actor.actor_id}' is missing or stale`, { status: 409 });
    }
  }
  return manifests;
}

function actionCapabilities(events, contentProfile, kind) {
  const verifiedActions = new Set(contentProfile.actions.map((entry) => entry.action));
  const capabilities = new Set();
  for (const event of events) {
    const definition = ACTION_DEFINITIONS[event.action];
    if (!definition || !verifiedActions.has(event.action)) continue;
    const values = kind === "actor" ? definition.actor_capabilities : definition.target_capabilities;
    values.forEach((value) => capabilities.add(value));
  }
  return [...capabilities].sort();
}

function chooseActorManifest(actorId, plan, manifests) {
  const sourceId = actorId === "camera_wearer" ? "camera_wearer_hands_and_forearms" : actorId;
  const candidates = plan.actors.filter((actor) => (
    actor.role === "scene_entity"
    && actor.source_entity_id === sourceId
    && actor.asset.kind === "blueprint_class"
    && manifests.has(actor.actor_id)
  ));
  if (candidates.length !== 1) {
    fail("ANIMATION_BINDINGS_MISMATCH", `Timeline actor '${actorId}' does not resolve to one verified pawn actor`, { status: 409 });
  }
  return manifests.get(candidates[0].actor_id);
}

function chooseTargetManifest(targetId, events, plan, manifests) {
  let candidates = plan.actors.filter((actor) => (
    actor.role === "scene_entity"
    && actor.source_entity_id === targetId
    && manifests.has(actor.actor_id)
  ));
  if (candidates.length > 1) {
    const nonContactActions = events.every((event) => new Set(["look_at", "pause"]).has(event.action));
    const cameraTarget = candidates.filter((actor) => actor.actor_id === plan.camera.target_actor_id);
    if (nonContactActions && cameraTarget.length === 1) candidates = cameraTarget;
  }
  if (candidates.length !== 1) {
    fail("ANIMATION_BINDINGS_MISMATCH", `Timeline target '${targetId}' does not resolve to one verified actor anchor`, { status: 409 });
  }
  return manifests.get(candidates[0].actor_id);
}

function createVerifiedSceneBindingResolver() {
  return async function resolveVerifiedBindings(context) {
    exactKeys(
      context,
      ["sceneSpec", "buildPlan", "buildResult", "contentProfile", "identity"],
      ["sceneSpec", "buildPlan", "buildResult", "contentProfile", "identity"],
      "animation binding context",
      "ANIMATION_BINDINGS_MISMATCH",
    );
    let plan;
    let profile;
    try {
      plan = validateVistaSceneBuildPlan(context.buildPlan);
      profile = validateContentProfile(context.contentProfile);
    } catch {
      fail("ANIMATION_BINDINGS_MISMATCH", "Animation binding inputs failed exact validation", { status: 409 });
    }
    const scene = context.sceneSpec;
    if (!isPlainObject(scene) || scene.schema !== "vista-simworld-scene/v1"
        || scene.scene_id !== plan.scene_id || !Array.isArray(scene.timeline) || scene.timeline.length === 0) {
      fail("ANIMATION_BINDINGS_MISMATCH", "Animation binding SceneSpec does not match the verified BuildPlan", { status: 409 });
    }
    exactKeys(
      context.identity,
      ["ownerId", "sessionId", "slotId", "leaseId", "mcpPort"],
      ["ownerId", "sessionId", "slotId", "leaseId", "mcpPort"],
      "animation binding identity",
      "ANIMATION_BINDINGS_MISMATCH",
    );
    if (!OPAQUE_ID_RE.test(context.identity.ownerId)
        || !OPAQUE_ID_RE.test(context.identity.sessionId)
        || !OPAQUE_ID_RE.test(context.identity.leaseId)
        || !Number.isSafeInteger(context.identity.slotId) || context.identity.slotId < 0 || context.identity.slotId > 1023
        || !Number.isSafeInteger(context.identity.mcpPort) || context.identity.mcpPort < 1 || context.identity.mcpPort > 65535) {
      fail("ANIMATION_BINDINGS_MISMATCH", "Animation binding identity is invalid", { status: 409 });
    }
    const manifests = validateManifestAgainstPlan(plan, context.buildResult);
    const actorEvents = new Map();
    const targetEvents = new Map();
    for (const event of scene.timeline) {
      if (!isPlainObject(event) || !SAFE_ID_RE.test(event.actor_id) || !ACTION_DEFINITIONS[event.action]) {
        fail("ANIMATION_BINDINGS_MISMATCH", "Animation timeline contains an unsupported actor/action binding", { status: 409 });
      }
      const actors = actorEvents.get(event.actor_id) || [];
      actors.push(event);
      actorEvents.set(event.actor_id, actors);
      if (event.target_id !== null && event.target_id !== undefined) {
        if (!SAFE_ID_RE.test(event.target_id)) fail("ANIMATION_BINDINGS_MISMATCH", "Animation timeline target identity is invalid", { status: 409 });
        const targets = targetEvents.get(event.target_id) || [];
        targets.push(event);
        targetEvents.set(event.target_id, targets);
      }
    }
    const actors = [...actorEvents.entries()].map(([sourceId, events]) => {
      const manifest = chooseActorManifest(sourceId, plan, manifests);
      return {
        source_id: sourceId,
        binding_id: manifest.actor_id,
        kind: "player",
        capabilities: actionCapabilities(events, profile, "actor"),
      };
    });
    const entities = [...targetEvents.entries()].map(([sourceId, events]) => {
      const manifest = chooseTargetManifest(sourceId, events, plan, manifests);
      return {
        source_id: sourceId,
        binding_id: manifest.actor_id,
        kind: "prop",
        capabilities: actionCapabilities(events, profile, "target"),
      };
    });
    const revisionBody = {
      plan_id: plan.plan_id,
      scene_id: plan.scene_id,
      profile_revision: profile.revision,
      actors,
      entities,
    };
    return validateBindings({
      schema: BINDINGS_SCHEMA,
      revision: `bindings_${digest(revisionBody).slice(0, 24)}`,
      actors,
      entities,
    });
  };
}

function createUnavailableService(code, message) {
  const unavailable = async () => fail(code, message, { status: 503, retryable: false });
  return Object.freeze({
    preflight: unavailable,
    start: unavailable,
    status: unavailable,
    stop: unavailable,
    replay: unavailable,
  });
}

function createVistaAnimationTimelineRuntime(options = {}) {
  if (options.isActiveSessionBinding !== undefined && typeof options.isActiveSessionBinding !== "function") {
    fail("ANIMATION_RUNTIME_CONFIG_INVALID", "isActiveSessionBinding must be a function when configured");
  }
  const clock = typeof options.clock === "function" ? options.clock : () => new Date();
  const config = resolveVistaAnimationTimelineConfig(options.env || process.env, {
    baseDir: options.baseDir || __dirname,
    defaultRecordRoot: options.defaultRecordRoot,
    fsImpl: options.fsImpl,
    readinessMaxAgeMs: options.readinessMaxAgeMs,
  });
  const unavailable = (serviceCode, serviceMessage, readinessCode) => {
    const globalReadiness = createGlobalAnimationReadiness({
      config,
      clock,
      isActiveSessionBinding: options.isActiveSessionBinding,
      unavailableCode: readinessCode,
    });
    return Object.freeze({
      config,
      service: createUnavailableService(serviceCode, serviceMessage),
      runtimeProvider: null,
      bindingResolver: null,
      animationUeProbe: globalReadiness.animationUeProbe,
    });
  };
  if (!config.enabled) {
    return unavailable(
      "ANIMATION_RUNTIME_DISABLED",
      "VISTA animation timeline runtime is disabled",
      "ANIMATION_UE_READINESS_DISABLED",
    );
  }
  if (!config.configured) {
    return unavailable(
      "ANIMATION_RUNTIME_NOT_CONFIGURED",
      "Pinned VISTA animation runtime receipts are not configured",
      "ANIMATION_UE_READINESS_NOT_CONFIGURED",
    );
  }
  if (typeof options.transportResolver !== "function") {
    return unavailable(
      "ANIMATION_UE_TRANSPORT_MISSING",
      "Dedicated VISTA animation UE transport is not configured",
      "ANIMATION_UE_PLUGIN_TRANSPORT_MISSING",
    );
  }
  if (!options.importService || typeof options.importService.status !== "function"
      || !options.sceneBuildService || typeof options.sceneBuildService.status !== "function") {
    fail("ANIMATION_RUNTIME_CONFIG_INVALID", "Enabled animation runtime requires import and scene-build services");
  }
  if (!options.runtimeLifecycle || typeof options.runtimeLifecycle.startForIdentity !== "function"
      || typeof options.runtimeLifecycle.stateForIdentity !== "function"
      || typeof options.runtimeLifecycle.stopForIdentity !== "function") {
    fail("ANIMATION_RUNTIME_CONFIG_INVALID", "Enabled animation runtime requires the lease-bound backend PIE lifecycle");
  }
  const bindingResolver = createVerifiedSceneBindingResolver();
  const globalReadiness = createGlobalAnimationReadiness({
    config,
    clock,
    isActiveSessionBinding: options.isActiveSessionBinding,
  });
  const runtimeProvider = async (rawIdentity) => {
    const identity = normalizeRuntimeIdentity(rawIdentity);
    let resolved;
    try {
      resolved = await options.transportResolver(identity);
    } catch (error) {
      if (error instanceof VistaAnimationTimelineRuntimeError) throw error;
      fail("ANIMATION_UE_TRANSPORT_UNAVAILABLE", "Dedicated VISTA animation UE transport could not be resolved", { status: 503, retryable: true });
    }
    const transport = normalizeDedicatedTransport(resolved);
    const adapter = createVistaAnimationUeAdapter({
      transport,
      contentProfile: config.contentProfile,
    });
    const runtimeClock = () => {
      const value = clock();
      const date = value instanceof Date ? value : new Date(value);
      if (!Number.isFinite(date.getTime())) fail("ANIMATION_RUNTIME_CLOCK_INVALID", "Animation runtime clock is invalid");
      return date.toISOString();
    };
    const readinessClock = () => Date.parse(runtimeClock());
    const runtime = createVistaAnimationRuntime({
      broker: adapter,
      contentProfile: config.contentProfile,
      evidenceHooks: createEvidenceHooks(transport, identity),
      clock: runtimeClock,
    });
    const liveProbe = createVistaAnimationUeReadinessProbe({
      transport,
      expectedArtifact: config.pluginArtifact,
      contentProfile: config.contentProfile,
      slotBinding: makeSlotBinding(identity),
      clock: readinessClock,
      timeoutMs: config.probeTimeoutMs,
    });
    const probeReadiness = async (probeOptions = {}) => {
      const result = await liveProbe(probeOptions);
      if (result && result.status === "ready") {
        if (typeof options.isActiveSessionBinding === "function"
            && !await globalReadiness.revalidate(leaseBinding(identity))) {
          return readinessNotReady("ANIMATION_UE_SESSION_BINDING_REVOKED", result.revision);
        }
        if (typeof options.isActiveSessionBinding === "function"
            && !globalReadiness.record(result, identity)) {
          return readinessNotReady("ANIMATION_UE_READINESS_PROOF_MISMATCH", result.revision);
        }
      }
      return result;
    };
    return Object.freeze({
      runtime,
      probeReadiness,
      engineTimeSampler: createEngineTimeSampler(transport, identity),
    });
  };
  const service = createVistaAnimationTimelineService({
    importService: options.importService,
    sceneBuildService: options.sceneBuildService,
    runtimeLifecycle: options.runtimeLifecycle,
    runtimeProvider,
    bindingResolver,
    recordRoot: config.recordRoot,
    ...(typeof options.clock === "function" ? { clock: options.clock } : {}),
    ...(typeof options.randomBytes === "function" ? { randomBytes: options.randomBytes } : {}),
    ...(isPlainObject(options.schedulerOptions) ? { schedulerOptions: options.schedulerOptions } : {}),
    readinessMaxAgeMs: config.readinessMaxAgeMs,
  });
  return Object.freeze({
    config,
    service,
    runtimeProvider,
    bindingResolver,
    animationUeProbe: globalReadiness.animationUeProbe,
  });
}

module.exports = {
  DEDICATED_TRANSPORT_METHODS,
  ENGINE_TIME_REQUEST_SCHEMA,
  ENGINE_TIME_RESPONSE_SCHEMA,
  EVIDENCE_CAPTURE_REQUEST_SCHEMA,
  EVIDENCE_CAPTURE_RESPONSE_SCHEMA,
  GLOBAL_READINESS_SCHEMA,
  VistaAnimationTimelineRuntimeError,
  createVerifiedSceneBindingResolver,
  createVistaAnimationTimelineRuntime,
  resolveVistaAnimationTimelineConfig,
  validatePluginArtifact,
};
