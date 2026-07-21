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
const MAX_PLUGIN_SOURCE_FILE_BYTES = 1_048_576;
const SOURCE_AUDIT_READ_CHUNK_BYTES = 64 * 1024;
const PLUGIN_SOURCE_RELATIVE_ROOT = "Plugins/VistaAnimationContentApi";

const EXPECTED_PLUGIN_SOURCE_MANIFEST = deepFreeze([
  {
    path: `${PLUGIN_SOURCE_RELATIVE_ROOT}/VistaAnimationContentApi.uplugin`,
    sha256: "bc9fc7c0f227722221e709e65b91dc2cbdef8c21c91b4a9df775ad5766c965af",
  },
  {
    path: `${PLUGIN_SOURCE_RELATIVE_ROOT}/Config/FilterPlugin.ini`,
    sha256: "5bb06a2a79c30f12f891befbd34294914b4bdf1b634577d03c1c14c251b56b72",
  },
  {
    path: `${PLUGIN_SOURCE_RELATIVE_ROOT}/ContentProfiles/vista-mmg040-project-profile-source-v1.json`,
    sha256: "1b0aa6e48d251cb8dbeac4f34528ca8fa6084fb330fc2d150ef341f630528b1c",
  },
  {
    path: `${PLUGIN_SOURCE_RELATIVE_ROOT}/Contract/vista-animation-content-api-v1.json`,
    sha256: "b43d7ea45ad5cb8ff8bf645e52fb8a155462c71b47d8f625d45529a61400bd2e",
  },
  {
    path: `${PLUGIN_SOURCE_RELATIVE_ROOT}/Contract/vista-animation-content-inspection-receipt-v1.schema.json`,
    sha256: "919ba41b8effd621b88844be7a786cc2595627f14e8bb38f69d3e8279e11b463",
  },
  {
    path: `${PLUGIN_SOURCE_RELATIVE_ROOT}/Contract/vista-animation-project-profile-source-v1.schema.json`,
    sha256: "0c875748d29d76b8a7eaae1e6196a445631faee69a337b4b3567753dc0b5365c",
  },
  {
    path: `${PLUGIN_SOURCE_RELATIVE_ROOT}/Source/VistaAnimationContentApi/VistaAnimationContentApi.Build.cs`,
    sha256: "54d899c87f5121bacbb0ac18b29f320855ce564e54ee286e6c340f3aec7a91ed",
  },
  {
    path: `${PLUGIN_SOURCE_RELATIVE_ROOT}/Source/VistaAnimationContentApi/Public/VistaAnimationContentApiModule.h`,
    sha256: "3569a537793faec46bb3240ac53575b30e73d9f212ac05f7bbd90a57311b9535",
  },
  {
    path: `${PLUGIN_SOURCE_RELATIVE_ROOT}/Source/VistaAnimationContentApi/Private/VistaAnimationContentApiModule.cpp`,
    sha256: "fd27a21e49eea87bbfdf7bc21d6f9ed14f4cae074ad9e69fb749e4859d71e623",
  },
  {
    path: `${PLUGIN_SOURCE_RELATIVE_ROOT}/Source/VistaAnimationContentApi/Public/VistaAnimationContentApiSubsystem.h`,
    sha256: "ddadedfb967a397f04416295eb40c689d964414e22ba40f39746d83fff289e77",
  },
  {
    path: `${PLUGIN_SOURCE_RELATIVE_ROOT}/Source/VistaAnimationContentApi/Private/VistaAnimationContentApiSubsystem.cpp`,
    sha256: "6ecb19ad80ea769712c29bff32924abbf675e4e617d67563ca1681e8506232f5",
  },
  {
    path: `${PLUGIN_SOURCE_RELATIVE_ROOT}/Source/VistaAnimationContentApi/Public/VistaAnimationContentDriver.h`,
    sha256: "92ec98da354baf76e2aa39e6d81e016a65fed73abcd030ef7eb60f6842585b9f",
  },
  {
    path: `${PLUGIN_SOURCE_RELATIVE_ROOT}/Source/VistaAnimationContentApi/Private/VistaAnimationStrictJson.h`,
    sha256: "8259e25e01b156b985744d556e00830b199aec6272e96272658cc1311e524838",
  },
  {
    path: `${PLUGIN_SOURCE_RELATIVE_ROOT}/Source/VistaAnimationContentApi/Private/VistaAnimationStrictJson.cpp`,
    sha256: "104d838750191764b0451ba12f86889867ad732c8c5fef7e72740c130bcd0ca4",
  },
  {
    path: `${PLUGIN_SOURCE_RELATIVE_ROOT}/Source/VistaAnimationContentApi/Public/VistaMmg040ContentDriver.h`,
    sha256: "e88a9d498c75fb21740307a98f2f7f46c8f0f01c6a7a5168ee893be8b99832b3",
  },
  {
    path: `${PLUGIN_SOURCE_RELATIVE_ROOT}/Source/VistaAnimationContentApi/Private/VistaMmg040ContentDriver.cpp`,
    sha256: "c69dd60ca28119858124951d2ae77a3463092902bc845a94f107078867df4302",
  },
]);

const EXPECTED_PLUGIN_SOURCE_FILES = Object.freeze(
  EXPECTED_PLUGIN_SOURCE_MANIFEST.map((entry) => entry.path),
);
const ANIMATION_UE_SOURCE_MANIFEST_SHA256 = digest({
  schema: ANIMATION_UE_SOURCE_AUDIT_SCHEMA,
  plugin_name: ANIMATION_UE_PLUGIN_NAME,
  files: EXPECTED_PLUGIN_SOURCE_MANIFEST,
});

const AUDITED_PLUGIN_DIRECTORY_CHILDREN = deepFreeze({
  [PLUGIN_SOURCE_RELATIVE_ROOT]: [
    "Config",
    "ContentProfiles",
    "Contract",
    "Source",
    "VistaAnimationContentApi.uplugin",
  ],
  [`${PLUGIN_SOURCE_RELATIVE_ROOT}/Config`]: ["FilterPlugin.ini"],
  [`${PLUGIN_SOURCE_RELATIVE_ROOT}/ContentProfiles`]: [
    "vista-mmg040-project-profile-source-v1.json",
  ],
  [`${PLUGIN_SOURCE_RELATIVE_ROOT}/Contract`]: [
    "vista-animation-content-api-v1.json",
    "vista-animation-content-inspection-receipt-v1.schema.json",
    "vista-animation-project-profile-source-v1.schema.json",
  ],
  [`${PLUGIN_SOURCE_RELATIVE_ROOT}/Source`]: ["VistaAnimationContentApi"],
  [`${PLUGIN_SOURCE_RELATIVE_ROOT}/Source/VistaAnimationContentApi`]: [
    "Private",
    "Public",
    "VistaAnimationContentApi.Build.cs",
  ],
  [`${PLUGIN_SOURCE_RELATIVE_ROOT}/Source/VistaAnimationContentApi/Public`]: [
    "VistaAnimationContentApiModule.h",
    "VistaAnimationContentApiSubsystem.h",
    "VistaAnimationContentDriver.h",
    "VistaMmg040ContentDriver.h",
  ],
  [`${PLUGIN_SOURCE_RELATIVE_ROOT}/Source/VistaAnimationContentApi/Private`]: [
    "VistaAnimationContentApiModule.cpp",
    "VistaAnimationContentApiSubsystem.cpp",
    "VistaAnimationStrictJson.cpp",
    "VistaAnimationStrictJson.h",
    "VistaMmg040ContentDriver.cpp",
  ],
});

const OPTIONAL_NONPRODUCTION_PLUGIN_ENTRIES = deepFreeze({
  ".gitignore": "file",
  "README.md": "file",
  Scripts: "directory",
  Tests: "directory",
  Binaries: "directory",
  Intermediate: "directory",
});

const AUDITED_PLUGIN_DIRECTORIES = Object.freeze(
  Object.keys(AUDITED_PLUGIN_DIRECTORY_CHILDREN),
);
const EXPECTED_PLUGIN_SOURCE_FILE_SET = new Set(EXPECTED_PLUGIN_SOURCE_FILES);
const LINUX_DIRECTORY_DESCRIPTOR_ROOT = "/proc/self/fd";

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

class SourceAuditFault extends Error {
  constructor(reason) {
    super(reason);
    this.name = "SourceAuditFault";
    this.reason = reason;
  }
}

function sourceAuditReason(error, fallback = "io_error") {
  if (error instanceof SourceAuditFault) return error.reason;
  if (error && (error.code === "ENOENT" || error.code === "ENOTDIR")) return "missing";
  if (error && (error.code === "EACCES" || error.code === "EPERM")) return "unreadable";
  if (error && error.code === "ELOOP") return "symlink";
  return fallback;
}

function sourceAuditPath(projectRoot, relativePath) {
  const resolved = path.resolve(projectRoot, ...relativePath.split("/"));
  const relative = path.relative(projectRoot, resolved);
  if (
    relative === ".."
    || relative.startsWith(`..${path.sep}`)
    || path.isAbsolute(relative)
  ) throw new SourceAuditFault("outside_project_root");
  return resolved;
}

function absolutePathComponents(absolutePath) {
  const resolved = path.resolve(absolutePath);
  const root = path.parse(resolved).root;
  const tail = path.relative(root, resolved);
  const components = [root];
  let cursor = root;
  for (const component of tail.split(path.sep).filter(Boolean)) {
    cursor = path.join(cursor, component);
    components.push(cursor);
  }
  return components;
}

function statToken(value) {
  return typeof value === "bigint" ? value.toString() : String(value);
}

function hasStatValue(value) {
  return typeof value === "bigint" || Number.isFinite(value);
}

function sameFileIdentity(left, right) {
  return Boolean(
    left
    && right
    && hasStatValue(left.dev)
    && hasStatValue(left.ino)
    && hasStatValue(right.dev)
    && hasStatValue(right.ino)
    && statToken(left.dev) === statToken(right.dev)
    && statToken(left.ino) === statToken(right.ino),
  );
}

function stableStatValue(value) {
  if (typeof value === "bigint") return value.toString();
  return Number.isFinite(value) ? value : null;
}

function sourceEntryType(status) {
  if (
    !status
    || typeof status.isSymbolicLink !== "function"
    || typeof status.isDirectory !== "function"
    || typeof status.isFile !== "function"
  ) throw new SourceAuditFault("invalid_stat");
  if (status.isSymbolicLink()) return "symlink";
  if (status.isDirectory()) return "directory";
  if (status.isFile()) return "file";
  return "other";
}

function sourceEntrySnapshot(status) {
  const type = sourceEntryType(status);
  for (const field of ["dev", "ino", "mode", "nlink", "size", "mtimeMs", "ctimeMs"]) {
    if (!hasStatValue(status[field])) throw new SourceAuditFault("invalid_stat");
  }
  return Object.freeze({
    type,
    dev: statToken(status.dev),
    ino: statToken(status.ino),
    mode: stableStatValue(status.mode),
    nlink: stableStatValue(status.nlink),
    size: stableStatValue(status.size),
    mtime_ms: stableStatValue(status.mtimeMs),
    ctime_ms: stableStatValue(status.ctimeMs),
  });
}

function sameSourceEntrySnapshot(left, right) {
  return Boolean(left && right && Object.keys(left).every((key) => left[key] === right[key]));
}

function sameDirectoryIdentity(left, right) {
  return Boolean(
    left
    && right
    && left.dev === right.dev
    && left.ino === right.ino
    && left.mode === right.mode
    && left.type === "directory"
    && right.type === "directory",
  );
}

function sameDirectorySnapshot(left, right) {
  return sameDirectoryIdentity(left, right) && sameSourceEntrySnapshot(left, right);
}

function directorySnapshot(status) {
  const snapshot = sourceEntrySnapshot(status);
  if (snapshot.type !== "directory") throw new SourceAuditFault("not_directory");
  return snapshot;
}

function inspectCanonicalDirectory(fsImpl, directory) {
  const resolvedDirectory = path.resolve(directory);
  let status;
  try {
    status = fsImpl.lstatSync(resolvedDirectory);
  } catch (error) {
    throw new SourceAuditFault(sourceAuditReason(error));
  }
  if (sourceEntryType(status) === "symlink") {
    throw new SourceAuditFault("ancestor_symlink");
  }
  if (sourceEntryType(status) !== "directory") {
    throw new SourceAuditFault("not_directory");
  }
  let canonical;
  try {
    canonical = fsImpl.realpathSync(resolvedDirectory);
  } catch (error) {
    throw new SourceAuditFault(sourceAuditReason(error));
  }
  if (path.resolve(String(canonical)) !== resolvedDirectory) {
    throw new SourceAuditFault("ancestor_symlink");
  }
  const snapshot = directorySnapshot(status);
  return snapshot;
}

function inspectCanonicalDirectoryChain(fsImpl, directory) {
  return captureCanonicalDirectoryChain(fsImpl, directory).at(-1).snapshot;
}

function captureCanonicalDirectoryChain(fsImpl, directory) {
  return absolutePathComponents(directory).map((component) => ({
    path: component,
    snapshot: inspectCanonicalDirectory(fsImpl, component),
  }));
}

function sameDirectoryChain(left, right) {
  return left.length === right.length && left.every((entry, index) => (
    entry.path === right[index].path
    && sameDirectoryIdentity(entry.snapshot, right[index].snapshot)
  ));
}

function numericStatSize(status) {
  const value = typeof status.size === "bigint" ? Number(status.size) : status.size;
  if (!Number.isSafeInteger(value) || value < 0) throw new SourceAuditFault("invalid_size");
  return value;
}

function requireRegularSingleLink(status, { opened = false } = {}) {
  if (!opened && (typeof status.isSymbolicLink !== "function" || status.isSymbolicLink())) {
    throw new SourceAuditFault("symlink");
  }
  if (typeof status.isFile !== "function" || !status.isFile()) {
    throw new SourceAuditFault("not_regular_file");
  }
  for (const field of ["dev", "ino", "mode", "nlink", "size", "mtimeMs", "ctimeMs"]) {
    if (!hasStatValue(status[field])) throw new SourceAuditFault("invalid_stat");
  }
  if (Number(status.nlink) !== 1) throw new SourceAuditFault("hardlink");
  const size = numericStatSize(status);
  if (size > MAX_PLUGIN_SOURCE_FILE_BYTES) throw new SourceAuditFault("oversize");
  return size;
}

function sameStableFile(left, right) {
  return sameFileIdentity(left, right)
    && stableStatValue(left.mode) === stableStatValue(right.mode)
    && stableStatValue(left.nlink) === stableStatValue(right.nlink)
    && stableStatValue(left.size) === stableStatValue(right.size)
    && stableStatValue(left.mtimeMs) === stableStatValue(right.mtimeMs)
    && stableStatValue(left.ctimeMs) === stableStatValue(right.ctimeMs);
}

function hashOpenedSourceFile(fsImpl, descriptor, filepath, lexicalStatus) {
  const openedBefore = fsImpl.fstatSync(descriptor);
  const expectedSize = requireRegularSingleLink(openedBefore, { opened: true });
  if (!sameFileIdentity(lexicalStatus, openedBefore)) {
    throw new SourceAuditFault("identity_changed");
  }
  const hasher = crypto.createHash("sha256");
  const chunk = Buffer.allocUnsafe(SOURCE_AUDIT_READ_CHUNK_BYTES);
  let position = 0;
  while (position <= MAX_PLUGIN_SOURCE_FILE_BYTES) {
    const remaining = MAX_PLUGIN_SOURCE_FILE_BYTES - position + 1;
    const requested = Math.min(chunk.length, remaining);
    const bytesRead = fsImpl.readSync(descriptor, chunk, 0, requested, position);
    if (!Number.isSafeInteger(bytesRead) || bytesRead < 0 || bytesRead > requested) {
      throw new SourceAuditFault("io_error");
    }
    if (bytesRead === 0) break;
    hasher.update(chunk.subarray(0, bytesRead));
    position += bytesRead;
  }
  if (position > MAX_PLUGIN_SOURCE_FILE_BYTES) throw new SourceAuditFault("oversize");

  const openedAfter = fsImpl.fstatSync(descriptor);
  requireRegularSingleLink(openedAfter, { opened: true });
  if (
    position !== expectedSize
    || !sameStableFile(openedBefore, openedAfter)
  ) throw new SourceAuditFault("identity_changed");

  const lexicalAfter = fsImpl.lstatSync(filepath);
  requireRegularSingleLink(lexicalAfter);
  if (!sameStableFile(openedAfter, lexicalAfter)) {
    throw new SourceAuditFault("identity_changed");
  }
  return hasher.digest("hex");
}

function addPolicyViolation(policyViolations, relativePath, reason) {
  const key = `${relativePath}\u0000${reason}`;
  if (!policyViolations.keys.has(key)) {
    policyViolations.keys.add(key);
    policyViolations.values.push({ path: relativePath, reason });
  }
}

function expectedDirectoryChildTypes(relativeDirectory) {
  const expectedTypes = new Map();
  for (const name of AUDITED_PLUGIN_DIRECTORY_CHILDREN[relativeDirectory]) {
    const relativePath = `${relativeDirectory}/${name}`;
    if (Object.prototype.hasOwnProperty.call(AUDITED_PLUGIN_DIRECTORY_CHILDREN, relativePath)) {
      expectedTypes.set(name, "directory");
    } else if (EXPECTED_PLUGIN_SOURCE_FILE_SET.has(relativePath)) {
      expectedTypes.set(name, "file");
    } else {
      throw new SourceAuditFault("invalid_manifest");
    }
  }
  if (relativeDirectory === PLUGIN_SOURCE_RELATIVE_ROOT) {
    for (const [name, expectedType] of Object.entries(OPTIONAL_NONPRODUCTION_PLUGIN_ENTRIES)) {
      expectedTypes.set(name, expectedType);
    }
  }
  return expectedTypes;
}

function sanitizedUnexpectedEntry(relativeDirectory, name) {
  const safeName = (
    name !== "."
    && name !== ".."
    && /^[A-Za-z0-9._-]{1,160}$/.test(name)
  ) ? name : "[invalid-entry]";
  return `${relativeDirectory}/${safeName}`;
}

function validateOptionalEntry(fsImpl, projectRoot, relativePath, expectedType, status) {
  if (expectedType === "directory") {
    if (sourceEntryType(status) === "symlink") throw new SourceAuditFault("ancestor_symlink");
    if (sourceEntryType(status) !== "directory") throw new SourceAuditFault("not_directory");
    inspectCanonicalDirectoryChain(fsImpl, sourceAuditPath(projectRoot, relativePath));
    return;
  }
  requireRegularSingleLink(status);
}

function captureAuditedPluginTreeSnapshot(fsImpl, projectRoot) {
  const unexpectedEntries = new Set();
  const allowedEntries = [];
  const directories = new Map();
  const directoryFailures = new Map();
  const policyViolations = { keys: new Set(), values: [] };

  for (const relativeDirectory of AUDITED_PLUGIN_DIRECTORIES) {
    let directory;
    let snapshot;
    try {
      directory = sourceAuditPath(projectRoot, relativeDirectory);
      snapshot = inspectCanonicalDirectoryChain(fsImpl, directory);
    } catch (error) {
      const reason = sourceAuditReason(error);
      directoryFailures.set(relativeDirectory, reason);
      if (reason !== "missing") addPolicyViolation(policyViolations, relativeDirectory, reason);
      continue;
    }
    const capturedDirectory = {
      snapshot,
      actualChildren: [],
      children: new Map(),
      childFailures: new Map(),
      readFailure: null,
    };
    directories.set(relativeDirectory, capturedDirectory);

    let actualChildren;
    try {
      actualChildren = fsImpl.readdirSync(directory).map(String).sort();
    } catch (error) {
      const reason = sourceAuditReason(error);
      capturedDirectory.readFailure = reason;
      addPolicyViolation(policyViolations, relativeDirectory, reason);
      continue;
    }
    capturedDirectory.actualChildren = actualChildren;
    const expectedTypes = expectedDirectoryChildTypes(relativeDirectory);
    for (const name of actualChildren) {
      if (!expectedTypes.has(name)) {
        unexpectedEntries.add(sanitizedUnexpectedEntry(relativeDirectory, name));
      }
    }
    for (const [name, expectedType] of expectedTypes) {
      if (!actualChildren.includes(name)) continue;
      const relativePath = `${relativeDirectory}/${name}`;
      let status;
      try {
        status = fsImpl.lstatSync(sourceAuditPath(projectRoot, relativePath));
        capturedDirectory.children.set(name, {
          snapshot: sourceEntrySnapshot(status),
          status,
        });
      } catch (error) {
        const reason = sourceAuditReason(error);
        capturedDirectory.childFailures.set(name, reason);
        if (
          expectedType === "directory"
          || Object.prototype.hasOwnProperty.call(OPTIONAL_NONPRODUCTION_PLUGIN_ENTRIES, name)
        ) {
          addPolicyViolation(policyViolations, relativePath, reason);
        }
        continue;
      }
      if (
        relativeDirectory === PLUGIN_SOURCE_RELATIVE_ROOT
        && Object.prototype.hasOwnProperty.call(OPTIONAL_NONPRODUCTION_PLUGIN_ENTRIES, name)
      ) {
        try {
          validateOptionalEntry(fsImpl, projectRoot, relativePath, expectedType, status);
          allowedEntries.push(relativePath);
        } catch (error) {
          addPolicyViolation(policyViolations, relativePath, sourceAuditReason(error));
        }
      }
    }
  }

  return {
    directories,
    directoryFailures,
    unexpectedEntries: [...unexpectedEntries].sort(),
    allowedEntries: allowedEntries.sort(),
    policyViolations: policyViolations.values.sort((left, right) => (
      left.path.localeCompare(right.path) || left.reason.localeCompare(right.reason)
    )),
  };
}

function assertAnchoredTraversalPlatform() {
  const requiredConstants = [
    fs.constants.O_RDONLY,
    fs.constants.O_DIRECTORY,
    fs.constants.O_NOFOLLOW,
    fs.constants.O_NONBLOCK,
  ];
  if (process.platform !== "linux" || requiredConstants.some((value) => !Number.isInteger(value))) {
    throw new SourceAuditFault("platform_unsupported");
  }
}

function closeHeldDirectoryHandles(fsImpl, handles) {
  let failed = false;
  for (const handle of [...handles.values()].reverse()) {
    try {
      fsImpl.closeSync(handle.descriptor);
    } catch {
      failed = true;
    }
  }
  handles.clear();
  return failed;
}

function openHeldDirectoryHandles(fsImpl, projectRoot, treeSnapshot) {
  const handles = new Map();
  const flags = fs.constants.O_RDONLY | fs.constants.O_DIRECTORY | fs.constants.O_NOFOLLOW;
  try {
    for (const relativeDirectory of AUDITED_PLUGIN_DIRECTORIES) {
      const captured = treeSnapshot.directories.get(relativeDirectory);
      if (!captured) continue;
      const descriptor = fsImpl.openSync(
        sourceAuditPath(projectRoot, relativeDirectory),
        flags,
      );
      const anchorPath = `${LINUX_DIRECTORY_DESCRIPTOR_ROOT}/${descriptor}`;
      handles.set(relativeDirectory, { anchorPath, descriptor });
      const openedSnapshot = directorySnapshot(fsImpl.fstatSync(descriptor));
      if (!sameDirectorySnapshot(captured.snapshot, openedSnapshot)) {
        throw new SourceAuditFault("identity_changed");
      }

      let probeDescriptor;
      try {
        probeDescriptor = fsImpl.openSync(`${anchorPath}/.`, flags);
        const probeSnapshot = directorySnapshot(fsImpl.fstatSync(probeDescriptor));
        if (!sameDirectorySnapshot(openedSnapshot, probeSnapshot)) {
          throw new SourceAuditFault("platform_unsupported");
        }
      } catch {
        throw new SourceAuditFault("platform_unsupported");
      } finally {
        if (probeDescriptor !== undefined) {
          try {
            fsImpl.closeSync(probeDescriptor);
          } catch {
            throw new SourceAuditFault("platform_unsupported");
          }
        }
      }
    }
    return handles;
  } catch (error) {
    if (closeHeldDirectoryHandles(fsImpl, handles)) {
      throw new SourceAuditFault("io_error");
    }
    throw error;
  }
}

function auditExpectedSourceFile(fsImpl, treeSnapshot, directoryHandles, manifestEntry) {
  const relativeDirectory = path.posix.dirname(manifestEntry.path);
  const filename = path.posix.basename(manifestEntry.path);
  const capturedDirectory = treeSnapshot.directories.get(relativeDirectory);
  if (!capturedDirectory) {
    const reason = treeSnapshot.directoryFailures.get(relativeDirectory) || "missing";
    return reason === "missing"
      ? { state: "missing" }
      : { state: "mismatch", reason, actualSha256: null };
  }
  if (capturedDirectory.readFailure) {
    return { state: "mismatch", reason: capturedDirectory.readFailure, actualSha256: null };
  }
  if (capturedDirectory.childFailures.has(filename)) {
    const reason = capturedDirectory.childFailures.get(filename);
    return reason === "missing"
      ? { state: "missing" }
      : { state: "mismatch", reason, actualSha256: null };
  }
  const capturedFile = capturedDirectory.children.get(filename);
  if (!capturedFile) return { state: "missing" };

  let descriptor;
  let outcome;
  try {
    requireRegularSingleLink(capturedFile.status);
    const directoryHandle = directoryHandles.get(relativeDirectory);
    if (!directoryHandle) throw new SourceAuditFault("platform_unsupported");
    const anchoredPath = `${directoryHandle.anchorPath}/${filename}`;
    const anchoredStatus = fsImpl.lstatSync(anchoredPath);
    requireRegularSingleLink(anchoredStatus);
    if (!sameSourceEntrySnapshot(capturedFile.snapshot, sourceEntrySnapshot(anchoredStatus))) {
      throw new SourceAuditFault("identity_changed");
    }
    descriptor = fsImpl.openSync(
      anchoredPath,
      fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK,
    );
    const actualSha256 = hashOpenedSourceFile(fsImpl, descriptor, anchoredPath, anchoredStatus);
    outcome = actualSha256 === manifestEntry.sha256
      ? { state: "present" }
      : { state: "mismatch", reason: "hash_mismatch", actualSha256 };
  } catch (error) {
    outcome = {
      state: "mismatch",
      reason: sourceAuditReason(error),
      actualSha256: null,
    };
  } finally {
    if (descriptor !== undefined) {
      try {
        fsImpl.closeSync(descriptor);
      } catch {
        outcome = { state: "mismatch", reason: "io_error", actualSha256: null };
      }
    }
  }
  return outcome;
}

function addTreeSnapshotDiagnostics(
  treeSnapshot,
  unexpectedEntries,
  allowedEntries,
  policyViolations,
) {
  for (const entry of treeSnapshot.unexpectedEntries) unexpectedEntries.add(entry);
  for (const entry of treeSnapshot.allowedEntries) allowedEntries.add(entry);
  for (const entry of treeSnapshot.policyViolations) {
    addPolicyViolation(policyViolations, entry.path, entry.reason);
  }
}

function sameCapturedTreeEntry(left, right) {
  if (!left || !right || left.snapshot.type !== right.snapshot.type) return false;
  if (left.snapshot.type === "directory") {
    return sameDirectoryIdentity(left.snapshot, right.snapshot);
  }
  return sameSourceEntrySnapshot(left.snapshot, right.snapshot);
}

function sameExactChildren(left, right) {
  return left.length === right.length && left.every((name, index) => name === right[index]);
}

function compareAuditedPluginTreeSnapshots(before, after, policyViolations) {
  for (const relativeDirectory of AUDITED_PLUGIN_DIRECTORIES) {
    const left = before.directories.get(relativeDirectory);
    const right = after.directories.get(relativeDirectory);
    if (!left || !right) {
      if (Boolean(left) !== Boolean(right)) {
        addPolicyViolation(policyViolations, relativeDirectory, "identity_changed");
      }
      continue;
    }
    if (!sameDirectorySnapshot(left.snapshot, right.snapshot)) {
      addPolicyViolation(policyViolations, relativeDirectory, "identity_changed");
    }
    if (!sameExactChildren(left.actualChildren, right.actualChildren)) {
      addPolicyViolation(policyViolations, relativeDirectory, "identity_changed");
    }
    for (const name of expectedDirectoryChildTypes(relativeDirectory).keys()) {
      const leftEntry = left.children.get(name);
      const rightEntry = right.children.get(name);
      if (Boolean(leftEntry) !== Boolean(rightEntry) || (
        leftEntry
        && rightEntry
        && !sameCapturedTreeEntry(leftEntry, rightEntry)
      )) {
        addPolicyViolation(
          policyViolations,
          `${relativeDirectory}/${name}`,
          "identity_changed",
        );
      }
    }
  }
}

function inspectVistaAnimationUePluginSource(projectRoot, { fsImpl = fs } = {}) {
  if (typeof projectRoot !== "string" || projectRoot.length === 0 || !path.isAbsolute(projectRoot)) {
    throw new TypeError("projectRoot must be an absolute path");
  }
  const requiredFsMethods = [
    "closeSync",
    "fstatSync",
    "lstatSync",
    "openSync",
    "readSync",
    "readdirSync",
    "realpathSync",
  ];
  if (!fsImpl || requiredFsMethods.some((method) => typeof fsImpl[method] !== "function")) {
    throw new TypeError("fsImpl must provide the complete synchronous source-audit interface");
  }
  const canonicalProjectRoot = path.resolve(projectRoot);
  const presentFiles = [];
  const missingFiles = [];
  const mismatchedFiles = [];
  const unexpectedEntries = new Set();
  const allowedEntries = new Set();
  const policyViolations = { keys: new Set(), values: [] };
  let directoryHandles = new Map();
  let fatalReason = null;

  try {
    assertAnchoredTraversalPlatform();
    const projectRootChain = captureCanonicalDirectoryChain(fsImpl, canonicalProjectRoot);
    const before = captureAuditedPluginTreeSnapshot(fsImpl, canonicalProjectRoot);
    addTreeSnapshotDiagnostics(before, unexpectedEntries, allowedEntries, policyViolations);
    directoryHandles = openHeldDirectoryHandles(fsImpl, canonicalProjectRoot, before);
    for (const manifestEntry of EXPECTED_PLUGIN_SOURCE_MANIFEST) {
      const outcome = auditExpectedSourceFile(fsImpl, before, directoryHandles, manifestEntry);
      if (outcome.state === "present") {
        presentFiles.push(manifestEntry.path);
      } else if (outcome.state === "missing") {
        missingFiles.push(manifestEntry.path);
      } else {
        mismatchedFiles.push({
          path: manifestEntry.path,
          reason: outcome.reason,
          expected_sha256: manifestEntry.sha256,
          actual_sha256: outcome.actualSha256,
        });
      }
    }
    const after = captureAuditedPluginTreeSnapshot(fsImpl, canonicalProjectRoot);
    addTreeSnapshotDiagnostics(after, unexpectedEntries, allowedEntries, policyViolations);
    compareAuditedPluginTreeSnapshots(before, after, policyViolations);
    const finalProjectRootChain = captureCanonicalDirectoryChain(fsImpl, canonicalProjectRoot);
    if (!sameDirectoryChain(projectRootChain, finalProjectRootChain)) {
      throw new SourceAuditFault("identity_changed");
    }
  } catch (error) {
    fatalReason = sourceAuditReason(error);
  } finally {
    if (closeHeldDirectoryHandles(fsImpl, directoryHandles)) fatalReason = "io_error";
  }

  if (fatalReason) {
    presentFiles.length = 0;
    missingFiles.length = 0;
    mismatchedFiles.length = 0;
    missingFiles.push(...EXPECTED_PLUGIN_SOURCE_FILES);
    addPolicyViolation(policyViolations, ".", fatalReason);
  }

  const finalUnexpectedEntries = [...unexpectedEntries].sort();
  const finalAllowedEntries = [...allowedEntries].sort();
  const finalPolicyViolations = policyViolations.values
    .map((entry) => ({ ...entry }))
    .sort((left, right) => (
      left.path.localeCompare(right.path) || left.reason.localeCompare(right.reason)
    ));
  const sourceTreeComplete = (
    presentFiles.length === EXPECTED_PLUGIN_SOURCE_MANIFEST.length
    && missingFiles.length === 0
    && mismatchedFiles.length === 0
    && finalUnexpectedEntries.length === 0
    && finalPolicyViolations.length === 0
  );
  return deepFreeze({
    schema: ANIMATION_UE_SOURCE_AUDIT_SCHEMA,
    plugin_name: ANIMATION_UE_PLUGIN_NAME,
    source_manifest_sha256: ANIMATION_UE_SOURCE_MANIFEST_SHA256,
    source_tree_complete: sourceTreeComplete,
    expected_manifest: EXPECTED_PLUGIN_SOURCE_MANIFEST.map((entry) => ({ ...entry })),
    expected_files: [...EXPECTED_PLUGIN_SOURCE_FILES],
    present_files: presentFiles,
    missing_files: [...new Set(missingFiles)],
    mismatched_files: mismatchedFiles,
    unexpected_entries: finalUnexpectedEntries,
    allowed_nonproduction_entries: finalAllowedEntries,
    policy_violations: finalPolicyViolations,
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
  ANIMATION_UE_SOURCE_MANIFEST_SHA256,
  EXPECTED_PLUGIN_SOURCE_MANIFEST,
  EXPECTED_PLUGIN_SOURCE_FILES,
  MAX_PLUGIN_SOURCE_FILE_BYTES,
  OPERATION_SET,
  SECURITY_POLICY,
  VistaAnimationUeReadinessError,
  createVistaAnimationUeReadinessProbe,
  inspectVistaAnimationUePluginSource,
  validateCapabilityResponse,
};
