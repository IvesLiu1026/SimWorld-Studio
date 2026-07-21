"use strict";

const crypto = require("node:crypto");

const {
  VistaSceneBuildError,
  validateVistaSceneBuildPlan,
} = require("./vista-scene-build-plan");

const PREFLIGHT_REQUEST_SCHEMA = "vista-scene-build-preflight-request/v1";
const PREFLIGHT_RESPONSE_SCHEMA = "vista-scene-build-preflight-response/v1";
const PREFLIGHT_RESULT_SCHEMA = "vista-scene-build-preflight-result/v1";
const BUILD_RESULT_SCHEMA = "vista-scene-build-result/v1";
const EXECUTOR_NAME = "vista-scene-executor";
const EXECUTOR_VERSION = "1.0.0";
const ACTOR_STATES = new Set(["absent", "exact_match", "conflict"]);
const PLAYER_STATES = new Set(["exact_match", "needs_update", "unavailable"]);
const EVIDENCE_STATUSES = new Set(["captured", "skipped", "failed"]);

class VistaSceneExecutionError extends VistaSceneBuildError {
  constructor(code, message, options = {}) {
    super(code, message, options);
    this.name = "VistaSceneExecutionError";
    this.result = options.result || null;
  }
}

function fail(code, message, options) {
  throw new VistaSceneExecutionError(code, message, options);
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function exactKeys(value, allowed, required, pointer, code = "SCENE_BUILD_PREFLIGHT_INVALID") {
  if (!isPlainObject(value)) fail(code, `${pointer} must be an object`, { status: 502, details: { pointer } });
  const allowedSet = new Set(allowed);
  const unknown = Object.keys(value).filter((key) => !allowedSet.has(key));
  const missing = required.filter((key) => !Object.prototype.hasOwnProperty.call(value, key));
  if (unknown.length || missing.length) {
    fail(code, `${pointer} has an invalid shape`, { status: 502, details: { pointer, unknown, missing } });
  }
  return value;
}

function deepFreeze(value) {
  if (!value || typeof value !== "object" || Object.isFrozen(value)) return value;
  Object.freeze(value);
  for (const child of Object.values(value)) deepFreeze(child);
  return value;
}

function cloneJson(value, pointer = "value", depth = 0) {
  if (depth > 8) fail("SCENE_BUILD_EVIDENCE_INVALID", `${pointer} exceeds the maximum depth`, { status: 500 });
  if (value === null || typeof value === "boolean" || typeof value === "string") return value;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (Array.isArray(value)) {
    if (value.length > 256) fail("SCENE_BUILD_EVIDENCE_INVALID", `${pointer} is too large`, { status: 500 });
    return value.map((item, index) => cloneJson(item, `${pointer}[${index}]`, depth + 1));
  }
  if (!isPlainObject(value)) fail("SCENE_BUILD_EVIDENCE_INVALID", `${pointer} is not JSON safe`, { status: 500 });
  const keys = Object.keys(value);
  if (keys.length > 256) fail("SCENE_BUILD_EVIDENCE_INVALID", `${pointer} has too many fields`, { status: 500 });
  const output = {};
  for (const key of keys.sort()) {
    if (["__proto__", "constructor", "prototype"].includes(key)) {
      fail("SCENE_BUILD_EVIDENCE_INVALID", `${pointer} contains an unsafe key`, { status: 500 });
    }
    if (/token|secret|password|credential|authorization|cookie/i.test(key)) continue;
    output[key] = cloneJson(value[key], `${pointer}.${key}`, depth + 1);
  }
  return output;
}

function assetKey(asset) {
  return [
    asset.snapshot_id,
    asset.asset_id,
    asset.ue_path,
    asset.class_path,
    asset.content_revision,
    asset.verification_revision,
  ].join("|");
}

function sceneOperationId(planId, actor) {
  return `vso-${crypto.createHash("sha256")
    .update(`${planId}\0${actor.actor_name}\0${actor.fingerprint}`, "utf8")
    .digest("hex")
    .slice(0, 24)}`;
}

function makeVistaScenePreflightRequest(plan) {
  validateVistaSceneBuildPlan(plan);
  const assets = new Map();
  for (const actor of plan.actors) {
    const key = assetKey(actor.asset);
    if (!assets.has(key)) {
      assets.set(key, {
        asset_key: key,
        snapshot_id: actor.asset.snapshot_id,
        asset_id: actor.asset.asset_id,
        ue_path: actor.asset.ue_path,
        class_path: actor.asset.class_path,
        content_revision: actor.asset.content_revision,
        verification_revision: actor.asset.verification_revision,
      });
    }
  }
  return deepFreeze({
    schema: PREFLIGHT_REQUEST_SCHEMA,
    plan_id: plan.plan_id,
    scene_id: plan.scene_id,
    assets: [...assets.values()].sort((left, right) => left.asset_key.localeCompare(right.asset_key)),
    actors: plan.actors.map((actor) => ({
      actor_name: actor.actor_name,
      fingerprint: actor.fingerprint,
      operation_id: sceneOperationId(plan.plan_id, actor),
      asset_kind: actor.asset.kind,
      class_path: actor.asset.class_path,
      ue_path: actor.asset.ue_path,
      transform: cloneJson(actor.transform),
      mobility: actor.mobility,
      collision: cloneJson(actor.collision),
    })),
    player_start: cloneJson(plan.player_start),
  });
}

function isTransform(value) {
  return isPlainObject(value)
    && Array.isArray(value.location_cm) && value.location_cm.length === 3 && value.location_cm.every(Number.isFinite)
    && Array.isArray(value.rotation_deg) && value.rotation_deg.length === 3 && value.rotation_deg.every(Number.isFinite)
    && Array.isArray(value.scale) && value.scale.length === 3 && value.scale.every((item) => Number.isFinite(item) && item > 0);
}

function validateVistaScenePreflightResponse(request, raw) {
  exactKeys(raw, ["schema", "plan_id", "ok", "assets", "actors", "player_start"], ["schema", "plan_id", "ok", "assets", "actors", "player_start"], "preflight response");
  if (raw.schema !== PREFLIGHT_RESPONSE_SCHEMA || raw.plan_id !== request.plan_id || typeof raw.ok !== "boolean") {
    fail("SCENE_BUILD_PREFLIGHT_INVALID", "Preflight response identity is invalid", { status: 502 });
  }
  if (!Array.isArray(raw.assets) || raw.assets.length !== request.assets.length) {
    fail("SCENE_BUILD_PREFLIGHT_INVALID", "Preflight asset response cardinality is invalid", { status: 502 });
  }
  const expectedAssets = new Map(request.assets.map((asset) => [asset.asset_key, asset]));
  const seenAssets = new Set();
  const assets = raw.assets.map((asset, index) => {
    exactKeys(
      asset,
      ["asset_key", "available", "class_matches", "revision_matches"],
      ["asset_key", "available", "class_matches", "revision_matches"],
      `preflight response.assets[${index}]`,
    );
    if (typeof asset.asset_key !== "string" || seenAssets.has(asset.asset_key) || !expectedAssets.has(asset.asset_key)) {
      fail("SCENE_BUILD_PREFLIGHT_INVALID", "Preflight returned an unknown or duplicate asset", { status: 502 });
    }
    if (typeof asset.available !== "boolean" || typeof asset.class_matches !== "boolean" || typeof asset.revision_matches !== "boolean") {
      fail("SCENE_BUILD_PREFLIGHT_INVALID", "Preflight asset flags must be boolean", { status: 502 });
    }
    seenAssets.add(asset.asset_key);
    return { ...expectedAssets.get(asset.asset_key), available: asset.available, class_matches: asset.class_matches, revision_matches: asset.revision_matches };
  }).sort((left, right) => left.asset_key.localeCompare(right.asset_key));

  if (!Array.isArray(raw.actors) || raw.actors.length !== request.actors.length) {
    fail("SCENE_BUILD_PREFLIGHT_INVALID", "Preflight actor response cardinality is invalid", { status: 502 });
  }
  const expectedActors = new Map(request.actors.map((actor) => [actor.actor_name, actor]));
  const seenActors = new Set();
  const actors = raw.actors.map((actor, index) => {
    exactKeys(
      actor,
      ["actor_name", "state", "actual_fingerprint", "actual_operation_id", "object_guid", "spec_matches"],
      ["actor_name", "state", "actual_fingerprint", "actual_operation_id", "object_guid", "spec_matches"],
      `preflight response.actors[${index}]`,
    );
    if (typeof actor.actor_name !== "string" || seenActors.has(actor.actor_name) || !expectedActors.has(actor.actor_name) || !ACTOR_STATES.has(actor.state)) {
      fail("SCENE_BUILD_PREFLIGHT_INVALID", "Preflight returned an invalid actor state", { status: 502 });
    }
    const expected = expectedActors.get(actor.actor_name);
    if (actor.state === "exact_match" && (
      actor.actual_fingerprint !== expected.fingerprint
      || actor.actual_operation_id !== expected.operation_id
      || actor.spec_matches !== true
      || typeof actor.object_guid !== "string"
      || !/^[A-Fa-f0-9-]{16,64}$/.test(actor.object_guid)
    )) {
      fail("SCENE_BUILD_PREFLIGHT_INVALID", "Preflight exact_match does not carry the complete expected actor identity", { status: 502 });
    }
    if (actor.state === "absent" && (
      actor.actual_fingerprint !== null
      || actor.actual_operation_id !== null
      || actor.object_guid !== null
      || actor.spec_matches !== false
    )) {
      fail("SCENE_BUILD_PREFLIGHT_INVALID", "Preflight absent actor cannot carry runtime identity", { status: 502 });
    }
    if (actor.actual_fingerprint !== null && typeof actor.actual_fingerprint !== "string") {
      fail("SCENE_BUILD_PREFLIGHT_INVALID", "Preflight actor fingerprint is invalid", { status: 502 });
    }
    if (actor.actual_operation_id !== null && typeof actor.actual_operation_id !== "string") {
      fail("SCENE_BUILD_PREFLIGHT_INVALID", "Preflight actor operation id is invalid", { status: 502 });
    }
    if (actor.object_guid !== null && typeof actor.object_guid !== "string") {
      fail("SCENE_BUILD_PREFLIGHT_INVALID", "Preflight actor object guid is invalid", { status: 502 });
    }
    if (typeof actor.spec_matches !== "boolean") {
      fail("SCENE_BUILD_PREFLIGHT_INVALID", "Preflight actor spec flag is invalid", { status: 502 });
    }
    seenActors.add(actor.actor_name);
    return {
      actor_name: actor.actor_name,
      expected_fingerprint: expected.fingerprint,
      expected_operation_id: expected.operation_id,
      state: actor.state,
      actual_fingerprint: actor.actual_fingerprint,
      actual_operation_id: actor.actual_operation_id,
      object_guid: actor.object_guid,
      spec_matches: actor.spec_matches,
    };
  }).sort((left, right) => left.actor_name.localeCompare(right.actor_name));

  exactKeys(
    raw.player_start,
    ["actor_name", "class_path", "state", "current_transform", "object_guid"],
    ["actor_name", "class_path", "state", "current_transform", "object_guid"],
    "preflight response.player_start",
  );
  if (
    raw.player_start.actor_name !== request.player_start.actor_name
    || raw.player_start.class_path !== request.player_start.class_path
    || !PLAYER_STATES.has(raw.player_start.state)
  ) fail("SCENE_BUILD_PREFLIGHT_INVALID", "Preflight PlayerStart identity is invalid", { status: 502 });
  if (raw.player_start.state !== "unavailable" && !isTransform(raw.player_start.current_transform)) {
    fail("SCENE_BUILD_PREFLIGHT_INVALID", "Preflight PlayerStart transform is invalid", { status: 502 });
  }
  if (raw.player_start.state === "unavailable" && raw.player_start.current_transform !== null) {
    fail("SCENE_BUILD_PREFLIGHT_INVALID", "Unavailable PlayerStart cannot carry a transform", { status: 502 });
  }
  if (raw.player_start.state === "unavailable" ? raw.player_start.object_guid !== null
    : (typeof raw.player_start.object_guid !== "string"
      || !/^[A-Fa-f0-9-]{16,64}$/.test(raw.player_start.object_guid))) {
    fail("SCENE_BUILD_PREFLIGHT_INVALID", "Preflight PlayerStart object guid is invalid", { status: 502 });
  }
  const normalized = {
    schema: PREFLIGHT_RESPONSE_SCHEMA,
    plan_id: request.plan_id,
    ok: raw.ok,
    assets,
    actors,
    player_start: {
      actor_name: raw.player_start.actor_name,
      class_path: raw.player_start.class_path,
      state: raw.player_start.state,
      current_transform: raw.player_start.current_transform === null ? null : cloneJson(raw.player_start.current_transform),
      object_guid: raw.player_start.object_guid,
    },
  };
  return deepFreeze(normalized);
}

function throwIfAborted(signal) {
  if (!signal || !signal.aborted) return;
  fail("SCENE_BUILD_ABORTED", "VISTA scene build was aborted", { status: 499, retryable: false });
}

function createMutex() {
  let locked = false;
  const waiters = [];

  function release() {
    const next = waiters.shift();
    if (!next) {
      locked = false;
      return;
    }
    next.cleanup();
    next.resolve(release);
  }

  return {
    acquire(signal) {
      throwIfAborted(signal);
      if (!locked) {
        locked = true;
        return Promise.resolve(release);
      }
      return new Promise((resolve, reject) => {
        const waiter = {
          resolve,
          reject,
          cleanup: () => {},
        };
        const onAbort = () => {
          const index = waiters.indexOf(waiter);
          if (index >= 0) waiters.splice(index, 1);
          waiter.cleanup();
          try {
            throwIfAborted(signal);
          } catch (error) {
            reject(error);
          }
        };
        waiter.cleanup = () => signal && signal.removeEventListener("abort", onAbort);
        if (signal) signal.addEventListener("abort", onAbort, { once: true });
        waiters.push(waiter);
        if (signal && signal.aborted) onAbort();
      });
    },
  };
}

function executionSlotKey(options = {}) {
  return Number.isSafeInteger(options.slotId) && options.slotId >= 0
    ? `slot:${options.slotId}`
    : "slot:default";
}

function normalizeBrokerError(error, code, message, options = {}) {
  if (error instanceof VistaSceneExecutionError) return error;
  if (error && (error.name === "AbortError" || error.code === "UE_COMMAND_ABORTED")) {
    return new VistaSceneExecutionError("SCENE_BUILD_ABORTED", "VISTA scene build was aborted", { status: 499 });
  }
  return new VistaSceneExecutionError(code, message, {
    status: options.status || 503,
    retryable: options.retryable !== false,
    details: { cause: error && typeof error.code === "string" ? error.code : undefined },
  });
}

function brokerSucceeded(result) {
  return Boolean(result && typeof result === "object" && (result.ok === true || result.status === "success"));
}

function runtimeActorName(result) {
  if (!result || typeof result !== "object") return null;
  return result.actor_name
    || result.name
    || (result.result && (result.result.actor_name || result.result.name))
    || null;
}

function spawnParams(actor) {
  const common = {
    location: [...actor.transform.location_cm],
    rotation: [...actor.transform.rotation_deg],
    scale: [...actor.transform.scale],
    mobility: actor.mobility,
    collision: cloneJson(actor.collision),
    vista_fingerprint: actor.fingerprint,
  };
  if (actor.spawn_tool === "spawn_actor") {
    return { name: actor.actor_name, static_mesh: actor.asset.ue_path, ...common };
  }
  return { actor_name: actor.actor_name, blueprint_id: actor.asset.class_path, ...common };
}

function brokerExecutionOptions(executionOptions = {}, signal = executionOptions.signal) {
  return {
    ...(signal ? { signal } : {}),
    ...(typeof executionOptions.ownerId === "string" ? { ownerId: executionOptions.ownerId } : {}),
    ...(typeof executionOptions.sessionId === "string" ? { sessionId: executionOptions.sessionId } : {}),
    ...(Number.isSafeInteger(executionOptions.slotId) ? { slotId: executionOptions.slotId } : {}),
    ...(typeof executionOptions.leaseId === "string" ? { leaseId: executionOptions.leaseId } : {}),
    ...(Number.isSafeInteger(executionOptions.mcpPort) ? { mcpPort: executionOptions.mcpPort } : {}),
  };
}

async function spawnActor(broker, actor, signal, executionOptions = {}) {
  throwIfAborted(signal);
  let result;
  try {
    if (typeof broker.spawnActor === "function") {
      result = await broker.spawnActor(actor, brokerExecutionOptions(executionOptions, signal));
    } else {
      result = await broker.send(actor.spawn_tool, spawnParams(actor), {
        ...brokerExecutionOptions(executionOptions, signal),
        maxAttempts: 1,
      });
    }
  } catch (error) {
    throw normalizeBrokerError(error, "SCENE_BUILD_ACTOR_SPAWN_FAILED", `Failed to spawn '${actor.actor_name}'`);
  }
  if (!brokerSucceeded(result)) {
    fail("SCENE_BUILD_ACTOR_SPAWN_FAILED", `UE rejected actor '${actor.actor_name}'`, {
      status: 502, retryable: true, details: { actor_id: actor.actor_id },
    });
  }
  const returnedName = runtimeActorName(result);
  if (returnedName !== null && returnedName !== actor.actor_name) {
    fail("SCENE_BUILD_ACTOR_NAME_MISMATCH", "UE did not preserve the deterministic actor name", {
      status: 502, details: { actor_id: actor.actor_id, expected_actor_name: actor.actor_name, actual_actor_name: returnedName },
    });
  }
  const objectGuid = result.object_guid || (result.result && result.result.object_guid) || null;
  const operationId = result.operation_id || (result.result && result.result.operation_id) || null;
  if (operationId !== actor.operation_id) {
    fail("SCENE_BUILD_ACTOR_IDENTITY_MISMATCH", "UE did not preserve the deterministic operation id", {
      status: 502, details: { actor_id: actor.actor_id },
    });
  }
  if (typeof objectGuid !== "string" || !/^[A-Fa-f0-9-]{16,64}$/.test(objectGuid)) {
    fail("SCENE_BUILD_ACTOR_IDENTITY_MISMATCH", "UE returned an invalid actor object guid", {
      status: 502, details: { actor_id: actor.actor_id },
    });
  }
  return {
    runtime_actor_name: returnedName || actor.actor_name,
    object_guid: objectGuid,
    operation_id: operationId,
  };
}

async function deleteActor(broker, actor, executionOptions = {}) {
  const actorName = actor.actor_name;
  let result;
  if (typeof broker.deleteActor === "function") {
    result = await broker.deleteActor(actor, brokerExecutionOptions(executionOptions, null));
  }
  else result = await broker.send("delete_actor", {
    name: actorName,
    vista_fingerprint: actor.fingerprint,
  }, { ...brokerExecutionOptions(executionOptions, null), maxAttempts: 1 });
  if (!brokerSucceeded(result)) throw new Error(`UE rejected rollback delete for '${actorName}'`);
  return result;
}

async function setPlayerStart(broker, playerStart, camera, expectedCurrentTransform, expectedObjectGuid, signal, executionOptions = {}) {
  throwIfAborted(signal);
  let result;
  try {
    if (typeof broker.setPlayerStart === "function") {
      result = await broker.setPlayerStart(
        {
          player_start: playerStart,
          camera,
          expected_current_transform: expectedCurrentTransform,
          expected_object_guid: expectedObjectGuid,
        },
        brokerExecutionOptions(executionOptions, signal),
      );
    } else {
      result = await broker.send("set_actor_transform", {
        name: playerStart.actor_name,
        location: [...playerStart.transform.location_cm],
        rotation: [...playerStart.transform.rotation_deg],
        scale: [...playerStart.transform.scale],
        pawn_class_path: playerStart.pawn_class_path,
        camera: cloneJson(camera),
        expected_current_transform: cloneJson(expectedCurrentTransform),
        expected_object_guid: expectedObjectGuid,
      }, { ...brokerExecutionOptions(executionOptions, signal), maxAttempts: 1 });
    }
  } catch (error) {
    throw normalizeBrokerError(error, "SCENE_BUILD_PLAYER_START_FAILED", "Failed to configure VISTA PlayerStart");
  }
  if (!brokerSucceeded(result)) fail("SCENE_BUILD_PLAYER_START_FAILED", "UE rejected the VISTA PlayerStart configuration", { status: 502, retryable: true });
}

async function restorePlayerStart(broker, playerStart, transform, expectedCurrentTransform, expectedObjectGuid, executionOptions = {}) {
  let result;
  if (typeof broker.restorePlayerStart === "function") {
    result = await broker.restorePlayerStart(
      {
        actor_name: playerStart.actor_name,
        transform,
        expected_current_transform: expectedCurrentTransform,
        expected_object_guid: expectedObjectGuid,
      },
      brokerExecutionOptions(executionOptions, null),
    );
  } else {
    result = await broker.send("set_actor_transform", {
      name: playerStart.actor_name,
      location: [...transform.location_cm],
      rotation: [...transform.rotation_deg],
      scale: [...transform.scale],
      expected_current_transform: cloneJson(expectedCurrentTransform),
      expected_object_guid: expectedObjectGuid,
    }, { ...brokerExecutionOptions(executionOptions, null), maxAttempts: 1 });
  }
  if (!brokerSucceeded(result)) throw new Error("UE rejected PlayerStart rollback");
}

function nowIso(clock) {
  const value = clock();
  if (typeof value !== "string" || Number.isNaN(Date.parse(value))) {
    fail("SCENE_BUILD_CLOCK_INVALID", "Scene executor clock returned an invalid date-time", { status: 500 });
  }
  return value;
}

function errorMessage(error) {
  const message = error && typeof error.message === "string" ? error.message : "Unknown scene build failure";
  return message.slice(0, 1000);
}

function actorManifestEntry(actor, disposition, objectGuid = null) {
  return {
    actor_id: actor.actor_id,
    actor_name: actor.actor_name,
    runtime_actor_name: actor.actor_name,
    fingerprint: actor.fingerprint,
    operation_id: actor.operation_id,
    object_guid: objectGuid,
    disposition,
    role: actor.role,
    source_entity_id: actor.source_entity_id,
    component_id: actor.component_id,
    infrastructure_kind: actor.infrastructure_kind,
    asset: cloneJson(actor.asset),
    transform: cloneJson(actor.transform),
    mobility: actor.mobility,
    collision: cloneJson(actor.collision),
  };
}

function validateEvidenceHooks(hooks) {
  if (!isPlainObject(hooks)) fail("SCENE_BUILD_EXECUTOR_CONFIG_INVALID", "evidenceHooks must be an object", { status: 500 });
  for (const [kind, hook] of Object.entries(hooks)) {
    if (!new Set(["actor_snapshot", "screenshot", "collision_report", "floating_report"]).has(kind) || typeof hook !== "function") {
      fail("SCENE_BUILD_EXECUTOR_CONFIG_INVALID", "evidenceHooks contains an invalid collector", { status: 500, details: { kind } });
    }
  }
  return { ...hooks };
}

async function collectEvidence(plan, hooks, context) {
  const evidence = [];
  // Hooks share one private cache so the UE adapter can bind actor/collision/
  // floating observations to the same validation snapshot and compare it with
  // the post-screenshot snapshot. The cache is never serialized into evidence.
  const evidenceCache = new Map();
  for (const request of plan.evidence_requests) {
    throwIfAborted(context.signal);
    const hook = hooks[request.kind];
    if (!hook) {
      if (request.required) {
        fail("SCENE_BUILD_EVIDENCE_HOOK_MISSING", `Required evidence hook '${request.kind}' is unavailable`, {
          status: 500, details: { evidence_id: request.evidence_id, kind: request.kind },
        });
      }
      evidence.push({ evidence_id: request.evidence_id, kind: request.kind, required: false, status: "skipped", artifact: null, error: null });
      continue;
    }
    try {
      const artifact = await hook(Object.freeze({ ...context, request, evidence_cache: evidenceCache }));
      if (artifact === undefined || artifact === null) throw new Error("collector returned no artifact");
      evidence.push({
        evidence_id: request.evidence_id,
        kind: request.kind,
        required: request.required,
        status: "captured",
        artifact: cloneJson(artifact, `evidence.${request.evidence_id}`),
        error: null,
      });
    } catch (error) {
      if (error instanceof VistaSceneExecutionError) throw error;
      if (request.required) {
        fail("SCENE_BUILD_REQUIRED_EVIDENCE_FAILED", `Required evidence '${request.evidence_id}' failed`, {
          status: 502, retryable: true, details: { evidence_id: request.evidence_id, kind: request.kind },
        });
      }
      evidence.push({
        evidence_id: request.evidence_id,
        kind: request.kind,
        required: false,
        status: "failed",
        artifact: null,
        error: errorMessage(error),
      });
    }
  }
  if (evidence.some((entry) => !EVIDENCE_STATUSES.has(entry.status))) {
    fail("SCENE_BUILD_EVIDENCE_INVALID", "Evidence collector produced an invalid status", { status: 500 });
  }
  return evidence;
}

function evaluatePreflight(plan, response, hooks) {
  const missingHooks = plan.evidence_requests.filter((request) => request.required && typeof hooks[request.kind] !== "function");
  if (missingHooks.length) {
    fail("SCENE_BUILD_EVIDENCE_HOOK_MISSING", "A required evidence collector is unavailable", {
      status: 500,
      details: { evidence: missingHooks.map((request) => ({ evidence_id: request.evidence_id, kind: request.kind })) },
    });
  }
  const unavailable = response.assets.filter((asset) => !asset.available);
  if (unavailable.length) {
    fail("SCENE_BUILD_PREFLIGHT_ASSET_UNAVAILABLE", "One or more pinned UE assets are unavailable", {
      status: 409, details: { assets: unavailable.map((asset) => asset.ue_path) },
    });
  }
  const classMismatch = response.assets.filter((asset) => !asset.class_matches);
  if (classMismatch.length) {
    fail("SCENE_BUILD_PREFLIGHT_CLASS_MISMATCH", "One or more UE assets do not match the pinned class", {
      status: 409, details: { assets: classMismatch.map((asset) => asset.ue_path) },
    });
  }
  const revisionMismatch = response.assets.filter((asset) => !asset.revision_matches);
  if (revisionMismatch.length) {
    fail("SCENE_BUILD_PREFLIGHT_REVISION_MISMATCH", "One or more UE assets do not match the pinned revision", {
      status: 409, details: { assets: revisionMismatch.map((asset) => asset.ue_path) },
    });
  }
  const conflicts = response.actors.filter((actor) => actor.state === "conflict");
  if (conflicts.length) {
    fail("SCENE_BUILD_PREFLIGHT_ACTOR_CONFLICT", "A deterministic actor name is occupied by different content", {
      status: 409, details: { actor_names: conflicts.map((actor) => actor.actor_name) },
    });
  }
  if (response.player_start.state === "unavailable") {
    fail("SCENE_BUILD_PREFLIGHT_PLAYER_START_UNAVAILABLE", "The pinned PlayerStart is unavailable", { status: 409 });
  }
  if (response.ok !== true) {
    fail("SCENE_BUILD_PREFLIGHT_REJECTED", "UE preflight rejected the scene build", { status: 409 });
  }
}

function createVistaSceneExecutor(options = {}) {
  exactKeys(options, ["broker", "evidenceHooks", "clock"], ["broker", "evidenceHooks"], "executor options", "SCENE_BUILD_EXECUTOR_CONFIG_INVALID");
  const broker = options.broker;
  if (!broker || typeof broker !== "object" || typeof broker.preflight !== "function") {
    fail("SCENE_BUILD_EXECUTOR_CONFIG_INVALID", "broker.preflight must be injected", { status: 500 });
  }
  if (typeof broker.spawnActor !== "function" && typeof broker.send !== "function") {
    fail("SCENE_BUILD_EXECUTOR_CONFIG_INVALID", "broker must expose spawnActor or send", { status: 500 });
  }
  if (typeof broker.deleteActor !== "function" && typeof broker.send !== "function") {
    fail("SCENE_BUILD_EXECUTOR_CONFIG_INVALID", "broker must expose deleteActor or send", { status: 500 });
  }
  const hooks = validateEvidenceHooks(options.evidenceHooks);
  const clock = options.clock === undefined ? (() => new Date().toISOString()) : options.clock;
  if (typeof clock !== "function") fail("SCENE_BUILD_EXECUTOR_CONFIG_INVALID", "clock must be a function", { status: 500 });
  const activePlans = new Set();
  const slotMutexes = new Map();

  async function withSlotLock(executionOptions, operation) {
    const key = executionSlotKey(executionOptions);
    let mutex = slotMutexes.get(key);
    if (!mutex) {
      mutex = createMutex();
      slotMutexes.set(key, mutex);
    }
    const release = await mutex.acquire(executionOptions.signal);
    try {
      return await operation();
    } finally {
      release();
    }
  }

  async function preflightUnlocked(plan, executionOptions = {}) {
    validateVistaSceneBuildPlan(plan);
    const signal = executionOptions.signal;
    throwIfAborted(signal);
    const request = makeVistaScenePreflightRequest(plan);
    let raw;
    try {
      raw = await broker.preflight(request, executionOptions);
    } catch (error) {
      throw normalizeBrokerError(error, "SCENE_BUILD_PREFLIGHT_UNAVAILABLE", "UE scene preflight is unavailable");
    }
    throwIfAborted(signal);
    const response = validateVistaScenePreflightResponse(request, raw);
    evaluatePreflight(plan, response, hooks);
    return deepFreeze({
      schema: PREFLIGHT_RESULT_SCHEMA,
      plan_id: plan.plan_id,
      ready: true,
      assets: response.assets,
      actors: response.actors,
      player_start: response.player_start,
    });
  }

  async function preflight(plan, executionOptions = {}) {
    return withSlotLock(executionOptions, () => preflightUnlocked(plan, executionOptions));
  }

  async function rollback(
    attemptedActors,
    playerMutated,
    playerStart,
    previousPlayerTransform,
    playerObjectGuid,
    executionOptions,
  ) {
    const failures = [];
    const deletedActorNames = [];
    for (const actor of [...attemptedActors].reverse()) {
      try {
        const result = await deleteActor(broker, actor, executionOptions);
        if (!result || result.disposition !== "absent") deletedActorNames.push(actor.actor_name);
      } catch (error) {
        failures.push({ operation: "delete_actor", actor_name: actor.actor_name, error: errorMessage(error) });
      }
    }
    let restoredPlayerStart = false;
    if (playerMutated) {
      try {
        await restorePlayerStart(
          broker,
          playerStart,
          previousPlayerTransform,
          playerStart.transform,
          playerObjectGuid,
          executionOptions,
        );
        restoredPlayerStart = true;
      } catch (error) {
        failures.push({ operation: "restore_player_start", actor_name: playerStart.actor_name, error: errorMessage(error) });
      }
    }
    return {
      state: failures.length ? "partial" : (attemptedActors.length || playerMutated ? "completed" : "not_required"),
      deleted_actor_names: deletedActorNames,
      restored_player_start: restoredPlayerStart,
      failures,
    };
  }

  async function execute(plan, executionOptions = {}) {
    validateVistaSceneBuildPlan(plan);
    const activeKey = `${executionSlotKey(executionOptions)}:${plan.plan_id}`;
    if (activePlans.has(activeKey)) {
      fail("SCENE_BUILD_ALREADY_RUNNING", "This BuildPlan is already executing", { status: 409, retryable: true });
    }
    activePlans.add(activeKey);
    try {
      return await withSlotLock(executionOptions, () => executeUnlocked(plan, executionOptions));
    } finally {
      activePlans.delete(activeKey);
    }
  }

  async function executeUnlocked(plan, executionOptions = {}) {
    const signal = executionOptions.signal;
    const startedAt = nowIso(clock);
    const attemptedActors = [];
    const actorManifest = [];
    let playerMutationAttempted = false;
    let previousPlayerTransform = null;
    let preflightResult = null;
    let mutationCount = 0;
    let evidence = [];
    try {
      preflightResult = await preflightUnlocked(plan, executionOptions);
      const actorStates = new Map(preflightResult.actors.map((actor) => [actor.actor_name, actor.state]));
      const preflightActors = new Map(preflightResult.actors.map((actor) => [actor.actor_name, actor]));
      for (const actor of plan.actors) {
        throwIfAborted(signal);
        const runtimeActor = {
          ...actor,
          operation_id: sceneOperationId(plan.plan_id, actor),
          object_guid: null,
        };
        if (actorStates.get(actor.actor_name) === "exact_match") {
          runtimeActor.object_guid = preflightActors.get(actor.actor_name).object_guid;
          actorManifest.push(actorManifestEntry(runtimeActor, "reused", runtimeActor.object_guid));
          continue;
        }
        attemptedActors.push(runtimeActor);
        const spawned = await spawnActor(broker, runtimeActor, signal, executionOptions);
        runtimeActor.object_guid = spawned.object_guid;
        mutationCount += 1;
        actorManifest.push(actorManifestEntry(runtimeActor, "spawned", runtimeActor.object_guid));
      }
      if (preflightResult.player_start.state === "needs_update") {
        previousPlayerTransform = cloneJson(preflightResult.player_start.current_transform);
        // Record intent before dispatch: UE may apply the mutation and lose the
        // response, in which case rollback must still restore the prior state.
        playerMutationAttempted = true;
        await setPlayerStart(
          broker,
          plan.player_start,
          plan.camera,
          previousPlayerTransform,
          preflightResult.player_start.object_guid,
          signal,
          executionOptions,
        );
        mutationCount += 1;
      }
      evidence = await collectEvidence(plan, hooks, {
        plan,
        preflight: preflightResult,
        actor_manifest: deepFreeze(cloneJson(actorManifest)),
        player_start: plan.player_start,
        camera: plan.camera,
        broker,
        ...brokerExecutionOptions(executionOptions, signal),
      });
      const result = {
        schema: BUILD_RESULT_SCHEMA,
        build_id: `vsb-${plan.plan_id.slice(4)}`,
        plan_id: plan.plan_id,
        scene_id: plan.scene_id,
        status: mutationCount === 0 ? "already_applied" : "succeeded",
        executor: { name: EXECUTOR_NAME, version: EXECUTOR_VERSION },
        started_at: startedAt,
        ended_at: nowIso(clock),
        mutation_count: mutationCount,
        actor_manifest: actorManifest,
        player_start: {
          ...cloneJson(plan.player_start),
          disposition: preflightResult.player_start.state === "needs_update" ? "updated" : "reused",
          applied: {
            transform: true,
            pawn_class: false,
          },
          pawn_application_status: "not_applied",
          pawn_application_reason: "animation_runtime_required",
        },
        camera: {
          requested: cloneJson(plan.camera),
          application_status: "not_applied",
          reason: "animation_runtime_required",
        },
        evidence,
        rollback: {
          state: "not_required",
          deleted_actor_names: [],
          restored_player_start: false,
          failures: [],
        },
      };
      return deepFreeze(result);
    } catch (error) {
      const rollbackResult = await rollback(
        attemptedActors,
        playerMutationAttempted,
        plan.player_start,
        previousPlayerTransform,
        preflightResult && preflightResult.player_start.object_guid,
        executionOptions,
      );
      const normalizedError = error instanceof VistaSceneExecutionError
        ? error
        : normalizeBrokerError(error, "SCENE_BUILD_EXECUTION_FAILED", "VISTA scene build execution failed", { retryable: true });
      const failedResult = deepFreeze({
        schema: BUILD_RESULT_SCHEMA,
        build_id: `vsb-${plan.plan_id.slice(4)}`,
        plan_id: plan.plan_id,
        scene_id: plan.scene_id,
        status: "failed",
        executor: { name: EXECUTOR_NAME, version: EXECUTOR_VERSION },
        started_at: startedAt,
        ended_at: nowIso(clock),
        mutation_count: mutationCount,
        actor_manifest: actorManifest,
        player_start: cloneJson(plan.player_start),
        camera: cloneJson(plan.camera),
        evidence,
        error: { code: normalizedError.code, message: errorMessage(normalizedError) },
        rollback: rollbackResult,
      });
      normalizedError.result = failedResult;
      throw normalizedError;
    }
  }

  return Object.freeze({ preflight, execute });
}

module.exports = {
  PREFLIGHT_REQUEST_SCHEMA,
  PREFLIGHT_RESPONSE_SCHEMA,
  PREFLIGHT_RESULT_SCHEMA,
  BUILD_RESULT_SCHEMA,
  EXECUTOR_NAME,
  EXECUTOR_VERSION,
  VistaSceneExecutionError,
  createVistaSceneExecutor,
  makeVistaScenePreflightRequest,
  validateVistaScenePreflightResponse,
};
