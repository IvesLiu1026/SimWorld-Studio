"use strict";

const bundledAgentRegistry = require("./agent-registry.json");

const {
  ACTION_DEFINITIONS,
  createAdapterDescriptor,
  deepFreeze,
  digest,
  validateAnimationPreflightArtifact,
  validateContentProfile,
} = require("./vista-animation-contract");
const { CAPABILITY_REGISTRY_SCHEMA } = require("./vista-timeline-compiler");

const AGENT_ACTION_AUDIT_SCHEMA = "vista-agent-action-audit/v1";
const AGENT_ACTION_AUTHORIZATION_SCHEMA = "vista-agent-action-authorization/v1";
const AGENT_ACTION_POLICY_SCHEMA = "vista-agent-action-adapter-policy/v1";
const LIFECYCLE_METHODS = Object.freeze([
  "precondition",
  "execute",
  "completion",
  "timeout",
  "cancel",
  "cleanup",
]);
const SAFE_ID_RE = /^[a-z][a-z0-9_-]{0,119}$/;

// This is intentionally the only legacy agent_action mapping. StopAction is a
// bounded candidate for VISTA's semantic pause action. It is not executable
// merely because it exists in agent-registry.json: a pinned content profile and
// live animation preflight still have to prove hold_pose, completion, timeout,
// and cleanup through the dedicated animation content API.
const LEGACY_HUMANOID_PAWN = "/Game/TrafficSystem/Pedestrian/Base_User_Agent.Base_User_Agent_C";
const RAW_SERVER_MAPPINGS = {
  pause: {
    semantic_action: "pause",
    source_agent_type: "humanoid",
    source_action: "stop_action",
    source_blueprint_path: LEGACY_HUMANOID_PAWN,
    source_definition: {
      cmd: "StopAction",
      params: [],
      defaults: [],
    },
    implementation_asset: LEGACY_HUMANOID_PAWN,
    completion_signal: "vista_pause_complete",
    max_timeout_ms: 10_000,
  },
};

const SERVER_MAPPINGS = deepFreeze(Object.fromEntries(
  Object.entries(RAW_SERVER_MAPPINGS).map(([action, mapping]) => {
    const definition = ACTION_DEFINITIONS[action];
    if (!definition) throw new Error(`Missing fixed VISTA action definition for '${action}'`);
    const identity = {
      semantic_action: mapping.semantic_action,
      source_agent_type: mapping.source_agent_type,
      source_action: mapping.source_action,
      source_blueprint_path: mapping.source_blueprint_path,
      source_definition: mapping.source_definition,
      implementation_asset: mapping.implementation_asset,
      completion_signal: mapping.completion_signal,
      max_timeout_ms: mapping.max_timeout_ms,
      adapter_id: definition.adapter_id,
      bridge_action_id: definition.bridge_action_id,
    };
    return [action, {
      ...identity,
      mapping_fingerprint: digest(identity),
    }];
  }),
));

class VistaAnimationAgentActionRegistryError extends Error {
  constructor(code, message, { status = 400, details = {} } = {}) {
    super(message);
    this.name = "VistaAnimationAgentActionRegistryError";
    this.code = code;
    this.status = status;
    this.retryable = false;
    this.details = sanitizeDetails(details);
  }
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
    if (/token|secret|password|credential|authorization|cookie|command|cmd|code|python|path/i.test(key)) continue;
    const normalized = sanitizeDetails(value[key], depth + 1);
    if (normalized !== undefined) output[key] = normalized;
  }
  return output;
}

function fail(code, message, options) {
  throw new VistaAnimationAgentActionRegistryError(code, message, options);
}

function exactKeys(value, expected, pointer, code = "ANIMATION_AGENT_ACTION_INPUT_INVALID") {
  if (!isPlainObject(value)) fail(code, `${pointer} must be an object`);
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  if (actual.length !== wanted.length || actual.some((key, index) => key !== wanted[index])) {
    fail(code, `${pointer} has an invalid shape`, {
      details: {
        pointer,
        unknown: actual.filter((key) => !wanted.includes(key)),
        missing: wanted.filter((key) => !actual.includes(key)),
      },
    });
  }
}

function requireString(value, pointer, pattern = SAFE_ID_RE, max = 160, code = "ANIMATION_AGENT_ACTION_INPUT_INVALID") {
  if (typeof value !== "string" || value.length === 0 || value.length > max || (pattern && !pattern.test(value))) {
    fail(code, `${pointer} is invalid`, { details: { pointer } });
  }
  return value;
}

function normalizeSourceDefinition(actionDefinition) {
  if (!isPlainObject(actionDefinition) || typeof actionDefinition.cmd !== "string") return null;
  const params = actionDefinition.params === undefined ? [] : actionDefinition.params;
  const defaults = actionDefinition.defaults === undefined ? [] : actionDefinition.defaults;
  if (!Array.isArray(params) || !Array.isArray(defaults)) return null;
  return {
    cmd: actionDefinition.cmd,
    params: [...params],
    defaults: [...defaults],
  };
}

function auditVistaAgentActionSource() {
  const supportedMappings = [];
  const issues = [];
  for (const mapping of Object.values(SERVER_MAPPINGS)) {
    const agentType = bundledAgentRegistry
      && bundledAgentRegistry.agentTypes
      && bundledAgentRegistry.agentTypes[mapping.source_agent_type];
    const sourceDefinition = agentType
      && agentType.actions
      && agentType.actions[mapping.source_action];
    const normalizedSource = normalizeSourceDefinition(sourceDefinition);
    const sourceMatches = Boolean(
      agentType
      && agentType.blueprintPath === mapping.source_blueprint_path
      && normalizedSource
      && digest(normalizedSource) === digest(mapping.source_definition),
    );
    if (!sourceMatches) {
      issues.push({
        action: mapping.semantic_action,
        code: "AGENT_ACTION_SOURCE_DRIFT",
        subject: `${mapping.source_agent_type}.${mapping.source_action}`,
      });
      continue;
    }
    supportedMappings.push({
      semantic_action: mapping.semantic_action,
      source_agent_type: mapping.source_agent_type,
      source_action: mapping.source_action,
      adapter_id: mapping.adapter_id,
      bridge_action_id: mapping.bridge_action_id,
      mapping_fingerprint: mapping.mapping_fingerprint,
    });
  }
  supportedMappings.sort((left, right) => left.semantic_action.localeCompare(right.semantic_action));
  issues.sort((left, right) => left.action.localeCompare(right.action));
  const body = { supported_mappings: supportedMappings, issues };
  return deepFreeze({
    schema: AGENT_ACTION_AUDIT_SCHEMA,
    audit_revision: `agent_action_audit_${digest(body).slice(0, 16)}`,
    ready: issues.length === 0,
    ...body,
  });
}

function isVistaLegacyAgentActionProfile(contentProfile) {
  return Boolean(
    isPlainObject(contentProfile)
    && contentProfile.pawn_class_path === LEGACY_HUMANOID_PAWN,
  );
}

function validateLifecycle(lifecycle) {
  exactKeys(
    lifecycle,
    LIFECYCLE_METHODS,
    "agent action lifecycle",
    "ANIMATION_AGENT_ACTION_LIFECYCLE_INVALID",
  );
  for (const method of LIFECYCLE_METHODS) {
    if (typeof lifecycle[method] !== "function") {
      fail("ANIMATION_AGENT_ACTION_LIFECYCLE_INVALID", `agent action lifecycle.${method} must be a function`, { status: 500 });
    }
  }
  return lifecycle;
}

function createVistaAnimationAgentActionPolicy(options = {}) {
  exactKeys(options, ["contentProfile"], "agent action policy options", "ANIMATION_AGENT_ACTION_CONFIG_INVALID");
  let profile;
  try {
    profile = validateContentProfile(options.contentProfile);
  } catch {
    fail("ANIMATION_AGENT_ACTION_CONFIG_INVALID", "A verified animation content profile is required", { status: 500 });
  }
  if (!isVistaLegacyAgentActionProfile(profile)) {
    fail("ANIMATION_AGENT_ACTION_PROFILE_MISMATCH", "Content profile is not bound to the server-owned legacy humanoid pawn", { status: 409 });
  }
  const audit = auditVistaAgentActionSource();
  if (!audit.ready) {
    fail("ANIMATION_AGENT_ACTION_SOURCE_DRIFT", "Bundled agent_action registry does not match the pinned server mapping", {
      status: 500,
      details: { issues: audit.issues },
    });
  }

  const actionProfiles = new Map(profile.actions.map((entry) => [entry.action, entry]));
  const supportedActions = [];
  for (const [action, mapping] of Object.entries(SERVER_MAPPINGS)) {
    const actionProfile = actionProfiles.get(action);
    if (!actionProfile) continue;
    if (
      actionProfile.adapter_id !== mapping.adapter_id
      || actionProfile.bridge_action_id !== mapping.bridge_action_id
      || actionProfile.implementation_asset !== mapping.implementation_asset
      || actionProfile.completion_signal !== mapping.completion_signal
      || actionProfile.timeout_ms > mapping.max_timeout_ms
    ) {
      fail("ANIMATION_AGENT_ACTION_PROFILE_MISMATCH", `Content profile action '${action}' is not bound to the server-owned implementation`, {
        status: 409,
        details: { action },
      });
    }
    supportedActions.push(action);
  }
  supportedActions.sort();
  const mappingRevision = `agent_action_${digest({
    audit_revision: audit.audit_revision,
    profile_id: profile.profile_id,
    profile_revision: profile.revision,
    content_digest: profile.content_digest,
    actions: supportedActions.map((action) => SERVER_MAPPINGS[action].mapping_fingerprint),
  }).slice(0, 16)}`;

  function authorize(request) {
    exactKeys(
      request,
      ["schema", "profile_id", "profile_revision", "content_digest", "requested_actions"],
      "agent action authorization",
    );
    if (
      request.schema !== AGENT_ACTION_AUTHORIZATION_SCHEMA
      || request.profile_id !== profile.profile_id
      || request.profile_revision !== profile.revision
      || request.content_digest !== profile.content_digest
    ) {
      fail("ANIMATION_AGENT_ACTION_PROFILE_MISMATCH", "Agent action authorization does not match the pinned content profile", { status: 409 });
    }
    if (!Array.isArray(request.requested_actions) || request.requested_actions.length === 0 || request.requested_actions.length > 128) {
      fail("ANIMATION_AGENT_ACTION_INPUT_INVALID", "requested_actions must be a bounded non-empty array");
    }
    const requested = request.requested_actions.map((action, index) => (
      requireString(action, `requested_actions[${index}]`)
    )).sort();
    if (new Set(requested).size !== requested.length) {
      fail("ANIMATION_AGENT_ACTION_INPUT_INVALID", "requested_actions must be unique");
    }
    const unsupported = requested.filter((action) => !supportedActions.includes(action));
    if (unsupported.length) {
      fail("ANIMATION_AGENT_ACTION_UNSUPPORTED", "Legacy agent_action has no verified typed adapter for one or more requested actions", {
        status: 409,
        details: { unsupported },
      });
    }
    return deepFreeze({
      schema: AGENT_ACTION_AUTHORIZATION_SCHEMA,
      policy_revision: mappingRevision,
      requested_actions: requested,
    });
  }

  function createCapabilityRegistry(request) {
    exactKeys(
      request,
      ["artifact", "lifecycleFactory"],
      "agent action capability registry request",
      "ANIMATION_AGENT_ACTION_CONFIG_INVALID",
    );
    if (typeof request.lifecycleFactory !== "function") {
      fail("ANIMATION_AGENT_ACTION_CONFIG_INVALID", "lifecycleFactory must be a function", { status: 500 });
    }
    let artifact;
    try {
      artifact = validateAnimationPreflightArtifact(request.artifact);
    } catch {
      fail("ANIMATION_AGENT_ACTION_PREFLIGHT_MISMATCH", "Animation preflight artifact failed exact validation", { status: 409 });
    }
    authorize({
      schema: AGENT_ACTION_AUTHORIZATION_SCHEMA,
      profile_id: artifact.profile_id,
      profile_revision: artifact.profile_revision,
      content_digest: artifact.content_digest,
      requested_actions: artifact.requested_actions,
    });
    const adapters = artifact.supported_actions.map((action) => {
      const actionProfile = actionProfiles.get(action);
      const definition = ACTION_DEFINITIONS[action];
      const capableActor = artifact.actors.some((actor) => (
        actor.ready
        && definition.actor_capabilities.every((capability) => actor.capabilities.includes(capability))
      ));
      if (!capableActor) {
        fail("ANIMATION_AGENT_ACTION_CAPABILITY_MISMATCH", `No verified actor can execute '${action}'`, {
          status: 409,
          details: { action },
        });
      }
      const lifecycle = validateLifecycle(request.lifecycleFactory(action));
      return createAdapterDescriptor(actionProfile, lifecycle);
    });
    return Object.freeze({
      schema: CAPABILITY_REGISTRY_SCHEMA,
      revision: artifact.registry_revision,
      adapters: Object.freeze(adapters),
    });
  }

  return Object.freeze({
    schema: AGENT_ACTION_POLICY_SCHEMA,
    registry_binding: mappingRevision,
    profile_id: profile.profile_id,
    profile_revision: profile.revision,
    content_digest: profile.content_digest,
    supported_actions: Object.freeze([...supportedActions]),
    authorize,
    createCapabilityRegistry,
  });
}

module.exports = {
  AGENT_ACTION_AUDIT_SCHEMA,
  AGENT_ACTION_AUTHORIZATION_SCHEMA,
  AGENT_ACTION_POLICY_SCHEMA,
  LEGACY_HUMANOID_PAWN,
  SERVER_MAPPINGS,
  VistaAnimationAgentActionRegistryError,
  auditVistaAgentActionSource,
  createVistaAnimationAgentActionPolicy,
  isVistaLegacyAgentActionProfile,
};
