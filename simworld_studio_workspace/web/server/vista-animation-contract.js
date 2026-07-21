"use strict";

const crypto = require("node:crypto");

const {
  ACTION_ADAPTER_SCHEMA,
  CAPABILITY_REGISTRY_SCHEMA,
  validateBindings,
  validateCompiledTimeline,
} = require("./vista-timeline-compiler");

const ANIMATION_CONTENT_PROFILE_SCHEMA = "vista-animation-content-profile/v1";
const ANIMATION_PREFLIGHT_REQUEST_SCHEMA = "vista-animation-preflight-request/v1";
const ANIMATION_PREFLIGHT_RESPONSE_SCHEMA = "vista-animation-preflight-response/v1";
const ANIMATION_PREFLIGHT_SCHEMA = "vista-animation-preflight/v1";
const ANIMATION_PROGRAM_SCHEMA = "vista-animation-program/v1";
const ADAPTER_VERSION = "1.0.0";
const DEFAULT_FPS = 30;
const MAX_FPS = 240;

const SAFE_ID_RE = /^[a-z][a-z0-9_-]{0,119}$/;
const OPAQUE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,159}$/;
const SHA256_RE = /^[a-f0-9]{64}$/;
const UE_CONTENT_PATH_RE = /^\/Game\/[A-Za-z0-9_./-]{1,500}$/;
const DATE_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/;
const EVENT_ID_RE = /^beat-[0-9]{4}(?:-[a-z0-9_-]+)?$/;

const ACTION_DEFINITIONS = deepFreeze({
  look_at: {
    adapter_id: "vista_look_at_v1",
    bridge_action_id: "vista_look_at_v1",
    actor_kinds: ["player"],
    target_policy: "required",
    target_kinds: ["prop"],
    actor_capabilities: ["gaze"],
    target_capabilities: ["gaze_target"],
    anchor_kinds: ["gaze_target"],
    defaults: { duration_sec: 1 },
  },
  pick_up: {
    adapter_id: "vista_pick_up_ik_v1",
    bridge_action_id: "vista_pick_up_ik_v1",
    actor_kinds: ["player"],
    target_policy: "required",
    target_kinds: ["prop"],
    actor_capabilities: ["upper_body_ik", "object_attachment"],
    target_capabilities: ["pickupable", "hand_contact_target"],
    anchor_kinds: ["hand_contact"],
    completion_signal: "vista_pick_up_attached",
    defaults: { hand: "right", duration_sec: 2 },
  },
  brace: {
    adapter_id: "vista_brace_ik_v1",
    bridge_action_id: "vista_brace_ik_v1",
    actor_kinds: ["player"],
    target_policy: "required",
    target_kinds: ["prop"],
    actor_capabilities: ["upper_body_ik"],
    target_capabilities: ["hand_contact_target"],
    anchor_kinds: ["hand_contact"],
    defaults: { hand: "both", duration_sec: 2 },
  },
  drag: {
    adapter_id: "vista_drag_ik_v1",
    bridge_action_id: "vista_drag_ik_v1",
    actor_kinds: ["player"],
    target_policy: "required",
    target_kinds: ["prop"],
    actor_capabilities: ["root_motion", "upper_body_ik"],
    target_capabilities: ["draggable", "hand_contact_target"],
    anchor_kinds: ["hand_contact"],
    defaults: { hand: "right", distance_cm: 120, duration_sec: 2 },
  },
  lift_foot: {
    adapter_id: "vista_lift_foot_ik_v1",
    bridge_action_id: "vista_lift_foot_ik_v1",
    actor_kinds: ["player"],
    target_policy: "required",
    target_kinds: ["prop"],
    actor_capabilities: ["lower_body_ik"],
    target_capabilities: ["foot_contact_target"],
    anchor_kinds: ["foot_contact"],
    defaults: { foot: "left", height_cm: 35, duration_sec: 2 },
  },
  pause: {
    adapter_id: "vista_pause_pose_v1",
    bridge_action_id: "vista_pause_pose_v1",
    actor_kinds: ["player"],
    target_policy: "optional",
    target_kinds: ["prop"],
    actor_capabilities: ["hold_pose"],
    target_capabilities: [],
    anchor_kinds: [],
    defaults: { duration_sec: 3 },
  },
  fall: {
    adapter_id: "vista_fall_montage_v1",
    bridge_action_id: "vista_fall_montage_v1",
    actor_kinds: ["player"],
    target_policy: "forbidden",
    target_kinds: [],
    actor_capabilities: ["fall_montage"],
    target_capabilities: [],
    anchor_kinds: [],
    defaults: { direction: "forward" },
  },
  recover: {
    adapter_id: "vista_recover_montage_v1",
    bridge_action_id: "vista_recover_montage_v1",
    actor_kinds: ["player"],
    target_policy: "forbidden",
    target_kinds: [],
    actor_capabilities: ["recover_montage"],
    target_capabilities: [],
    anchor_kinds: [],
    defaults: { direction: "forward" },
  },
});

class VistaAnimationContractError extends Error {
  constructor(code, message, { status = 400, retryable = false, details = {} } = {}) {
    super(message);
    this.name = "VistaAnimationContractError";
    this.code = code;
    this.status = status;
    this.retryable = retryable;
    this.details = sanitizeDetails(details);
  }
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function deepFreeze(value) {
  if (!value || typeof value !== "object" || Object.isFrozen(value)) return value;
  Object.freeze(value);
  for (const child of Object.values(value)) deepFreeze(child);
  return value;
}

function sanitizeDetails(value, depth = 0) {
  if (depth > 4) return "[truncated]";
  if (value === null || typeof value === "boolean" || typeof value === "number") return value;
  if (typeof value === "string") return value.slice(0, 500);
  if (Array.isArray(value)) return value.slice(0, 50).map((item) => sanitizeDetails(item, depth + 1));
  if (!isPlainObject(value)) return undefined;
  const output = {};
  for (const key of Object.keys(value).sort().slice(0, 50)) {
    if (/token|secret|password|credential|authorization|cookie/i.test(key)) continue;
    const normalized = sanitizeDetails(value[key], depth + 1);
    if (normalized !== undefined) output[key] = normalized;
  }
  return output;
}

function fail(code, message, options) {
  throw new VistaAnimationContractError(code, message, options);
}

function exactKeys(value, allowed, required, pointer, code = "ANIMATION_CONTRACT_INVALID") {
  if (!isPlainObject(value)) fail(code, `${pointer} must be an object`);
  const allowedSet = new Set(allowed);
  const unknown = Object.keys(value).filter((key) => !allowedSet.has(key));
  const missing = required.filter((key) => !Object.prototype.hasOwnProperty.call(value, key));
  if (unknown.length || missing.length) {
    fail(code, `${pointer} has an invalid shape`, { details: { pointer, unknown, missing } });
  }
}

function requireString(value, pointer, { pattern = null, max = 500, code = "ANIMATION_CONTRACT_INVALID" } = {}) {
  if (typeof value !== "string" || value.length === 0 || value.length > max || (pattern && !pattern.test(value))) {
    fail(code, `${pointer} is invalid`, { details: { pointer } });
  }
  return value;
}

function requireBoolean(value, pointer, code = "ANIMATION_CONTRACT_INVALID") {
  if (typeof value !== "boolean") fail(code, `${pointer} must be boolean`, { details: { pointer } });
  return value;
}

function requireUniqueStrings(value, pointer, { pattern = SAFE_ID_RE, maxItems = 128, code = "ANIMATION_CONTRACT_INVALID" } = {}) {
  if (!Array.isArray(value) || value.length > maxItems) fail(code, `${pointer} must be a bounded array`);
  const seen = new Set();
  for (let index = 0; index < value.length; index += 1) {
    const item = requireString(value[index], `${pointer}[${index}]`, { pattern, max: 160, code });
    if (seen.has(item)) fail(code, `${pointer} must be unique`, { details: { item } });
    seen.add(item);
  }
  return [...seen].sort();
}

function cloneJson(value, pointer = "value", depth = 0) {
  if (depth > 8) fail("ANIMATION_JSON_INVALID", `${pointer} exceeds maximum depth`);
  if (value === null || typeof value === "boolean" || typeof value === "string") return value;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (Array.isArray(value)) {
    if (value.length > 128) fail("ANIMATION_JSON_INVALID", `${pointer} is too large`);
    return value.map((item, index) => cloneJson(item, `${pointer}[${index}]`, depth + 1));
  }
  if (!isPlainObject(value)) fail("ANIMATION_JSON_INVALID", `${pointer} must contain JSON-safe values`);
  const keys = Object.keys(value);
  if (keys.length > 128) fail("ANIMATION_JSON_INVALID", `${pointer} has too many keys`);
  const output = {};
  for (const key of keys.sort()) {
    if (["__proto__", "constructor", "prototype"].includes(key)) fail("ANIMATION_JSON_INVALID", `${pointer} contains an unsafe key`);
    output[key] = cloneJson(value[key], `${pointer}.${key}`, depth + 1);
  }
  return output;
}

function canonicalize(value) {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalize).join(",")}]`;
  return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalize(value[key])}`).join(",")}}`;
}

function digest(value) {
  return crypto.createHash("sha256").update(canonicalize(value)).digest("hex");
}

function sameMembers(actual, expected) {
  if (actual.length !== expected.length) return false;
  return actual.every((value, index) => value === expected[index]);
}

function includesAll(actual, required) {
  const values = new Set(actual);
  return required.every((value) => values.has(value));
}

function requiredAnchorsForCapabilities(capabilities) {
  const declared = new Set(capabilities);
  const anchors = new Set();
  for (const definition of Object.values(ACTION_DEFINITIONS)) {
    if (definition.target_capabilities.some((capability) => declared.has(capability))) {
      definition.anchor_kinds.forEach((anchor) => anchors.add(anchor));
    }
  }
  return [...anchors].sort();
}

function validateActionParameters(action, parameters) {
  if (!isPlainObject(parameters)) fail("ANIMATION_PARAMETERS_INVALID", `Parameters for '${action}' must be an object`);
  const definition = ACTION_DEFINITIONS[action];
  if (!definition) fail("ANIMATION_ACTION_UNSUPPORTED", `Action '${action}' has no fixed animation definition`);
  const allowed = {
    look_at: ["duration_sec"],
    pick_up: ["hand", "duration_sec"],
    brace: ["hand", "duration_sec"],
    drag: ["hand", "distance_cm", "duration_sec"],
    lift_foot: ["foot", "height_cm", "duration_sec"],
    pause: ["duration_sec"],
    fall: ["direction"],
    recover: ["direction"],
  }[action];
  exactKeys(parameters, allowed, [], `parameters.${action}`, "ANIMATION_PARAMETERS_INVALID");
  const normalized = { ...definition.defaults, ...cloneJson(parameters, `parameters.${action}`) };
  if (normalized.duration_sec !== undefined && (!Number.isFinite(normalized.duration_sec) || normalized.duration_sec <= 0 || normalized.duration_sec > 60)) {
    fail("ANIMATION_PARAMETERS_INVALID", `${action}.duration_sec must be in (0, 60]`);
  }
  if (normalized.distance_cm !== undefined && (!Number.isFinite(normalized.distance_cm) || normalized.distance_cm < 1 || normalized.distance_cm > 500)) {
    fail("ANIMATION_PARAMETERS_INVALID", "drag.distance_cm must be in [1, 500]");
  }
  if (normalized.height_cm !== undefined && (!Number.isFinite(normalized.height_cm) || normalized.height_cm < 1 || normalized.height_cm > 150)) {
    fail("ANIMATION_PARAMETERS_INVALID", "lift_foot.height_cm must be in [1, 150]");
  }
  if (normalized.hand !== undefined && !new Set(["left", "right", "both"]).has(normalized.hand)) {
    fail("ANIMATION_PARAMETERS_INVALID", `${action}.hand is invalid`);
  }
  if (normalized.foot !== undefined && !new Set(["left", "right"]).has(normalized.foot)) {
    fail("ANIMATION_PARAMETERS_INVALID", "lift_foot.foot is invalid");
  }
  if (normalized.direction !== undefined && !new Set(["forward", "backward", "left", "right"]).has(normalized.direction)) {
    fail("ANIMATION_PARAMETERS_INVALID", `${action}.direction is invalid`);
  }
  return deepFreeze(cloneJson(normalized));
}

function validateContentProfile(profile) {
  const code = "ANIMATION_CONTENT_PROFILE_INVALID";
  exactKeys(
    profile,
    ["schema", "profile_id", "revision", "content_revision", "content_digest", "pawn_class_path", "skeleton_path", "verification", "actions"],
    ["schema", "profile_id", "revision", "content_revision", "content_digest", "pawn_class_path", "skeleton_path", "verification", "actions"],
    "content profile",
    code,
  );
  if (profile.schema !== ANIMATION_CONTENT_PROFILE_SCHEMA) fail(code, "Unsupported animation content profile schema");
  const profileId = requireString(profile.profile_id, "content profile.profile_id", { pattern: SAFE_ID_RE, max: 120, code });
  const revision = requireString(profile.revision, "content profile.revision", { pattern: SAFE_ID_RE, max: 120, code });
  const contentRevision = requireString(profile.content_revision, "content profile.content_revision", { pattern: OPAQUE_ID_RE, max: 160, code });
  const contentDigest = requireString(profile.content_digest, "content profile.content_digest", { pattern: SHA256_RE, max: 64, code });
  const pawnClassPath = requireString(profile.pawn_class_path, "content profile.pawn_class_path", { pattern: UE_CONTENT_PATH_RE, max: 512, code });
  const skeletonPath = requireString(profile.skeleton_path, "content profile.skeleton_path", { pattern: UE_CONTENT_PATH_RE, max: 512, code });
  if ([pawnClassPath, skeletonPath].some((value) => value.includes("..") || value.includes("//"))) {
    fail(code, "Animation content paths must be canonical /Game paths");
  }
  exactKeys(profile.verification, ["status", "receipt_id", "verified_at"], ["status", "receipt_id", "verified_at"], "content profile.verification", code);
  if (profile.verification.status !== "verified") fail(code, "Animation content profile is not verified");
  const receiptId = requireString(profile.verification.receipt_id, "content profile.verification.receipt_id", { pattern: OPAQUE_ID_RE, max: 160, code });
  const verifiedAt = requireString(profile.verification.verified_at, "content profile.verification.verified_at", { pattern: DATE_RE, max: 32, code });
  if (Number.isNaN(Date.parse(verifiedAt))) fail(code, "Animation content verification timestamp is invalid");
  if (!Array.isArray(profile.actions) || profile.actions.length === 0 || profile.actions.length > Object.keys(ACTION_DEFINITIONS).length) {
    fail(code, "content profile.actions must contain a bounded verified set");
  }
  const seen = new Set();
  const actions = profile.actions.map((entry, index) => {
    const pointer = `content profile.actions[${index}]`;
    exactKeys(
      entry,
      ["action", "adapter_id", "version", "bridge_action_id", "implementation_asset", "completion_signal", "timeout_ms"],
      ["action", "adapter_id", "version", "bridge_action_id", "implementation_asset", "completion_signal", "timeout_ms"],
      pointer,
      code,
    );
    const action = requireString(entry.action, `${pointer}.action`, { pattern: SAFE_ID_RE, max: 120, code });
    const definition = ACTION_DEFINITIONS[action];
    if (!definition) fail(code, `Action '${action}' is not in the fixed server allowlist`);
    if (seen.has(action)) fail(code, `Action '${action}' is duplicated`);
    seen.add(action);
    if (entry.adapter_id !== definition.adapter_id || entry.bridge_action_id !== definition.bridge_action_id || entry.version !== ADAPTER_VERSION) {
      fail(code, `Action '${action}' does not use the fixed adapter identity`);
    }
    const implementationAsset = requireString(entry.implementation_asset, `${pointer}.implementation_asset`, { pattern: UE_CONTENT_PATH_RE, max: 512, code });
    if (implementationAsset.includes("..") || implementationAsset.includes("//")) fail(code, `${pointer}.implementation_asset is not canonical`);
    const completionSignal = requireString(entry.completion_signal, `${pointer}.completion_signal`, { pattern: SAFE_ID_RE, max: 120, code });
    if (definition.completion_signal && completionSignal !== definition.completion_signal) {
      fail(code, `Action '${action}' does not use the fixed completion signal`);
    }
    if (!Number.isInteger(entry.timeout_ms) || entry.timeout_ms < 100 || entry.timeout_ms > 60_000) {
      fail(code, `${pointer}.timeout_ms must be an integer in [100, 60000]`);
    }
    return {
      action,
      adapter_id: definition.adapter_id,
      version: ADAPTER_VERSION,
      bridge_action_id: definition.bridge_action_id,
      implementation_asset: implementationAsset,
      completion_signal: completionSignal,
      timeout_ms: entry.timeout_ms,
    };
  }).sort((left, right) => left.action.localeCompare(right.action));
  return deepFreeze({
    schema: ANIMATION_CONTENT_PROFILE_SCHEMA,
    profile_id: profileId,
    revision,
    content_revision: contentRevision,
    content_digest: contentDigest,
    pawn_class_path: pawnClassPath,
    skeleton_path: skeletonPath,
    verification: { status: "verified", receipt_id: receiptId, verified_at: verifiedAt },
    actions,
  });
}

function makeAnimationPreflightRequest({ contentProfile, sceneRevision, bindings, requestedActions }) {
  const profile = validateContentProfile(contentProfile);
  const normalizedBindings = validateBindings(bindings);
  const scene = requireString(sceneRevision, "sceneRevision", { pattern: OPAQUE_ID_RE, max: 160, code: "ANIMATION_PREFLIGHT_INPUT_INVALID" });
  const actions = requireUniqueStrings(requestedActions, "requestedActions", { code: "ANIMATION_PREFLIGHT_INPUT_INVALID" });
  if (actions.length === 0) fail("ANIMATION_PREFLIGHT_INPUT_INVALID", "requestedActions cannot be empty");
  for (const action of actions) {
    if (!ACTION_DEFINITIONS[action]) fail("ANIMATION_PREFLIGHT_INPUT_INVALID", `Action '${action}' is not allowlisted`);
  }
  return deepFreeze({
    schema: ANIMATION_PREFLIGHT_REQUEST_SCHEMA,
    scene_revision: scene,
    profile_id: profile.profile_id,
    profile_revision: profile.revision,
    content_revision: profile.content_revision,
    content_digest: profile.content_digest,
    pawn_class_path: profile.pawn_class_path,
    skeleton_path: profile.skeleton_path,
    requested_actions: actions,
    actor_binding_ids: normalizedBindings.actors.map((binding) => binding.binding_id).sort(),
    target_binding_ids: normalizedBindings.entities.map((binding) => binding.binding_id).sort(),
  });
}

function validateRuntimeBinding(entry, pointer, expectedIds, code) {
  exactKeys(
    entry,
    ["binding_id", "available", "class_matches", "skeleton_matches", "capabilities", "anchor_kinds"],
    ["binding_id", "available", "class_matches", "skeleton_matches", "capabilities", "anchor_kinds"],
    pointer,
    code,
  );
  const bindingId = requireString(entry.binding_id, `${pointer}.binding_id`, { pattern: SAFE_ID_RE, max: 120, code });
  if (!expectedIds.has(bindingId)) fail(code, `${pointer}.binding_id was not requested`);
  return {
    binding_id: bindingId,
    available: requireBoolean(entry.available, `${pointer}.available`, code),
    class_matches: requireBoolean(entry.class_matches, `${pointer}.class_matches`, code),
    skeleton_matches: requireBoolean(entry.skeleton_matches, `${pointer}.skeleton_matches`, code),
    capabilities: requireUniqueStrings(entry.capabilities, `${pointer}.capabilities`, { code }),
    anchor_kinds: requireUniqueStrings(entry.anchor_kinds, `${pointer}.anchor_kinds`, { code }),
  };
}

function validateAnimationPreflightResponse(request, response) {
  const code = "ANIMATION_PREFLIGHT_PROTOCOL_INVALID";
  exactKeys(
    response,
    ["schema", "scene_revision", "profile_revision", "content_revision", "content_digest", "ready", "actors", "targets", "actions"],
    ["schema", "scene_revision", "profile_revision", "content_revision", "content_digest", "ready", "actors", "targets", "actions"],
    "animation preflight response",
    code,
  );
  if (
    response.schema !== ANIMATION_PREFLIGHT_RESPONSE_SCHEMA
    || response.scene_revision !== request.scene_revision
    || response.profile_revision !== request.profile_revision
    || response.content_revision !== request.content_revision
    || response.content_digest !== request.content_digest
  ) fail(code, "Animation preflight response provenance does not match the request", { status: 502 });
  const expectedActors = new Set(request.actor_binding_ids);
  const expectedTargets = new Set(request.target_binding_ids);
  if (!Array.isArray(response.actors) || !Array.isArray(response.targets) || !Array.isArray(response.actions)) {
    fail(code, "Animation preflight response collections are invalid", { status: 502 });
  }
  const actors = response.actors.map((entry, index) => validateRuntimeBinding(entry, `animation preflight response.actors[${index}]`, expectedActors, code));
  const targets = response.targets.map((entry, index) => validateRuntimeBinding(entry, `animation preflight response.targets[${index}]`, expectedTargets, code));
  if (!sameMembers(actors.map((entry) => entry.binding_id).sort(), [...expectedActors].sort())) fail(code, "Animation preflight did not return every actor binding", { status: 502 });
  if (!sameMembers(targets.map((entry) => entry.binding_id).sort(), [...expectedTargets].sort())) fail(code, "Animation preflight did not return every target binding", { status: 502 });
  const requested = new Set(request.requested_actions);
  const actionIds = new Set();
  const actions = response.actions.map((entry, index) => {
    const pointer = `animation preflight response.actions[${index}]`;
    exactKeys(
      entry,
      ["action", "bridge_action_id", "available", "implementation_matches", "completion_signal_available"],
      ["action", "bridge_action_id", "available", "implementation_matches", "completion_signal_available"],
      pointer,
      code,
    );
    const action = requireString(entry.action, `${pointer}.action`, { pattern: SAFE_ID_RE, max: 120, code });
    if (!requested.has(action) || actionIds.has(action)) fail(code, `${pointer}.action was not requested or is duplicated`, { status: 502 });
    actionIds.add(action);
    const definition = ACTION_DEFINITIONS[action];
    if (entry.bridge_action_id !== definition.bridge_action_id) fail(code, `${pointer}.bridge_action_id is not the fixed action id`, { status: 502 });
    return {
      action,
      bridge_action_id: definition.bridge_action_id,
      available: requireBoolean(entry.available, `${pointer}.available`, code),
      implementation_matches: requireBoolean(entry.implementation_matches, `${pointer}.implementation_matches`, code),
      completion_signal_available: requireBoolean(entry.completion_signal_available, `${pointer}.completion_signal_available`, code),
    };
  });
  if (!sameMembers([...actionIds].sort(), [...requested].sort())) fail(code, "Animation preflight did not return every requested action", { status: 502 });
  const computedReady = actors.every((entry) => entry.available && entry.class_matches && entry.skeleton_matches)
    && targets.every((entry) => entry.available && entry.class_matches)
    && actions.every((entry) => entry.available && entry.implementation_matches && entry.completion_signal_available);
  if (typeof response.ready !== "boolean" || response.ready !== computedReady) {
    fail(code, "Animation preflight ready flag is inconsistent", { status: 502 });
  }
  return deepFreeze({
    schema: ANIMATION_PREFLIGHT_RESPONSE_SCHEMA,
    scene_revision: request.scene_revision,
    profile_revision: request.profile_revision,
    content_revision: request.content_revision,
    content_digest: request.content_digest,
    ready: computedReady,
    actors: actors.sort((left, right) => left.binding_id.localeCompare(right.binding_id)),
    targets: targets.sort((left, right) => left.binding_id.localeCompare(right.binding_id)),
    actions: actions.sort((left, right) => left.action.localeCompare(right.action)),
  });
}

function makeIssue(code, subject, message) {
  return {
    issue_id: `vai-${digest({ code, subject }).slice(0, 16)}`,
    code,
    subject,
    message,
  };
}

function buildAnimationPreflightArtifact({ request, response, contentProfile, bindings, registryBinding = null }) {
  const profile = validateContentProfile(contentProfile);
  const normalizedBindings = validateBindings(bindings);
  if (registryBinding !== null) {
    requireString(registryBinding, "registryBinding", {
      pattern: SAFE_ID_RE,
      max: 120,
      code: "ANIMATION_PREFLIGHT_INPUT_INVALID",
    });
  }
  const actionProfiles = new Map(profile.actions.map((entry) => [entry.action, entry]));
  const bindingActors = new Map(normalizedBindings.actors.map((entry) => [entry.binding_id, entry]));
  const bindingTargets = new Map(normalizedBindings.entities.map((entry) => [entry.binding_id, entry]));
  const issues = [];
  const actors = response.actors.map((entry) => {
    const declared = bindingActors.get(entry.binding_id);
    const ready = entry.available && entry.class_matches && entry.skeleton_matches && includesAll(entry.capabilities, declared.capabilities);
    if (!ready) issues.push(makeIssue("ACTOR_CAPABILITY_UNVERIFIED", entry.binding_id, `Actor binding '${entry.binding_id}' did not pass runtime capability verification`));
    return { ...entry, declared_capabilities: [...declared.capabilities], ready };
  });
  const targets = response.targets.map((entry) => {
    const declared = bindingTargets.get(entry.binding_id);
    const requiredAnchors = requiredAnchorsForCapabilities(declared.capabilities);
    const ready = entry.available
      && entry.class_matches
      && includesAll(entry.capabilities, declared.capabilities)
      && includesAll(entry.anchor_kinds, requiredAnchors);
    if (!ready) issues.push(makeIssue("TARGET_CAPABILITY_UNVERIFIED", entry.binding_id, `Target binding '${entry.binding_id}' did not pass runtime capability verification`));
    return { ...entry, declared_capabilities: [...declared.capabilities], ready };
  });
  const actions = response.actions.map((entry) => {
    const actionProfile = actionProfiles.get(entry.action) || null;
    const ready = Boolean(actionProfile && entry.available && entry.implementation_matches && entry.completion_signal_available);
    let code = null;
    if (!actionProfile) code = "ACTION_CONTENT_UNVERIFIED";
    else if (!entry.available || !entry.implementation_matches) code = "ACTION_IMPLEMENTATION_UNAVAILABLE";
    else if (!entry.completion_signal_available) code = "ACTION_COMPLETION_SIGNAL_UNAVAILABLE";
    if (code) issues.push(makeIssue(code, entry.action, `Action '${entry.action}' is not verified for this runtime content revision`));
    return {
      ...entry,
      adapter_id: actionProfile ? actionProfile.adapter_id : ACTION_DEFINITIONS[entry.action].adapter_id,
      version: actionProfile ? actionProfile.version : ADAPTER_VERSION,
      timeout_ms: actionProfile ? actionProfile.timeout_ms : null,
      implementation_asset: actionProfile ? actionProfile.implementation_asset : null,
      completion_signal: actionProfile ? actionProfile.completion_signal : null,
      ready,
    };
  });
  issues.sort((left, right) => left.subject.localeCompare(right.subject) || left.code.localeCompare(right.code));
  const supportedActions = actions.filter((entry) => entry.ready).map((entry) => entry.action).sort();
  const registryRevision = `anim_${digest({
    scene_revision: request.scene_revision,
    profile_revision: profile.revision,
    content_digest: profile.content_digest,
    ...(registryBinding === null ? {} : { adapter_registry_binding: registryBinding }),
    supported_actions: supportedActions,
    actors: actors.map((entry) => ({ binding_id: entry.binding_id, capabilities: entry.capabilities, ready: entry.ready })),
    targets: targets.map((entry) => ({ binding_id: entry.binding_id, capabilities: entry.capabilities, anchor_kinds: entry.anchor_kinds, ready: entry.ready })),
  }).slice(0, 16)}`;
  const identity = {
    request,
    response,
    registry_revision: registryRevision,
    actors,
    targets,
    actions,
    issues,
  };
  return deepFreeze({
    schema: ANIMATION_PREFLIGHT_SCHEMA,
    preflight_id: `vap-${digest(identity).slice(0, 24)}`,
    scene_revision: request.scene_revision,
    profile_id: profile.profile_id,
    profile_revision: profile.revision,
    content_revision: profile.content_revision,
    content_digest: profile.content_digest,
    registry_revision: registryRevision,
    start_allowed: response.ready && issues.length === 0,
    requested_actions: [...request.requested_actions],
    supported_actions: supportedActions,
    actors,
    targets,
    actions,
    issues,
  });
}

function validateAnimationPreflightArtifact(artifact) {
  const code = "ANIMATION_PREFLIGHT_ARTIFACT_INVALID";
  exactKeys(
    artifact,
    ["schema", "preflight_id", "scene_revision", "profile_id", "profile_revision", "content_revision", "content_digest", "registry_revision", "start_allowed", "requested_actions", "supported_actions", "actors", "targets", "actions", "issues"],
    ["schema", "preflight_id", "scene_revision", "profile_id", "profile_revision", "content_revision", "content_digest", "registry_revision", "start_allowed", "requested_actions", "supported_actions", "actors", "targets", "actions", "issues"],
    "animation preflight artifact",
    code,
  );
  if (artifact.schema !== ANIMATION_PREFLIGHT_SCHEMA || !/^vap-[a-f0-9]{24}$/.test(artifact.preflight_id)) fail(code, "Animation preflight identity is invalid");
  requireString(artifact.scene_revision, "animation preflight.scene_revision", { pattern: OPAQUE_ID_RE, max: 160, code });
  requireString(artifact.profile_id, "animation preflight.profile_id", { pattern: SAFE_ID_RE, max: 120, code });
  requireString(artifact.profile_revision, "animation preflight.profile_revision", { pattern: SAFE_ID_RE, max: 120, code });
  requireString(artifact.content_revision, "animation preflight.content_revision", { pattern: OPAQUE_ID_RE, max: 160, code });
  requireString(artifact.content_digest, "animation preflight.content_digest", { pattern: SHA256_RE, max: 64, code });
  requireString(artifact.registry_revision, "animation preflight.registry_revision", { pattern: SAFE_ID_RE, max: 120, code });
  requireBoolean(artifact.start_allowed, "animation preflight.start_allowed", code);
  const requested = requireUniqueStrings(artifact.requested_actions, "animation preflight.requested_actions", { code });
  const supported = requireUniqueStrings(artifact.supported_actions, "animation preflight.supported_actions", { code });
  if (supported.some((action) => !requested.includes(action))) fail(code, "Supported actions must be requested");
  if (!Array.isArray(artifact.actors) || !Array.isArray(artifact.targets) || !Array.isArray(artifact.actions) || !Array.isArray(artifact.issues)) {
    fail(code, "Animation preflight collections are invalid");
  }
  const bindingIds = new Set();
  for (const [groupName, group] of [["actors", artifact.actors], ["targets", artifact.targets]]) {
    for (let index = 0; index < group.length; index += 1) {
      const entry = group[index];
      const pointer = `animation preflight.${groupName}[${index}]`;
      exactKeys(entry, ["binding_id", "available", "class_matches", "skeleton_matches", "capabilities", "anchor_kinds", "declared_capabilities", "ready"], ["binding_id", "available", "class_matches", "skeleton_matches", "capabilities", "anchor_kinds", "declared_capabilities", "ready"], pointer, code);
      const bindingId = requireString(entry.binding_id, `${pointer}.binding_id`, { pattern: SAFE_ID_RE, max: 120, code });
      if (bindingIds.has(bindingId)) fail(code, "Animation preflight binding ids must be globally unique");
      bindingIds.add(bindingId);
      for (const field of ["available", "class_matches", "skeleton_matches", "ready"]) requireBoolean(entry[field], `${pointer}.${field}`, code);
      const capabilities = requireUniqueStrings(entry.capabilities, `${pointer}.capabilities`, { code });
      requireUniqueStrings(entry.anchor_kinds, `${pointer}.anchor_kinds`, { code });
      const declared = requireUniqueStrings(entry.declared_capabilities, `${pointer}.declared_capabilities`, { code });
      const computed = entry.available
        && entry.class_matches
        && (groupName === "targets" || entry.skeleton_matches)
        && includesAll(capabilities, declared)
        && (groupName !== "targets" || includesAll(entry.anchor_kinds, requiredAnchorsForCapabilities(declared)));
      if (entry.ready !== computed) fail(code, `${pointer}.ready is inconsistent`);
    }
  }
  const actionIds = new Set();
  for (let index = 0; index < artifact.actions.length; index += 1) {
    const entry = artifact.actions[index];
    const pointer = `animation preflight.actions[${index}]`;
    exactKeys(entry, ["action", "bridge_action_id", "available", "implementation_matches", "completion_signal_available", "adapter_id", "version", "timeout_ms", "implementation_asset", "completion_signal", "ready"], ["action", "bridge_action_id", "available", "implementation_matches", "completion_signal_available", "adapter_id", "version", "timeout_ms", "implementation_asset", "completion_signal", "ready"], pointer, code);
    const action = requireString(entry.action, `${pointer}.action`, { pattern: SAFE_ID_RE, max: 120, code });
    const definition = ACTION_DEFINITIONS[action];
    if (!definition || actionIds.has(action) || !requested.includes(action)) fail(code, `${pointer}.action is invalid`);
    actionIds.add(action);
    if (entry.bridge_action_id !== definition.bridge_action_id || entry.adapter_id !== definition.adapter_id || entry.version !== ADAPTER_VERSION) fail(code, `${pointer} does not use the fixed adapter identity`);
    for (const field of ["available", "implementation_matches", "completion_signal_available", "ready"]) requireBoolean(entry[field], `${pointer}.${field}`, code);
    const hasProfile = entry.timeout_ms !== null || entry.implementation_asset !== null || entry.completion_signal !== null;
    if (hasProfile) {
      if (!Number.isInteger(entry.timeout_ms) || entry.timeout_ms < 100 || entry.timeout_ms > 60_000) fail(code, `${pointer}.timeout_ms is invalid`);
      const implementationAsset = requireString(entry.implementation_asset, `${pointer}.implementation_asset`, { pattern: UE_CONTENT_PATH_RE, max: 512, code });
      if (implementationAsset.includes("..") || implementationAsset.includes("//")) fail(code, `${pointer}.implementation_asset is not canonical`);
      requireString(entry.completion_signal, `${pointer}.completion_signal`, { pattern: SAFE_ID_RE, max: 120, code });
    } else if (![entry.timeout_ms, entry.implementation_asset, entry.completion_signal].every((value) => value === null)) {
      fail(code, `${pointer} has a partial content profile`);
    }
    const computed = hasProfile && entry.available && entry.implementation_matches && entry.completion_signal_available;
    if (entry.ready !== computed) fail(code, `${pointer}.ready is inconsistent`);
  }
  if (!sameMembers([...actionIds].sort(), requested)) fail(code, "Animation preflight action coverage is incomplete");
  const issueIds = new Set();
  const issueCodes = new Set(["ACTOR_CAPABILITY_UNVERIFIED", "TARGET_CAPABILITY_UNVERIFIED", "ACTION_CONTENT_UNVERIFIED", "ACTION_IMPLEMENTATION_UNAVAILABLE", "ACTION_COMPLETION_SIGNAL_UNAVAILABLE"]);
  for (let index = 0; index < artifact.issues.length; index += 1) {
    const issue = artifact.issues[index];
    exactKeys(issue, ["issue_id", "code", "subject", "message"], ["issue_id", "code", "subject", "message"], `animation preflight.issues[${index}]`, code);
    if (!/^vai-[a-f0-9]{16}$/.test(issue.issue_id) || issueIds.has(issue.issue_id) || !issueCodes.has(issue.code)) fail(code, "Animation preflight issue identity is invalid");
    issueIds.add(issue.issue_id);
    requireString(issue.subject, `animation preflight.issues[${index}].subject`, { max: 160, code });
    requireString(issue.message, `animation preflight.issues[${index}].message`, { max: 1000, code });
  }
  const readyActions = artifact.actions.filter((entry) => entry && entry.ready === true).map((entry) => entry.action).sort();
  if (!sameMembers(readyActions, supported)) fail(code, "Animation preflight supported action summary is inconsistent");
  const shouldStart = artifact.issues.length === 0
    && artifact.actors.every((entry) => entry.ready === true)
    && artifact.targets.every((entry) => entry.ready === true)
    && artifact.actions.every((entry) => entry.ready === true);
  if (artifact.start_allowed !== shouldStart) fail(code, "Animation preflight start policy is inconsistent");
  return artifact;
}

function toFrame(seconds, fps) {
  return Math.round(seconds * fps);
}

function compileVistaAnimationProgram(timeline, capabilityPreflight, options = {}) {
  validateCompiledTimeline(timeline);
  validateAnimationPreflightArtifact(capabilityPreflight);
  exactKeys(options, ["fps"], [], "animation program options", "ANIMATION_PROGRAM_INPUT_INVALID");
  const fps = options.fps === undefined ? DEFAULT_FPS : options.fps;
  if (!Number.isInteger(fps) || fps < 1 || fps > MAX_FPS) fail("ANIMATION_PROGRAM_INPUT_INVALID", `fps must be an integer in [1, ${MAX_FPS}]`);
  if (timeline.policy !== "strict" || !timeline.start_allowed || timeline.events.some((event) => event.disposition !== "execute")) {
    fail("ANIMATION_PROGRAM_START_BLOCKED", "Only a fully ready strict timeline can produce an animation program", { status: 409 });
  }
  if (!capabilityPreflight.start_allowed) fail("ANIMATION_PROGRAM_CAPABILITY_BLOCKED", "Animation runtime preflight does not allow execution", { status: 409 });
  if (timeline.scene_revision !== capabilityPreflight.scene_revision || timeline.registry_revision !== capabilityPreflight.registry_revision) {
    fail("ANIMATION_PROGRAM_REVISION_MISMATCH", "Timeline and animation capability revisions do not match", { status: 409 });
  }
  const actionEvidence = new Map(capabilityPreflight.actions.map((entry) => [entry.action, entry]));
  let previousFrame = -1;
  let frameOrder = -1;
  const events = timeline.events.map((event, index) => {
    const verified = actionEvidence.get(event.action);
    if (!verified || !verified.ready || verified.adapter_id !== event.adapter.adapter_id || verified.version !== event.adapter.version) {
      fail("ANIMATION_PROGRAM_ADAPTER_MISMATCH", `Event '${event.event_id}' is not backed by the verified content profile`, { status: 409 });
    }
    const atFrame = toFrame(event.at_sec, fps);
    frameOrder = atFrame === previousFrame ? frameOrder + 1 : 0;
    previousFrame = atFrame;
    return {
      event_id: event.event_id,
      order: index,
      at_sec: event.at_sec,
      at_frame: atFrame,
      frame_order: frameOrder,
      action: event.action,
      actor_binding_id: event.actor_binding_id,
      target_binding_id: event.target_binding_id,
      adapter_id: event.adapter.adapter_id,
      bridge_action_id: verified.bridge_action_id,
      parameters: validateActionParameters(event.action, event.parameters),
    };
  });
  const eventCheckpoints = events.map((event) => ({
    checkpoint_id: `vac-${digest({ event_id: event.event_id, kind: "event" }).slice(0, 16)}`,
    kind: "event",
    event_id: event.event_id,
    at_sec: event.at_sec,
    at_frame: event.at_frame,
    frame_order: event.frame_order,
  }));
  const durationFrame = toFrame(timeline.duration_sec, fps);
  const terminalFrameOrder = events.length && events.at(-1).at_frame === durationFrame ? events.at(-1).frame_order + 1 : 0;
  const checkpoints = [...eventCheckpoints, {
    checkpoint_id: `vac-${digest({ timeline_id: timeline.timeline_id, kind: "terminal" }).slice(0, 16)}`,
    kind: "terminal",
    event_id: null,
    at_sec: timeline.duration_sec,
    at_frame: durationFrame,
    frame_order: terminalFrameOrder,
  }].sort((left, right) => left.at_frame - right.at_frame || left.frame_order - right.frame_order || left.checkpoint_id.localeCompare(right.checkpoint_id));
  const identity = {
    timeline_id: timeline.timeline_id,
    preflight_id: capabilityPreflight.preflight_id,
    fps,
    duration_sec: timeline.duration_sec,
    events,
    checkpoints,
  };
  const program = {
    schema: ANIMATION_PROGRAM_SCHEMA,
    program_id: `vag-${digest(identity).slice(0, 24)}`,
    timeline_id: timeline.timeline_id,
    scene_revision: timeline.scene_revision,
    source_checksum: timeline.source_checksum,
    capability_preflight_id: capabilityPreflight.preflight_id,
    profile_revision: capabilityPreflight.profile_revision,
    content_revision: capabilityPreflight.content_revision,
    registry_revision: capabilityPreflight.registry_revision,
    fps,
    duration_sec: timeline.duration_sec,
    duration_frame: durationFrame,
    events,
    checkpoints,
  };
  return deepFreeze(validateVistaAnimationProgram(program));
}

function validateVistaAnimationProgram(program) {
  const code = "ANIMATION_PROGRAM_INVALID";
  const keys = ["schema", "program_id", "timeline_id", "scene_revision", "source_checksum", "capability_preflight_id", "profile_revision", "content_revision", "registry_revision", "fps", "duration_sec", "duration_frame", "events", "checkpoints"];
  exactKeys(program, keys, keys, "animation program", code);
  if (program.schema !== ANIMATION_PROGRAM_SCHEMA || !/^vag-[a-f0-9]{24}$/.test(program.program_id) || !/^vtl-[a-f0-9]{24}$/.test(program.timeline_id) || !/^vap-[a-f0-9]{24}$/.test(program.capability_preflight_id)) {
    fail(code, "Animation program identity is invalid");
  }
  requireString(program.scene_revision, "animation program.scene_revision", { pattern: OPAQUE_ID_RE, max: 160, code });
  requireString(program.source_checksum, "animation program.source_checksum", { pattern: SHA256_RE, max: 64, code });
  requireString(program.profile_revision, "animation program.profile_revision", { pattern: SAFE_ID_RE, max: 120, code });
  requireString(program.content_revision, "animation program.content_revision", { pattern: OPAQUE_ID_RE, max: 160, code });
  requireString(program.registry_revision, "animation program.registry_revision", { pattern: SAFE_ID_RE, max: 120, code });
  if (!Number.isInteger(program.fps) || program.fps < 1 || program.fps > MAX_FPS || !Number.isFinite(program.duration_sec) || program.duration_sec <= 0 || program.duration_sec > 3600 || program.duration_frame !== toFrame(program.duration_sec, program.fps)) {
    fail(code, "Animation program timing is invalid");
  }
  if (!Array.isArray(program.events) || program.events.length === 0 || !Array.isArray(program.checkpoints) || program.checkpoints.length !== program.events.length + 1) {
    fail(code, "Animation program event/checkpoint collections are invalid");
  }
  let previousFrame = -1;
  let previousFrameOrder = -1;
  let previousSec = -1;
  let previousEventId = null;
  const eventIds = new Set();
  for (let index = 0; index < program.events.length; index += 1) {
    const event = program.events[index];
    const eventKeys = ["event_id", "order", "at_sec", "at_frame", "frame_order", "action", "actor_binding_id", "target_binding_id", "adapter_id", "bridge_action_id", "parameters"];
    exactKeys(event, eventKeys, eventKeys, `animation program.events[${index}]`, code);
    if (!EVENT_ID_RE.test(event.event_id) || eventIds.has(event.event_id) || event.order !== index) fail(code, "Animation program event identity/order is invalid");
    eventIds.add(event.event_id);
    if (!Number.isFinite(event.at_sec) || event.at_sec < 0 || event.at_sec > program.duration_sec || event.at_frame !== toFrame(event.at_sec, program.fps)) fail(code, "Animation program event timing is invalid");
    if (event.at_sec < previousSec || (event.at_sec === previousSec && previousEventId !== null && event.event_id.localeCompare(previousEventId) <= 0)) fail(code, "Animation program time/event ordering is invalid");
    if (event.at_frame < previousFrame || (event.at_frame === previousFrame && event.frame_order !== previousFrameOrder + 1) || (event.at_frame !== previousFrame && event.frame_order !== 0)) {
      fail(code, "Animation program frame ordering is invalid");
    }
    previousFrame = event.at_frame;
    previousFrameOrder = event.frame_order;
    previousSec = event.at_sec;
    previousEventId = event.event_id;
    const definition = ACTION_DEFINITIONS[event.action];
    if (!definition || event.adapter_id !== definition.adapter_id || event.bridge_action_id !== definition.bridge_action_id) fail(code, "Animation program action identity is invalid");
    requireString(event.actor_binding_id, `animation program.events[${index}].actor_binding_id`, { pattern: SAFE_ID_RE, max: 120, code });
    if (event.target_binding_id !== null) requireString(event.target_binding_id, `animation program.events[${index}].target_binding_id`, { pattern: SAFE_ID_RE, max: 120, code });
    if (definition.target_policy === "required" && event.target_binding_id === null) fail(code, "Animation program action requires a target");
    if (definition.target_policy === "forbidden" && event.target_binding_id !== null) fail(code, "Animation program action forbids a target");
    validateActionParameters(event.action, event.parameters);
  }
  const terminal = program.checkpoints.filter((entry) => entry && entry.kind === "terminal");
  if (terminal.length !== 1 || terminal[0].event_id !== null || terminal[0].at_sec !== program.duration_sec || terminal[0].at_frame !== program.duration_frame) {
    fail(code, "Animation program terminal checkpoint is invalid");
  }
  const checkpointIds = new Set();
  let previousCheckpoint = null;
  for (const checkpoint of program.checkpoints) {
    exactKeys(checkpoint, ["checkpoint_id", "kind", "event_id", "at_sec", "at_frame", "frame_order"], ["checkpoint_id", "kind", "event_id", "at_sec", "at_frame", "frame_order"], "animation program.checkpoint", code);
    if (!/^vac-[a-f0-9]{16}$/.test(checkpoint.checkpoint_id) || checkpointIds.has(checkpoint.checkpoint_id) || !new Set(["event", "terminal"]).has(checkpoint.kind)) fail(code, "Animation program checkpoint identity is invalid");
    checkpointIds.add(checkpoint.checkpoint_id);
    if (!Number.isFinite(checkpoint.at_sec) || checkpoint.at_sec < 0 || checkpoint.at_sec > program.duration_sec || checkpoint.at_frame !== toFrame(checkpoint.at_sec, program.fps) || !Number.isInteger(checkpoint.frame_order) || checkpoint.frame_order < 0) fail(code, "Animation program checkpoint timing is invalid");
    const tuple = [checkpoint.at_frame, checkpoint.frame_order, checkpoint.checkpoint_id];
    if (previousCheckpoint && (tuple[0] < previousCheckpoint[0] || (tuple[0] === previousCheckpoint[0] && tuple[1] < previousCheckpoint[1]))) fail(code, "Animation program checkpoints are not ordered");
    previousCheckpoint = tuple;
    if (checkpoint.kind === "event") {
      const event = program.events.find((candidate) => candidate.event_id === checkpoint.event_id);
      if (!event || checkpoint.at_sec !== event.at_sec || checkpoint.at_frame !== event.at_frame || checkpoint.frame_order !== event.frame_order) fail(code, "Animation checkpoint does not match its event");
    }
  }
  return program;
}

function createAdapterDescriptor(actionProfile, lifecycle) {
  const definition = ACTION_DEFINITIONS[actionProfile.action];
  return {
    schema: ACTION_ADAPTER_SCHEMA,
    adapter_id: definition.adapter_id,
    version: ADAPTER_VERSION,
    action: actionProfile.action,
    contract: {
      actor_kinds: [...definition.actor_kinds],
      target_policy: definition.target_policy,
      target_kinds: [...definition.target_kinds],
      required_actor_capabilities: [...definition.actor_capabilities],
      required_target_capabilities: [...definition.target_capabilities],
    },
    timeout_ms: actionProfile.timeout_ms,
    ...lifecycle,
  };
}

module.exports = {
  ACTION_DEFINITIONS,
  ADAPTER_VERSION,
  ANIMATION_CONTENT_PROFILE_SCHEMA,
  ANIMATION_PREFLIGHT_REQUEST_SCHEMA,
  ANIMATION_PREFLIGHT_RESPONSE_SCHEMA,
  ANIMATION_PREFLIGHT_SCHEMA,
  ANIMATION_PROGRAM_SCHEMA,
  CAPABILITY_REGISTRY_SCHEMA,
  DEFAULT_FPS,
  VistaAnimationContractError,
  buildAnimationPreflightArtifact,
  canonicalize,
  cloneJson,
  compileVistaAnimationProgram,
  createAdapterDescriptor,
  deepFreeze,
  digest,
  makeAnimationPreflightRequest,
  validateActionParameters,
  validateAnimationPreflightArtifact,
  validateAnimationPreflightResponse,
  validateContentProfile,
  validateVistaAnimationProgram,
};
