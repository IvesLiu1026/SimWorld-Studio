"use strict";

const crypto = require("node:crypto");

const TIMELINE_SCHEMA = "vista-timeline/v1";
const TIMELINE_RUN_SCHEMA = "vista-timeline-run/v1";
const BINDINGS_SCHEMA = "vista-timeline-bindings/v1";
const CAPABILITY_REGISTRY_SCHEMA = "vista-action-capability-registry/v1";
const ACTION_ADAPTER_SCHEMA = "vista-action-adapter/v1";
const COMPILER_NAME = "vista-timeline-compiler";
const COMPILER_VERSION = "1.0.0";
const ACTION_ADAPTER_METHODS = Object.freeze([
  "precondition",
  "execute",
  "completion",
  "timeout",
  "cancel",
  "cleanup",
]);

const SAFE_ID_RE = /^[a-z][a-z0-9_-]{0,119}$/;
const EVENT_ID_RE = /^beat-[0-9]{4}(?:-[a-z0-9_-]+)?$/;
const JSON_POINTER_RE = /^\/(?:[^/~]|~[01])+(?:\/(?:[^/~]|~[01])+)*$/;
const SHA256_RE = /^[a-f0-9]{64}$/;
const SEMVER_RE = /^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$/;
const POLICIES = new Set(["strict", "lenient"]);
const TARGET_POLICIES = new Set(["required", "optional", "forbidden"]);
const EVENT_STATUSES = new Set(["ready", "unbound", "unsupported", "incompatible", "ambiguous"]);
const DISPOSITIONS = new Set(["execute", "block", "skip"]);
const MAX_DURATION_SEC = 3600;
const MAX_EVENTS = 1000;

class VistaTimelineCompileError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "VistaTimelineCompileError";
    this.code = code;
    this.details = sanitizeDetails(details);
    this.status = 400;
    this.retryable = false;
  }
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
    const sanitized = sanitizeDetails(value[key], depth + 1);
    if (sanitized !== undefined) output[key] = sanitized;
  }
  return output;
}

function fail(code, message, details) {
  throw new VistaTimelineCompileError(code, message, details);
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function exactKeys(value, allowed, required, pointer, code) {
  if (!isPlainObject(value)) fail(code, `${pointer} must be an object`);
  const allowedSet = new Set(allowed);
  const unknown = Object.keys(value).filter((key) => !allowedSet.has(key));
  const missing = required.filter((key) => !Object.prototype.hasOwnProperty.call(value, key));
  if (unknown.length || missing.length) {
    fail(code, `${pointer} has an invalid shape`, { pointer, unknown, missing });
  }
  return value;
}

function requireString(value, pointer, { pattern = null, max = 240, code = "TIMELINE_INPUT_INVALID" } = {}) {
  if (typeof value !== "string" || !value.length || value.length > max || (pattern && !pattern.test(value))) {
    fail(code, `${pointer} is invalid`, { pointer });
  }
  return value;
}

function requireFiniteNumber(value, pointer, { min = -Infinity, max = Infinity, code = "TIMELINE_INPUT_INVALID" } = {}) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < min || value > max) {
    fail(code, `${pointer} must be a finite number in range`, { pointer, min, max });
  }
  return value;
}

function requireUniqueStrings(value, pointer, { allowEmpty = true, code = "TIMELINE_INPUT_INVALID" } = {}) {
  if (!Array.isArray(value) || (!allowEmpty && value.length === 0) || value.length > 128) {
    fail(code, `${pointer} must be a bounded string array`, { pointer });
  }
  const seen = new Set();
  for (let index = 0; index < value.length; index += 1) {
    const item = requireString(value[index], `${pointer}[${index}]`, { pattern: SAFE_ID_RE, max: 120, code });
    if (seen.has(item)) fail(code, `${pointer} contains a duplicate`, { pointer, item });
    seen.add(item);
  }
  return [...seen].sort();
}

function cloneJson(value, pointer = "value", depth = 0) {
  if (depth > 8) fail("TIMELINE_INPUT_INVALID", `${pointer} exceeds the maximum nesting depth`, { pointer });
  if (value === null || typeof value === "boolean" || typeof value === "string") return value;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (Array.isArray(value)) {
    if (value.length > 128) fail("TIMELINE_INPUT_INVALID", `${pointer} is too large`, { pointer });
    return value.map((item, index) => cloneJson(item, `${pointer}[${index}]`, depth + 1));
  }
  if (!isPlainObject(value)) fail("TIMELINE_INPUT_INVALID", `${pointer} must contain JSON-safe values`, { pointer });
  const keys = Object.keys(value);
  if (keys.length > 128) fail("TIMELINE_INPUT_INVALID", `${pointer} has too many properties`, { pointer });
  const output = {};
  for (const key of keys.sort()) {
    if (["__proto__", "constructor", "prototype"].includes(key)) {
      fail("TIMELINE_INPUT_INVALID", `${pointer} contains an unsafe key`, { pointer });
    }
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

function deepFreeze(value) {
  if (!value || typeof value !== "object" || Object.isFrozen(value)) return value;
  Object.freeze(value);
  for (const child of Object.values(value)) deepFreeze(child);
  return value;
}

function validateBinding(binding, pointer) {
  exactKeys(
    binding,
    ["source_id", "binding_id", "kind", "capabilities"],
    ["source_id", "binding_id", "kind", "capabilities"],
    pointer,
    "TIMELINE_BINDINGS_INVALID",
  );
  const sourceId = requireString(binding.source_id, `${pointer}.source_id`, { pattern: SAFE_ID_RE, max: 120, code: "TIMELINE_BINDINGS_INVALID" });
  const bindingId = requireString(binding.binding_id, `${pointer}.binding_id`, { pattern: SAFE_ID_RE, max: 120, code: "TIMELINE_BINDINGS_INVALID" });
  const kind = requireString(binding.kind, `${pointer}.kind`, { pattern: SAFE_ID_RE, max: 120, code: "TIMELINE_BINDINGS_INVALID" });
  const capabilities = requireUniqueStrings(binding.capabilities, `${pointer}.capabilities`, { code: "TIMELINE_BINDINGS_INVALID" });
  return { source_id: sourceId, binding_id: bindingId, kind, capabilities };
}

function validateBindings(bindings) {
  exactKeys(
    bindings,
    ["schema", "revision", "actors", "entities"],
    ["schema", "revision", "actors", "entities"],
    "bindings",
    "TIMELINE_BINDINGS_INVALID",
  );
  if (bindings.schema !== BINDINGS_SCHEMA) fail("TIMELINE_BINDINGS_INVALID", "Unsupported bindings schema");
  const revision = requireString(bindings.revision, "bindings.revision", { pattern: SAFE_ID_RE, max: 120, code: "TIMELINE_BINDINGS_INVALID" });
  if (!Array.isArray(bindings.actors) || !Array.isArray(bindings.entities)) {
    fail("TIMELINE_BINDINGS_INVALID", "bindings.actors and bindings.entities must be arrays");
  }
  if (bindings.actors.length > 256 || bindings.entities.length > 1000) {
    fail("TIMELINE_BINDINGS_INVALID", "Bindings exceed the configured limits");
  }
  const seenBindingIds = new Set();
  const normalizeGroup = (group, name) => {
    const seenSourceIds = new Set();
    const normalized = group.map((binding, index) => validateBinding(binding, `bindings.${name}[${index}]`));
    for (const binding of normalized) {
      if (seenSourceIds.has(binding.source_id)) {
        fail("TIMELINE_BINDINGS_INVALID", `bindings.${name} contains duplicate source ids`, { source_id: binding.source_id });
      }
      if (seenBindingIds.has(binding.binding_id)) {
        fail("TIMELINE_BINDINGS_INVALID", "binding_id must be globally unique", { binding_id: binding.binding_id });
      }
      seenSourceIds.add(binding.source_id);
      seenBindingIds.add(binding.binding_id);
    }
    return normalized.sort((left, right) => left.source_id.localeCompare(right.source_id));
  };
  return deepFreeze({
    schema: BINDINGS_SCHEMA,
    revision,
    actors: normalizeGroup(bindings.actors, "actors"),
    entities: normalizeGroup(bindings.entities, "entities"),
  });
}

function validateActionAdapter(adapter, pointer = "adapter") {
  exactKeys(
    adapter,
    [
      "schema", "adapter_id", "version", "action", "contract", "timeout_ms",
      ...ACTION_ADAPTER_METHODS,
    ],
    [
      "schema", "adapter_id", "version", "action", "contract", "timeout_ms",
      ...ACTION_ADAPTER_METHODS,
    ],
    pointer,
    "TIMELINE_ADAPTER_INVALID",
  );
  if (adapter.schema !== ACTION_ADAPTER_SCHEMA) fail("TIMELINE_ADAPTER_INVALID", `${pointer}.schema is unsupported`);
  const adapterId = requireString(adapter.adapter_id, `${pointer}.adapter_id`, { pattern: SAFE_ID_RE, max: 120, code: "TIMELINE_ADAPTER_INVALID" });
  const version = requireString(adapter.version, `${pointer}.version`, { pattern: SEMVER_RE, max: 64, code: "TIMELINE_ADAPTER_INVALID" });
  const action = requireString(adapter.action, `${pointer}.action`, { pattern: SAFE_ID_RE, max: 120, code: "TIMELINE_ADAPTER_INVALID" });
  if (!Number.isInteger(adapter.timeout_ms) || adapter.timeout_ms < 1 || adapter.timeout_ms > 600_000) {
    fail("TIMELINE_ADAPTER_INVALID", `${pointer}.timeout_ms is invalid`);
  }
  exactKeys(
    adapter.contract,
    ["actor_kinds", "target_policy", "target_kinds", "required_actor_capabilities", "required_target_capabilities"],
    ["actor_kinds", "target_policy", "target_kinds", "required_actor_capabilities", "required_target_capabilities"],
    `${pointer}.contract`,
    "TIMELINE_ADAPTER_INVALID",
  );
  const actorKinds = requireUniqueStrings(adapter.contract.actor_kinds, `${pointer}.contract.actor_kinds`, { allowEmpty: false, code: "TIMELINE_ADAPTER_INVALID" });
  const targetPolicy = adapter.contract.target_policy;
  if (!TARGET_POLICIES.has(targetPolicy)) fail("TIMELINE_ADAPTER_INVALID", `${pointer}.contract.target_policy is invalid`);
  const targetKinds = requireUniqueStrings(adapter.contract.target_kinds, `${pointer}.contract.target_kinds`, { code: "TIMELINE_ADAPTER_INVALID" });
  if (targetPolicy === "required" && targetKinds.length === 0) {
    fail("TIMELINE_ADAPTER_INVALID", `${pointer} requires at least one target kind`);
  }
  if (targetPolicy === "forbidden" && targetKinds.length !== 0) {
    fail("TIMELINE_ADAPTER_INVALID", `${pointer} forbids targets but declares target kinds`);
  }
  const requiredActorCapabilities = requireUniqueStrings(
    adapter.contract.required_actor_capabilities,
    `${pointer}.contract.required_actor_capabilities`,
    { code: "TIMELINE_ADAPTER_INVALID" },
  );
  const requiredTargetCapabilities = requireUniqueStrings(
    adapter.contract.required_target_capabilities,
    `${pointer}.contract.required_target_capabilities`,
    { code: "TIMELINE_ADAPTER_INVALID" },
  );
  if (targetPolicy === "forbidden" && requiredTargetCapabilities.length !== 0) {
    fail("TIMELINE_ADAPTER_INVALID", `${pointer} forbids targets but declares target capabilities`);
  }
  for (const method of ACTION_ADAPTER_METHODS) {
    if (typeof adapter[method] !== "function") {
      fail("TIMELINE_ADAPTER_INVALID", `${pointer}.${method} must be a function`, { method });
    }
  }
  return Object.freeze({
    adapter_id: adapterId,
    version,
    action,
    timeout_ms: adapter.timeout_ms,
    contract: Object.freeze({
      actor_kinds: Object.freeze(actorKinds),
      target_policy: targetPolicy,
      target_kinds: Object.freeze(targetKinds),
      required_actor_capabilities: Object.freeze(requiredActorCapabilities),
      required_target_capabilities: Object.freeze(requiredTargetCapabilities),
    }),
    implementation: Object.freeze(Object.fromEntries(
      ACTION_ADAPTER_METHODS.map((method) => [method, adapter[method]]),
    )),
  });
}

function validateCapabilityRegistry(registry) {
  exactKeys(
    registry,
    ["schema", "revision", "adapters"],
    ["schema", "revision", "adapters"],
    "capabilityRegistry",
    "TIMELINE_CAPABILITY_REGISTRY_INVALID",
  );
  if (registry.schema !== CAPABILITY_REGISTRY_SCHEMA) {
    fail("TIMELINE_CAPABILITY_REGISTRY_INVALID", "Unsupported capability registry schema");
  }
  const revision = requireString(registry.revision, "capabilityRegistry.revision", { pattern: SAFE_ID_RE, max: 120, code: "TIMELINE_CAPABILITY_REGISTRY_INVALID" });
  if (!Array.isArray(registry.adapters) || registry.adapters.length > 256) {
    fail("TIMELINE_CAPABILITY_REGISTRY_INVALID", "capabilityRegistry.adapters must be a bounded array");
  }
  const ids = new Set();
  const adapters = registry.adapters.map((adapter, index) => validateActionAdapter(adapter, `capabilityRegistry.adapters[${index}]`));
  for (const adapter of adapters) {
    if (ids.has(adapter.adapter_id)) {
      fail("TIMELINE_CAPABILITY_REGISTRY_INVALID", "adapter_id must be unique", { adapter_id: adapter.adapter_id });
    }
    ids.add(adapter.adapter_id);
  }
  adapters.sort((left, right) => left.adapter_id.localeCompare(right.adapter_id));
  return Object.freeze({ schema: CAPABILITY_REGISTRY_SCHEMA, revision, adapters: Object.freeze(adapters) });
}

function validateSceneInput(scene) {
  if (!isPlainObject(scene) || scene.schema !== "vista-simworld-scene/v1") {
    fail("TIMELINE_SCENE_INVALID", "Expected a vista-simworld-scene/v1 SceneSpec");
  }
  const sceneId = requireString(scene.scene_id, "scene.scene_id", { max: 256, code: "TIMELINE_SCENE_INVALID" });
  const durationSec = requireFiniteNumber(scene.duration_sec, "scene.duration_sec", { min: 0.001, max: MAX_DURATION_SEC, code: "TIMELINE_SCENE_INVALID" });
  const sourceChecksum = scene.source && scene.source.source_checksum;
  if (typeof sourceChecksum !== "string" || !SHA256_RE.test(sourceChecksum)) {
    fail("TIMELINE_SCENE_INVALID", "scene.source.source_checksum is invalid");
  }
  if (!Array.isArray(scene.entities) || scene.entities.length > 1000) {
    fail("TIMELINE_SCENE_INVALID", "scene.entities must be a bounded array");
  }
  const entityIds = new Set();
  for (let index = 0; index < scene.entities.length; index += 1) {
    const entity = scene.entities[index];
    if (!isPlainObject(entity)) fail("TIMELINE_SCENE_INVALID", `scene.entities[${index}] must be an object`);
    const entityId = requireString(entity.id, `scene.entities[${index}].id`, { pattern: SAFE_ID_RE, max: 120, code: "TIMELINE_SCENE_INVALID" });
    if (entityIds.has(entityId)) fail("TIMELINE_SCENE_INVALID", "Scene entity ids must be unique", { entity_id: entityId });
    entityIds.add(entityId);
  }
  if (!Array.isArray(scene.timeline) || scene.timeline.length === 0 || scene.timeline.length > MAX_EVENTS) {
    fail("TIMELINE_SCENE_INVALID", "scene.timeline must contain a bounded set of events");
  }
  const eventIds = new Set();
  const events = scene.timeline.map((event, index) => {
    const pointer = `scene.timeline[${index}]`;
    if (!isPlainObject(event)) fail("TIMELINE_SCENE_INVALID", `${pointer} must be an object`);
    const eventId = requireString(event.event_id, `${pointer}.event_id`, { pattern: EVENT_ID_RE, max: 120, code: "TIMELINE_SCENE_INVALID" });
    if (eventIds.has(eventId)) fail("TIMELINE_SCENE_INVALID", "Timeline event ids must be unique", { event_id: eventId });
    eventIds.add(eventId);
    const atSec = requireFiniteNumber(event.at_sec, `${pointer}.at_sec`, { min: 0, max: durationSec, code: "TIMELINE_SCENE_INVALID" });
    const action = requireString(event.action, `${pointer}.action`, { pattern: SAFE_ID_RE, max: 120, code: "TIMELINE_SCENE_INVALID" });
    const actorId = requireString(event.actor_id, `${pointer}.actor_id`, { pattern: SAFE_ID_RE, max: 120, code: "TIMELINE_SCENE_INVALID" });
    let targetId = null;
    if (event.target_id !== null && event.target_id !== undefined) {
      targetId = requireString(event.target_id, `${pointer}.target_id`, { pattern: SAFE_ID_RE, max: 120, code: "TIMELINE_SCENE_INVALID" });
      if (!entityIds.has(targetId)) {
        fail("TIMELINE_SCENE_INVALID", "Timeline target_id does not reference a scene entity", { event_id: eventId, target_id: targetId });
      }
    }
    const parameters = cloneJson(event.parameters || {}, `${pointer}.parameters`);
    const sourcePointer = requireString(event.source_pointer, `${pointer}.source_pointer`, { pattern: JSON_POINTER_RE, max: 512, code: "TIMELINE_SCENE_INVALID" });
    return {
      event_id: eventId,
      at_sec: atSec,
      action,
      actor_id: actorId,
      target_id: targetId,
      parameters,
      source_pointer: sourcePointer,
    };
  });
  events.sort((left, right) => left.at_sec - right.at_sec || left.event_id.localeCompare(right.event_id));
  return Object.freeze({ scene_id: sceneId, source_checksum: sourceChecksum, duration_sec: durationSec, entity_ids: entityIds, events });
}

function includesAll(actual, required) {
  const values = new Set(actual);
  return required.every((value) => values.has(value));
}

function compatibility(adapter, actorBinding, targetBinding, hasTarget) {
  const contract = adapter.contract;
  if (!contract.actor_kinds.includes(actorBinding.kind)) return false;
  if (!includesAll(actorBinding.capabilities, contract.required_actor_capabilities)) return false;
  if (contract.target_policy === "required" && !hasTarget) return false;
  if (contract.target_policy === "forbidden" && hasTarget) return false;
  if (hasTarget) {
    if (!targetBinding) return false;
    if (!contract.target_kinds.includes(targetBinding.kind)) return false;
    if (!includesAll(targetBinding.capabilities, contract.required_target_capabilities)) return false;
  }
  return true;
}

function makeIssue(event, code, message, policy, candidates = []) {
  const normalizedCandidates = [...new Set(candidates)].sort();
  return {
    issue_id: `vti-${digest({ event_id: event ? event.event_id : null, code, candidates: normalizedCandidates }).slice(0, 16)}`,
    event_id: event ? event.event_id : null,
    code,
    severity: policy === "lenient" && event ? "warning" : "error",
    message,
    candidates: normalizedCandidates,
  };
}

function compileVistaTimeline(sceneSpec, options = {}) {
  exactKeys(
    options,
    ["policy", "bindings", "capabilityRegistry"],
    ["bindings", "capabilityRegistry"],
    "options",
    "TIMELINE_INPUT_INVALID",
  );
  const policy = options.policy === undefined ? "strict" : options.policy;
  if (!POLICIES.has(policy)) fail("TIMELINE_POLICY_INVALID", "Timeline policy must be strict or lenient", { policy });
  const scene = validateSceneInput(sceneSpec);
  const bindings = validateBindings(options.bindings);
  const registry = validateCapabilityRegistry(options.capabilityRegistry);

  for (const binding of bindings.entities) {
    if (!scene.entity_ids.has(binding.source_id)) {
      fail("TIMELINE_BINDINGS_INVALID", "Entity binding does not reference a SceneSpec entity", { source_id: binding.source_id });
    }
  }

  const actors = new Map(bindings.actors.map((binding) => [binding.source_id, binding]));
  const targets = new Map(bindings.entities.map((binding) => [binding.source_id, binding]));
  const adaptersByAction = new Map();
  for (const adapter of registry.adapters) {
    const group = adaptersByAction.get(adapter.action) || [];
    group.push(adapter);
    adaptersByAction.set(adapter.action, group);
  }

  const issues = [];
  const events = scene.events.map((event) => {
    const actorBinding = actors.get(event.actor_id) || null;
    const targetBinding = event.target_id ? targets.get(event.target_id) || null : null;
    const exactActionAdapters = adaptersByAction.get(event.action) || [];
    let status = "ready";
    let selected = null;
    let issue = null;

    if (!actorBinding) {
      status = "unbound";
      issue = makeIssue(event, "ACTOR_BINDING_MISSING", `Actor '${event.actor_id}' has no verified binding`, policy);
    } else if (event.target_id && !targetBinding) {
      status = "unbound";
      issue = makeIssue(event, "TARGET_BINDING_MISSING", `Target '${event.target_id}' has no verified binding`, policy);
    } else if (exactActionAdapters.length === 0) {
      status = "unsupported";
      issue = makeIssue(event, "ACTION_UNSUPPORTED", `Action '${event.action}' has no fixed verified adapter`, policy);
    } else {
      const compatible = exactActionAdapters.filter((adapter) => compatibility(
        adapter,
        actorBinding,
        targetBinding,
        event.target_id !== null,
      ));
      if (compatible.length === 0) {
        status = "incompatible";
        issue = makeIssue(
          event,
          "ACTION_ADAPTER_INCOMPATIBLE",
          `No '${event.action}' adapter satisfies the verified actor/target capabilities`,
          policy,
          exactActionAdapters.map((adapter) => adapter.adapter_id),
        );
      } else if (compatible.length > 1) {
        status = "ambiguous";
        issue = makeIssue(
          event,
          "ACTION_ADAPTER_AMBIGUOUS",
          `Action '${event.action}' matches more than one fixed adapter`,
          policy,
          compatible.map((adapter) => adapter.adapter_id),
        );
      } else {
        [selected] = compatible;
      }
    }

    if (issue) issues.push(issue);
    const disposition = status === "ready" ? "execute" : policy === "strict" ? "block" : "skip";
    return {
      event_id: event.event_id,
      at_sec: event.at_sec,
      action: event.action,
      actor_id: event.actor_id,
      target_id: event.target_id,
      parameters: event.parameters,
      source_pointer: event.source_pointer,
      actor_binding_id: actorBinding ? actorBinding.binding_id : null,
      target_binding_id: targetBinding ? targetBinding.binding_id : null,
      adapter: selected ? {
        adapter_id: selected.adapter_id,
        version: selected.version,
        timeout_ms: selected.timeout_ms,
      } : null,
      preflight_status: status,
      disposition,
      issue_ids: issue ? [issue.issue_id] : [],
    };
  });

  const ready = events.filter((event) => event.preflight_status === "ready").length;
  const skipped = events.filter((event) => event.disposition === "skip").length;
  const blocked = events.filter((event) => event.disposition === "block").length;
  let startAllowed = policy === "strict" ? blocked === 0 : ready > 0;
  if (ready === 0) {
    const globalIssue = makeIssue(null, "NO_EXECUTABLE_EVENTS", "Timeline has no executable events after preflight", "strict");
    issues.push(globalIssue);
    startAllowed = false;
  }
  issues.sort((left, right) => (left.event_id || "").localeCompare(right.event_id || "") || left.code.localeCompare(right.code));
  const normalizedBindings = {
    actors: bindings.actors.map((binding) => ({ ...binding })),
    entities: bindings.entities.map((binding) => ({ ...binding })),
  };
  const compiler = { name: COMPILER_NAME, version: COMPILER_VERSION };
  const summary = {
    total: events.length,
    ready,
    skipped,
    blocked,
    errors: issues.filter((issue) => issue.severity === "error").length,
    warnings: issues.filter((issue) => issue.severity === "warning").length,
  };
  const identity = {
    scene_id: scene.scene_id,
    source_checksum: scene.source_checksum,
    duration_sec: scene.duration_sec,
    policy,
    registry_revision: registry.revision,
    binding_revision: bindings.revision,
    bindings: normalizedBindings,
    events,
    issues,
    compiler,
  };
  const artifact = {
    schema: TIMELINE_SCHEMA,
    timeline_id: `vtl-${digest(identity).slice(0, 24)}`,
    scene_id: scene.scene_id,
    scene_revision: scene.scene_id,
    source_checksum: scene.source_checksum,
    duration_sec: scene.duration_sec,
    policy,
    registry_revision: registry.revision,
    binding_revision: bindings.revision,
    compiler,
    start_allowed: startAllowed,
    summary,
    bindings: normalizedBindings,
    events,
    issues,
  };
  return deepFreeze(validateCompiledTimeline(artifact));
}

function validateCompiledTimeline(timeline) {
  const keys = [
    "schema", "timeline_id", "scene_id", "scene_revision", "source_checksum", "duration_sec",
    "policy", "registry_revision", "binding_revision", "compiler", "start_allowed", "summary",
    "bindings", "events", "issues",
  ];
  exactKeys(timeline, keys, keys, "timeline", "TIMELINE_ARTIFACT_INVALID");
  if (timeline.schema !== TIMELINE_SCHEMA || !/^vtl-[a-f0-9]{24}$/.test(timeline.timeline_id)) {
    fail("TIMELINE_ARTIFACT_INVALID", "Compiled timeline identity is invalid");
  }
  if (!POLICIES.has(timeline.policy) || !SHA256_RE.test(timeline.source_checksum)) {
    fail("TIMELINE_ARTIFACT_INVALID", "Compiled timeline provenance is invalid");
  }
  requireFiniteNumber(timeline.duration_sec, "timeline.duration_sec", { min: 0.001, max: MAX_DURATION_SEC, code: "TIMELINE_ARTIFACT_INVALID" });
  if (!Array.isArray(timeline.events) || !Array.isArray(timeline.issues) || typeof timeline.start_allowed !== "boolean") {
    fail("TIMELINE_ARTIFACT_INVALID", "Compiled timeline collections are invalid");
  }
  exactKeys(timeline.compiler, ["name", "version"], ["name", "version"], "timeline.compiler", "TIMELINE_ARTIFACT_INVALID");
  if (timeline.compiler.name !== COMPILER_NAME || !SEMVER_RE.test(timeline.compiler.version)) {
    fail("TIMELINE_ARTIFACT_INVALID", "Compiled timeline compiler metadata is invalid");
  }
  exactKeys(timeline.summary, ["total", "ready", "skipped", "blocked", "errors", "warnings"], ["total", "ready", "skipped", "blocked", "errors", "warnings"], "timeline.summary", "TIMELINE_ARTIFACT_INVALID");
  exactKeys(timeline.bindings, ["actors", "entities"], ["actors", "entities"], "timeline.bindings", "TIMELINE_ARTIFACT_INVALID");
  if (!Array.isArray(timeline.bindings.actors) || !Array.isArray(timeline.bindings.entities)) {
    fail("TIMELINE_ARTIFACT_INVALID", "Compiled timeline binding evidence is invalid");
  }
  const issueIds = new Set();
  const issuesById = new Map();
  for (const issue of timeline.issues) {
    exactKeys(issue, ["issue_id", "event_id", "code", "severity", "message", "candidates"], ["issue_id", "event_id", "code", "severity", "message", "candidates"], "timeline.issue", "TIMELINE_ARTIFACT_INVALID");
    if (!/^vti-[a-f0-9]{16}$/.test(issue.issue_id) || issueIds.has(issue.issue_id)) {
      fail("TIMELINE_ARTIFACT_INVALID", "Compiled issue ids are invalid");
    }
    if (!new Set(["error", "warning"]).has(issue.severity) || typeof issue.message !== "string" || !issue.message.length || !Array.isArray(issue.candidates)) {
      fail("TIMELINE_ARTIFACT_INVALID", "Compiled issue shape is invalid");
    }
    issueIds.add(issue.issue_id);
    issuesById.set(issue.issue_id, issue);
  }
  let previous = -1;
  const eventIds = new Set();
  for (const event of timeline.events) {
    exactKeys(
      event,
      ["event_id", "at_sec", "action", "actor_id", "target_id", "parameters", "source_pointer", "actor_binding_id", "target_binding_id", "adapter", "preflight_status", "disposition", "issue_ids"],
      ["event_id", "at_sec", "action", "actor_id", "target_id", "parameters", "source_pointer", "actor_binding_id", "target_binding_id", "adapter", "preflight_status", "disposition", "issue_ids"],
      "timeline.event",
      "TIMELINE_ARTIFACT_INVALID",
    );
    if (!EVENT_ID_RE.test(event.event_id) || eventIds.has(event.event_id)) fail("TIMELINE_ARTIFACT_INVALID", "Compiled event ids are invalid");
    eventIds.add(event.event_id);
    if (event.at_sec < previous || event.at_sec > timeline.duration_sec) fail("TIMELINE_ARTIFACT_INVALID", "Compiled event timestamps are invalid");
    previous = event.at_sec;
    if (!EVENT_STATUSES.has(event.preflight_status) || !DISPOSITIONS.has(event.disposition)) {
      fail("TIMELINE_ARTIFACT_INVALID", "Compiled event lifecycle is invalid");
    }
    if (!Array.isArray(event.issue_ids) || event.issue_ids.length > 1 || event.issue_ids.some((issueId) => !issuesById.has(issueId) || issuesById.get(issueId).event_id !== event.event_id)) {
      fail("TIMELINE_ARTIFACT_INVALID", "Compiled event issue references are invalid");
    }
    if (event.preflight_status === "ready") {
      if (event.adapter === null || event.disposition !== "execute" || event.issue_ids.length !== 0) {
        fail("TIMELINE_ARTIFACT_INVALID", "Ready event adapter selection is inconsistent");
      }
    } else if (event.adapter !== null || event.disposition === "execute" || event.issue_ids.length === 0) {
      fail("TIMELINE_ARTIFACT_INVALID", "Compiled adapter selection is inconsistent");
    }
  }
  for (const issue of timeline.issues) {
    if (issue.event_id !== null && !eventIds.has(issue.event_id)) {
      fail("TIMELINE_ARTIFACT_INVALID", "Compiled issue references an unknown event");
    }
  }
  const total = timeline.events.length;
  const ready = timeline.events.filter((event) => event.preflight_status === "ready").length;
  const skipped = timeline.events.filter((event) => event.disposition === "skip").length;
  const blocked = timeline.events.filter((event) => event.disposition === "block").length;
  if (timeline.summary.total !== total || timeline.summary.ready !== ready || timeline.summary.skipped !== skipped || timeline.summary.blocked !== blocked) {
    fail("TIMELINE_ARTIFACT_INVALID", "Compiled timeline summary is inconsistent");
  }
  const errors = timeline.issues.filter((issue) => issue.severity === "error").length;
  const warnings = timeline.issues.filter((issue) => issue.severity === "warning").length;
  if (timeline.summary.errors !== errors || timeline.summary.warnings !== warnings) {
    fail("TIMELINE_ARTIFACT_INVALID", "Compiled issue summary is inconsistent");
  }
  if (timeline.policy === "strict" && timeline.start_allowed !== (blocked === 0 && ready > 0)) {
    fail("TIMELINE_ARTIFACT_INVALID", "Strict timeline start policy is inconsistent");
  }
  if (timeline.policy === "lenient" && timeline.start_allowed !== (ready > 0)) {
    fail("TIMELINE_ARTIFACT_INVALID", "Lenient timeline start policy is inconsistent");
  }
  return timeline;
}

module.exports = {
  ACTION_ADAPTER_METHODS,
  ACTION_ADAPTER_SCHEMA,
  BINDINGS_SCHEMA,
  CAPABILITY_REGISTRY_SCHEMA,
  COMPILER_NAME,
  COMPILER_VERSION,
  TIMELINE_RUN_SCHEMA,
  TIMELINE_SCHEMA,
  VistaTimelineCompileError,
  compileVistaTimeline,
  preflightVistaTimeline: compileVistaTimeline,
  validateActionAdapter,
  validateBindings,
  validateCapabilityRegistry,
  validateCompiledTimeline,
};
