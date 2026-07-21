"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const {
  canonicalize,
  deepFreeze,
  digest,
  validateContentProfile,
} = require("./vista-animation-contract");
const {
  ANIMATION_UE_CONTENT_PROOF_SCHEMA,
  ANIMATION_UE_MARKER_SCHEMA,
  ANIMATION_UE_OPERATION_ALLOWLIST,
} = require("./vista-animation-ue-adapter");

const ANIMATION_UE_CAPABILITY_SCHEMA = "vista-animation-ue-capability/v1";
const ANIMATION_UE_CAPABILITY_PROBE_SCHEMA = "vista-animation-ue-capability-probe/v1";
const ANIMATION_UE_PLUGIN_ARTIFACT_SCHEMA = "vista-animation-ue-plugin-artifact/v1";
const ANIMATION_UE_SECURITY_POLICY_SCHEMA = "vista-animation-ue-security-policy/v1";
const ANIMATION_UE_SLOT_BINDING_SCHEMA = "vista-animation-ue-slot-binding/v1";
const ANIMATION_UE_SOURCE_AUDIT_SCHEMA = "vista-animation-ue-source-audit/v1";
const ANIMATION_UE_OPERATION_SET_SCHEMA = "vista-animation-ue-operation-set/v1";

const ANIMATION_UE_PLUGIN_NAME = "VistaAnimationContentApi";
const ANIMATION_UE_PLUGIN_API_SCHEMA = "vista-animation-ue-content-api/v1";
const ANIMATION_UE_CAPABILITY_OPERATION_ID = "vista.animation.capabilities.v1";
const ANIMATION_UE_CAPABILITY_REQUEST_SCHEMA = ANIMATION_UE_CAPABILITY_PROBE_SCHEMA;
const ANIMATION_UE_CAPABILITY_RESPONSE_SCHEMA = ANIMATION_UE_CAPABILITY_SCHEMA;

const NONCE_RE = /^[a-f0-9]{32}$/;
const SHA256_RE = /^[a-f0-9]{64}$/;
const OPAQUE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,159}$/;
const VERSION_RE = /^\d+\.\d+\.\d+(?:-[A-Za-z0-9][A-Za-z0-9.-]{0,63})?$/;
const PLATFORM_RE = /^[a-z0-9][a-z0-9_.-]{0,63}$/;
const MAX_CAPABILITY_RESPONSE_BYTES = 131_072;
const DEFAULT_TIMEOUT_MS = 3_000;
const MAX_TIMEOUT_MS = 10_000;
const MAX_TRACKED_NONCES = 4_096;

const EXPECTED_PLUGIN_SOURCE_FILES = Object.freeze([
  "Plugins/VistaAnimationContentApi/VistaAnimationContentApi.uplugin",
  "Plugins/VistaAnimationContentApi/Source/VistaAnimationContentApi/VistaAnimationContentApi.Build.cs",
  "Plugins/VistaAnimationContentApi/Source/VistaAnimationContentApi/Public/VistaAnimationContentApiModule.h",
  "Plugins/VistaAnimationContentApi/Source/VistaAnimationContentApi/Private/VistaAnimationContentApiModule.cpp",
]);

const SECURITY_POLICY = deepFreeze({
  schema: ANIMATION_UE_SECURITY_POLICY_SCHEMA,
  json_only: true,
  fixed_operation_allowlist: true,
  nonce_echo_required: true,
  request_digest_echo_required: true,
  slot_binding_required: true,
  mutation_max_attempts: 1,
  caller_python: false,
  caller_console: false,
  caller_script: false,
  caller_asset_paths: false,
});

function buildOperationSet() {
  const operations = Object.values(ANIMATION_UE_OPERATION_ALLOWLIST)
    .map((entry) => ({
      operation_id: entry.operation_id,
      operation_fingerprint: entry.operation_fingerprint,
      request_schema: entry.request_schema,
      response_schema: entry.response_schema,
      mutation: entry.mutation,
      max_attempts: entry.max_attempts,
    }))
    .sort((left, right) => (
      left.operation_id < right.operation_id ? -1 : (left.operation_id > right.operation_id ? 1 : 0)
    ));
  const descriptor = {
    schema: ANIMATION_UE_OPERATION_SET_SCHEMA,
    operations,
  };
  return deepFreeze({
    operations,
    operation_allowlist_digest: digest(descriptor),
  });
}

const OPERATION_SET = buildOperationSet();
const ANIMATION_UE_CAPABILITY_OPERATION_FINGERPRINT = digest({
  schema: ANIMATION_UE_CAPABILITY_PROBE_SCHEMA,
  operation_id: ANIMATION_UE_CAPABILITY_OPERATION_ID,
  request_schema: ANIMATION_UE_CAPABILITY_REQUEST_SCHEMA,
  response_schema: ANIMATION_UE_CAPABILITY_RESPONSE_SCHEMA,
  mutation: false,
  max_attempts: 1,
});

const PUBLIC_CAUSES = Object.freeze({
  ANIMATION_UE_PLUGIN_TRANSPORT_MISSING: {
    message: "A trusted VISTA animation UE plugin transport is not configured.",
    retryable: false,
  },
  ANIMATION_UE_PLUGIN_PROBE_UNAVAILABLE: {
    message: "The trusted VISTA animation UE plugin capability probe is unavailable.",
    retryable: true,
  },
  ANIMATION_UE_PLUGIN_PROBE_TIMEOUT: {
    message: "The trusted VISTA animation UE plugin capability probe timed out.",
    retryable: true,
  },
  ANIMATION_UE_PLUGIN_PROBE_ABORTED: {
    message: "The trusted VISTA animation UE plugin capability probe was cancelled.",
    retryable: true,
  },
  ANIMATION_UE_PLUGIN_NONCE_INVALID: {
    message: "The animation UE plugin liveness challenge could not be created safely.",
    retryable: false,
  },
  ANIMATION_UE_PLUGIN_PROTOCOL_INVALID: {
    message: "The animation UE plugin returned an invalid capability response.",
    retryable: false,
  },
  ANIMATION_UE_PLUGIN_CORRELATION_MISMATCH: {
    message: "The animation UE plugin capability response did not match the live challenge.",
    retryable: false,
  },
  ANIMATION_UE_PLUGIN_ARTIFACT_MISMATCH: {
    message: "The loaded animation UE plugin does not match the pinned plugin artifact.",
    retryable: false,
  },
  ANIMATION_UE_PLUGIN_SLOT_MISMATCH: {
    message: "The animation UE plugin is not bound to the expected Studio owner, session, slot, and scene.",
    retryable: false,
  },
  ANIMATION_UE_PLUGIN_CONTENT_MISMATCH: {
    message: "The animation UE plugin does not have the pinned verified animation content profile.",
    retryable: false,
  },
  ANIMATION_UE_PLUGIN_SECURITY_POLICY_MISMATCH: {
    message: "The animation UE plugin does not enforce the fixed JSON-only security policy.",
    retryable: false,
  },
  ANIMATION_UE_PLUGIN_OPERATION_MISMATCH: {
    message: "The animation UE plugin operation allowlist does not match the server contract.",
    retryable: false,
  },
});

class VistaAnimationUeReadinessError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "VistaAnimationUeReadinessError";
    this.code = code;
  }
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function fail(code, message) {
  throw new VistaAnimationUeReadinessError(code, message);
}

function validateShape(value, allowed, required, pointer, code = "ANIMATION_UE_PLUGIN_PROTOCOL_INVALID") {
  if (!isPlainObject(value)) fail(code, `${pointer} must be an object`);
  const actual = Object.keys(value);
  const unknown = actual.filter((key) => !allowed.includes(key));
  const missing = required.filter((key) => !Object.prototype.hasOwnProperty.call(value, key));
  if (unknown.length > 0 || missing.length > 0) fail(code, `${pointer} has an invalid shape`);
}

function requireString(value, pointer, {
  pattern = OPAQUE_ID_RE,
  max = 160,
  code = "ANIMATION_UE_PLUGIN_PROTOCOL_INVALID",
} = {}) {
  if (typeof value !== "string" || value.length === 0 || value.length > max || (pattern && !pattern.test(value))) {
    fail(code, `${pointer} is invalid`);
  }
  return value;
}

function defaultNonceFactory() {
  return crypto.randomBytes(16).toString("hex");
}

function normalizePluginArtifact(value, code = "ANIMATION_UE_PLUGIN_CONFIG_INVALID") {
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
  validateShape(value, keys, keys, "plugin artifact", code);
  if (
    value.schema !== ANIMATION_UE_PLUGIN_ARTIFACT_SCHEMA
    || value.plugin_name !== ANIMATION_UE_PLUGIN_NAME
    || value.api_schema !== ANIMATION_UE_PLUGIN_API_SCHEMA
  ) fail(code, "plugin artifact identity is invalid");
  return deepFreeze({
    schema: ANIMATION_UE_PLUGIN_ARTIFACT_SCHEMA,
    plugin_name: ANIMATION_UE_PLUGIN_NAME,
    plugin_version: requireString(value.plugin_version, "plugin artifact.plugin_version", {
      pattern: VERSION_RE,
      max: 80,
      code,
    }),
    plugin_build_id: requireString(value.plugin_build_id, "plugin artifact.plugin_build_id", { code }),
    binary_sha256: requireString(value.binary_sha256, "plugin artifact.binary_sha256", {
      pattern: SHA256_RE,
      max: 64,
      code,
    }),
    engine_version: requireString(value.engine_version, "plugin artifact.engine_version", { code }),
    target_platform: requireString(value.target_platform, "plugin artifact.target_platform", {
      pattern: PLATFORM_RE,
      max: 64,
      code,
    }),
    api_schema: ANIMATION_UE_PLUGIN_API_SCHEMA,
  });
}

function normalizeSlotClaim(value) {
  const keys = ["owner_id", "session_id", "slot_id", "scene_revision"];
  validateShape(value, keys, keys, "slot binding", "ANIMATION_UE_PLUGIN_CONFIG_INVALID");
  return deepFreeze({
    owner_id: requireString(value.owner_id, "slot binding.owner_id", {
      code: "ANIMATION_UE_PLUGIN_CONFIG_INVALID",
    }),
    session_id: requireString(value.session_id, "slot binding.session_id", {
      code: "ANIMATION_UE_PLUGIN_CONFIG_INVALID",
    }),
    slot_id: requireString(value.slot_id, "slot binding.slot_id", {
      code: "ANIMATION_UE_PLUGIN_CONFIG_INVALID",
    }),
    scene_revision: requireString(value.scene_revision, "slot binding.scene_revision", {
      code: "ANIMATION_UE_PLUGIN_CONFIG_INVALID",
    }),
  });
}

function createSlotBinding(slotClaim) {
  const identity = {
    schema: ANIMATION_UE_SLOT_BINDING_SCHEMA,
    ...slotClaim,
  };
  return deepFreeze({ ...identity, binding_digest: digest(identity) });
}

function createContentProof(contentProfile) {
  return deepFreeze({
    schema: ANIMATION_UE_CONTENT_PROOF_SCHEMA,
    profile_id: contentProfile.profile_id,
    profile_revision: contentProfile.revision,
    content_revision: contentProfile.content_revision,
    content_digest: contentProfile.content_digest,
    verification_receipt_id: contentProfile.verification.receipt_id,
  });
}

function sameCanonical(left, right) {
  return canonicalize(left) === canonicalize(right);
}

function validateCapabilityResponse(raw, expected) {
  if (typeof raw !== "string" || Buffer.byteLength(raw, "utf8") > MAX_CAPABILITY_RESPONSE_BYTES) {
    fail("ANIMATION_UE_PLUGIN_PROTOCOL_INVALID", "capability response must be bounded JSON text");
  }
  let value;
  try {
    value = JSON.parse(raw);
  } catch {
    fail("ANIMATION_UE_PLUGIN_PROTOCOL_INVALID", "capability response must be JSON");
  }
  const keys = [
    "schema",
    "status",
    "operation_id",
    "operation_fingerprint",
    "nonce_marker",
    "challenge_digest",
    "slot_binding",
    "plugin_artifact",
    "process_instance_id",
    "content_proof",
    "security",
    "operation_allowlist_digest",
    "operations",
  ];
  validateShape(value, keys, keys, "capability response");
  if (
    value.schema !== ANIMATION_UE_CAPABILITY_SCHEMA
    || value.status !== "ready"
    || value.operation_id !== ANIMATION_UE_CAPABILITY_OPERATION_ID
    || value.operation_fingerprint !== ANIMATION_UE_CAPABILITY_OPERATION_FINGERPRINT
  ) fail("ANIMATION_UE_PLUGIN_PROTOCOL_INVALID", "capability response identity is invalid");

  validateShape(value.nonce_marker, ["schema", "nonce"], ["schema", "nonce"], "capability response.nonce_marker");
  if (
    value.nonce_marker.schema !== ANIMATION_UE_MARKER_SCHEMA
    || value.nonce_marker.nonce !== expected.nonce
    || value.challenge_digest !== expected.challengeDigest
  ) fail("ANIMATION_UE_PLUGIN_CORRELATION_MISMATCH", "capability response challenge is stale");

  if (!sameCanonical(value.slot_binding, expected.slotBinding)) {
    fail("ANIMATION_UE_PLUGIN_SLOT_MISMATCH", "capability response slot binding is invalid");
  }
  const artifact = normalizePluginArtifact(value.plugin_artifact, "ANIMATION_UE_PLUGIN_ARTIFACT_MISMATCH");
  if (!sameCanonical(artifact, expected.pluginArtifact)) {
    fail("ANIMATION_UE_PLUGIN_ARTIFACT_MISMATCH", "capability response plugin artifact is invalid");
  }
  const processInstanceId = requireString(value.process_instance_id, "capability response.process_instance_id");
  if (!sameCanonical(value.content_proof, expected.contentProof)) {
    fail("ANIMATION_UE_PLUGIN_CONTENT_MISMATCH", "capability response content proof is invalid");
  }
  if (!sameCanonical(value.security, SECURITY_POLICY)) {
    fail("ANIMATION_UE_PLUGIN_SECURITY_POLICY_MISMATCH", "capability response security policy is invalid");
  }
  if (
    value.operation_allowlist_digest !== OPERATION_SET.operation_allowlist_digest
    || !sameCanonical(value.operations, OPERATION_SET.operations)
  ) fail("ANIMATION_UE_PLUGIN_OPERATION_MISMATCH", "capability response operation allowlist is invalid");

  return deepFreeze({
    process_instance_id: processInstanceId,
    plugin_artifact: artifact,
    slot_binding: expected.slotBinding,
    content_proof: expected.contentProof,
    operation_allowlist_digest: OPERATION_SET.operation_allowlist_digest,
  });
}

function publicCause(code) {
  const known = PUBLIC_CAUSES[code] || PUBLIC_CAUSES.ANIMATION_UE_PLUGIN_PROBE_UNAVAILABLE;
  return {
    code: PUBLIC_CAUSES[code] ? code : "ANIMATION_UE_PLUGIN_PROBE_UNAVAILABLE",
    message: known.message,
    retryable: known.retryable,
    dependency: "vista_animation_ue_plugin",
  };
}

function notReady(code, revision) {
  return {
    status: "not_ready",
    revision,
    causes: [publicCause(code)],
  };
}

function errorCode(error, signal) {
  if (
    (signal && signal.aborted)
    || (error && (error.name === "AbortError" || error.code === "ABORT_ERR"))
  ) return "ANIMATION_UE_PLUGIN_PROBE_ABORTED";
  if (error instanceof VistaAnimationUeReadinessError && PUBLIC_CAUSES[error.code]) return error.code;
  return "ANIMATION_UE_PLUGIN_PROBE_UNAVAILABLE";
}

function makeRevision(pluginArtifact, contentProof, slotBinding, extra = {}) {
  return deepFreeze({
    plugin_name: pluginArtifact.plugin_name,
    plugin_version: pluginArtifact.plugin_version,
    plugin_build_id: pluginArtifact.plugin_build_id,
    binary_sha256: pluginArtifact.binary_sha256,
    engine_version: pluginArtifact.engine_version,
    target_platform: pluginArtifact.target_platform,
    api_schema: pluginArtifact.api_schema,
    profile_id: contentProof.profile_id,
    profile_revision: contentProof.profile_revision,
    content_revision: contentProof.content_revision,
    content_digest: contentProof.content_digest,
    slot_binding_digest: slotBinding.binding_digest,
    operation_allowlist_digest: OPERATION_SET.operation_allowlist_digest,
    ...extra,
  });
}

function normalizeClock(clock) {
  let value;
  try {
    value = clock();
  } catch {
    fail("ANIMATION_UE_PLUGIN_CONFIG_INVALID", "clock failed");
  }
  const milliseconds = value instanceof Date ? value.getTime() : Number(value);
  if (!Number.isFinite(milliseconds)) fail("ANIMATION_UE_PLUGIN_CONFIG_INVALID", "clock is invalid");
  try {
    return new Date(milliseconds).toISOString();
  } catch {
    fail("ANIMATION_UE_PLUGIN_CONFIG_INVALID", "clock is invalid");
  }
}

function nextUniqueNonce(nonceFactory, usedNonces, nonceOrder) {
  let nonce;
  try {
    nonce = nonceFactory();
  } catch {
    fail("ANIMATION_UE_PLUGIN_NONCE_INVALID", "nonce source failed");
  }
  if (typeof nonce !== "string" || !NONCE_RE.test(nonce) || usedNonces.has(nonce)) {
    fail("ANIMATION_UE_PLUGIN_NONCE_INVALID", "nonce source failed policy");
  }
  usedNonces.add(nonce);
  nonceOrder.push(nonce);
  if (nonceOrder.length > MAX_TRACKED_NONCES) usedNonces.delete(nonceOrder.shift());
  return nonce;
}

function createVistaAnimationUeReadinessProbe(options = {}) {
  validateShape(
    options,
    ["transport", "expectedArtifact", "contentProfile", "slotBinding", "nonceFactory", "clock", "timeoutMs"],
    ["expectedArtifact", "contentProfile", "slotBinding"],
    "readiness options",
    "ANIMATION_UE_PLUGIN_CONFIG_INVALID",
  );
  const pluginArtifact = normalizePluginArtifact(options.expectedArtifact);
  let contentProfile;
  try {
    contentProfile = validateContentProfile(options.contentProfile);
  } catch {
    fail("ANIMATION_UE_PLUGIN_CONFIG_INVALID", "a verified animation content profile is required");
  }
  const contentProof = createContentProof(contentProfile);
  const slotBinding = createSlotBinding(normalizeSlotClaim(options.slotBinding));
  const nonceFactory = options.nonceFactory === undefined ? defaultNonceFactory : options.nonceFactory;
  const clock = options.clock === undefined ? Date.now : options.clock;
  const timeoutMs = options.timeoutMs === undefined ? DEFAULT_TIMEOUT_MS : Number(options.timeoutMs);
  if (typeof nonceFactory !== "function" || typeof clock !== "function") {
    fail("ANIMATION_UE_PLUGIN_CONFIG_INVALID", "nonceFactory and clock must be functions");
  }
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > MAX_TIMEOUT_MS) {
    fail("ANIMATION_UE_PLUGIN_CONFIG_INVALID", "timeoutMs is outside the supported range");
  }
  const transport = options.transport;
  const transportAvailable = Boolean(transport && typeof transport.probeAnimationContentApi === "function");
  const revision = makeRevision(pluginArtifact, contentProof, slotBinding);
  const usedNonces = new Set();
  const nonceOrder = [];

  return async function probeVistaAnimationUe({ signal } = {}) {
    if (signal !== undefined && (!signal || typeof signal.aborted !== "boolean")) {
      return notReady("ANIMATION_UE_PLUGIN_PROBE_UNAVAILABLE", revision);
    }
    if (!transportAvailable) return notReady("ANIMATION_UE_PLUGIN_TRANSPORT_MISSING", revision);
    if (signal && signal.aborted) return notReady("ANIMATION_UE_PLUGIN_PROBE_ABORTED", revision);

    let nonce;
    let checkedAt;
    try {
      nonce = nextUniqueNonce(nonceFactory, usedNonces, nonceOrder);
      checkedAt = normalizeClock(clock);
    } catch (error) {
      return notReady(errorCode(error, signal), revision);
    }
    const challenge = {
      schema: ANIMATION_UE_CAPABILITY_PROBE_SCHEMA,
      operation_id: ANIMATION_UE_CAPABILITY_OPERATION_ID,
      operation_fingerprint: ANIMATION_UE_CAPABILITY_OPERATION_FINGERPRINT,
      nonce_marker: { schema: ANIMATION_UE_MARKER_SCHEMA, nonce },
      slot_binding: slotBinding,
      content_proof: contentProof,
      operation_allowlist_digest: OPERATION_SET.operation_allowlist_digest,
    };
    const challengeDigest = digest(challenge);
    const request = deepFreeze({
      ...challenge,
      challenge_digest: challengeDigest,
    });

    const controller = new AbortController();
    let rejectControl;
    const controlPromise = new Promise((_resolve, reject) => {
      rejectControl = reject;
    });
    const abortFromParent = () => {
      controller.abort();
      rejectControl(new VistaAnimationUeReadinessError(
        "ANIMATION_UE_PLUGIN_PROBE_ABORTED",
        "capability probe was aborted",
      ));
    };
    if (signal) signal.addEventListener("abort", abortFromParent, { once: true });
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
      rejectControl(new VistaAnimationUeReadinessError(
        "ANIMATION_UE_PLUGIN_PROBE_TIMEOUT",
        "capability probe timed out",
      ));
    }, timeoutMs);

    try {
      const brokerOptions = Object.freeze({
        operationId: ANIMATION_UE_CAPABILITY_OPERATION_ID,
        operationFingerprint: ANIMATION_UE_CAPABILITY_OPERATION_FINGERPRINT,
        challengeDigest,
        slotBindingDigest: slotBinding.binding_digest,
        mutation: false,
        maxAttempts: 1,
        timeoutMs,
        queueDeadlineMs: timeoutMs,
        signal: controller.signal,
      });
      const transportPromise = Promise.resolve().then(
        () => transport.probeAnimationContentApi(JSON.stringify(request), brokerOptions),
      );
      const raw = await Promise.race([transportPromise, controlPromise]);
      if (timedOut) return notReady("ANIMATION_UE_PLUGIN_PROBE_TIMEOUT", revision);
      if (signal && signal.aborted) return notReady("ANIMATION_UE_PLUGIN_PROBE_ABORTED", revision);
      const capability = validateCapabilityResponse(raw, {
        nonce,
        challengeDigest,
        slotBinding,
        pluginArtifact,
        contentProof,
      });
      return {
        status: "ready",
        revision: makeRevision(pluginArtifact, contentProof, slotBinding, {
          process_instance_id: capability.process_instance_id,
          checked_at: checkedAt,
          verification: "live_plugin_challenge",
        }),
        causes: [],
      };
    } catch (error) {
      if (timedOut) return notReady("ANIMATION_UE_PLUGIN_PROBE_TIMEOUT", revision);
      return notReady(errorCode(error, signal), revision);
    } finally {
      clearTimeout(timer);
      if (signal) signal.removeEventListener("abort", abortFromParent);
    }
  };
}

function inspectVistaAnimationUePluginSource(projectRoot, { fsImpl = fs } = {}) {
  if (typeof projectRoot !== "string" || projectRoot.length === 0 || !path.isAbsolute(projectRoot)) {
    throw new TypeError("projectRoot must be an absolute path");
  }
  if (!fsImpl || typeof fsImpl.lstatSync !== "function") throw new TypeError("fsImpl.lstatSync is required");
  const presentFiles = [];
  const missingFiles = [];
  for (const relativePath of EXPECTED_PLUGIN_SOURCE_FILES) {
    let present = false;
    try {
      const status = fsImpl.lstatSync(path.resolve(projectRoot, ...relativePath.split("/")));
      present = status.isFile() && !status.isSymbolicLink();
    } catch {
      present = false;
    }
    (present ? presentFiles : missingFiles).push(relativePath);
  }
  return deepFreeze({
    schema: ANIMATION_UE_SOURCE_AUDIT_SCHEMA,
    plugin_name: ANIMATION_UE_PLUGIN_NAME,
    source_tree_complete: missingFiles.length === 0,
    expected_files: [...EXPECTED_PLUGIN_SOURCE_FILES],
    present_files: presentFiles,
    missing_files: missingFiles,
  });
}

module.exports = {
  ANIMATION_UE_CAPABILITY_OPERATION_FINGERPRINT,
  ANIMATION_UE_CAPABILITY_OPERATION_ID,
  ANIMATION_UE_CAPABILITY_PROBE_SCHEMA,
  ANIMATION_UE_CAPABILITY_REQUEST_SCHEMA,
  ANIMATION_UE_CAPABILITY_RESPONSE_SCHEMA,
  ANIMATION_UE_CAPABILITY_SCHEMA,
  ANIMATION_UE_OPERATION_SET_SCHEMA,
  ANIMATION_UE_PLUGIN_API_SCHEMA,
  ANIMATION_UE_PLUGIN_ARTIFACT_SCHEMA,
  ANIMATION_UE_PLUGIN_NAME,
  ANIMATION_UE_SECURITY_POLICY_SCHEMA,
  ANIMATION_UE_SLOT_BINDING_SCHEMA,
  ANIMATION_UE_SOURCE_AUDIT_SCHEMA,
  EXPECTED_PLUGIN_SOURCE_FILES,
  OPERATION_SET,
  SECURITY_POLICY,
  VistaAnimationUeReadinessError,
  createVistaAnimationUeReadinessProbe,
  inspectVistaAnimationUePluginSource,
  validateCapabilityResponse,
};
