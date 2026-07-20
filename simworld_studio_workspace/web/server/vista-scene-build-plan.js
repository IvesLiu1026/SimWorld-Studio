"use strict";

const crypto = require("node:crypto");

const BUILD_PLAN_SCHEMA = "vista-scene-build-plan/v1";
const LAYOUT_PROFILE_SCHEMA = "vista-scene-layout-profile/v1";
const COMPILER_NAME = "vista-scene-build-plan-compiler";
const COMPILER_VERSION = "1.0.0";
const STATIC_MESH_ACTOR_CLASS = "/Script/Engine.StaticMeshActor";
const PLAYER_START_CLASS = "/Script/Engine.PlayerStart";
const ENGINE_RUNTIME_GROUND = "/Engine/BasicShapes/Cube.Cube";

const SAFE_ID_RE = /^[a-z][a-z0-9_-]{0,119}$/;
const SAFE_REVISION_RE = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,159}$/;
const SCENE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,79}@[a-f0-9]{12,64}$/;
const SHA256_RE = /^[a-f0-9]{64}$/;
const UE_OBJECT_PATH_RE = /^\/(?:Game|Engine)\/[A-Za-z0-9_./-]{1,504}$/;
const UE_GAME_CLASS_RE = /^\/Game\/[A-Za-z0-9_./-]+_C$/;
const UE_SCRIPT_CLASS_RE = /^\/Script\/[A-Za-z0-9_./-]+$/;
const ACTOR_NAME_RE = /^VISTA_[A-Za-z0-9_]{1,114}$/;
const PLAYER_NAME_RE = /^[A-Za-z0-9_]{1,120}$/;
const ASSET_KINDS = new Set(["static_mesh", "blueprint_class"]);
const ASSET_SOURCES = new Set(["scene_binding", "curated_component"]);
const INFRASTRUCTURE_KINDS = new Set(["runtime_ground", "environment_shell", "lighting"]);
const MOBILITIES = new Set(["static", "stationary", "movable"]);
const COLLISION_MODES = new Set(["query_and_physics", "query_only", "disabled"]);
const EVIDENCE_KINDS = new Set(["actor_snapshot", "screenshot", "collision_report", "floating_report"]);
const MAX_ACTORS = 512;

class VistaSceneBuildError extends Error {
  constructor(code, message, options = {}) {
    super(message);
    this.name = "VistaSceneBuildError";
    this.code = code;
    this.status = Number.isInteger(options.status) ? options.status : 400;
    this.retryable = options.retryable === true;
    this.details = sanitizeDetails(options.details || {});
  }
}

function sanitizeDetails(value, depth = 0) {
  if (depth > 5) return "[truncated]";
  if (value === null || typeof value === "boolean" || typeof value === "number") return value;
  if (typeof value === "string") return value.slice(0, 500);
  if (Array.isArray(value)) return value.slice(0, 64).map((item) => sanitizeDetails(item, depth + 1));
  if (!isPlainObject(value)) return undefined;
  const output = {};
  for (const key of Object.keys(value).sort().slice(0, 64)) {
    if (/token|secret|password|credential|authorization|cookie/i.test(key)) continue;
    const sanitized = sanitizeDetails(value[key], depth + 1);
    if (sanitized !== undefined) output[key] = sanitized;
  }
  return output;
}

function fail(code, message, options) {
  throw new VistaSceneBuildError(code, message, options);
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function exactKeys(value, allowed, required, pointer, code) {
  if (!isPlainObject(value)) fail(code, `${pointer} must be an object`, { details: { pointer } });
  const allowedSet = new Set(allowed);
  const unknown = Object.keys(value).filter((key) => !allowedSet.has(key));
  const missing = required.filter((key) => !Object.prototype.hasOwnProperty.call(value, key));
  if (unknown.length || missing.length) {
    fail(code, `${pointer} has an invalid shape`, { details: { pointer, unknown, missing } });
  }
  return value;
}

function requireString(value, pointer, { pattern = null, max = 240, code = "SCENE_BUILD_INPUT_INVALID" } = {}) {
  if (typeof value !== "string" || value.length < 1 || value.length > max || (pattern && !pattern.test(value))) {
    fail(code, `${pointer} is invalid`, { details: { pointer } });
  }
  return value;
}

function requireNumber(value, pointer, { min = -Infinity, max = Infinity, exclusiveMin = false, code = "SCENE_BUILD_INPUT_INVALID" } = {}) {
  const below = exclusiveMin ? value <= min : value < min;
  if (typeof value !== "number" || !Number.isFinite(value) || below || value > max) {
    fail(code, `${pointer} must be a finite number in range`, { details: { pointer, min, max } });
  }
  return value;
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

function normalizeVector(value, pointer, { min, max, exclusiveMin = false, code }) {
  if (!Array.isArray(value) || value.length !== 3) {
    fail(code, `${pointer} must be a three-element numeric vector`, { details: { pointer } });
  }
  return value.map((component, index) => requireNumber(component, `${pointer}[${index}]`, {
    min: Array.isArray(min) ? min[index] : min,
    max: Array.isArray(max) ? max[index] : max,
    exclusiveMin,
    code,
  }));
}

function normalizeTransform(value, pointer, code) {
  exactKeys(value, ["location_cm", "rotation_deg", "scale"], ["location_cm", "rotation_deg", "scale"], pointer, code);
  return {
    location_cm: normalizeVector(value.location_cm, `${pointer}.location_cm`, {
      min: [-9500, -9500, -1000], max: [9500, 9500, 50000], code,
    }),
    rotation_deg: normalizeVector(value.rotation_deg, `${pointer}.rotation_deg`, {
      min: -360000, max: 360000, code,
    }),
    scale: normalizeVector(value.scale, `${pointer}.scale`, {
      min: 0, max: 1000, exclusiveMin: true, code,
    }),
  };
}

function normalizeCollision(value, pointer, code) {
  exactKeys(value, ["mode", "profile_name", "generate_overlap_events"], ["mode", "profile_name", "generate_overlap_events"], pointer, code);
  if (!COLLISION_MODES.has(value.mode)) fail(code, `${pointer}.mode is invalid`, { details: { pointer } });
  const profileName = requireString(value.profile_name, `${pointer}.profile_name`, {
    pattern: /^[A-Za-z0-9_]+$/, max: 80, code,
  });
  if (typeof value.generate_overlap_events !== "boolean") {
    fail(code, `${pointer}.generate_overlap_events must be boolean`, { details: { pointer } });
  }
  if (value.mode === "disabled" && profileName !== "NoCollision") {
    fail(code, `${pointer} must use NoCollision when disabled`, { details: { pointer } });
  }
  return {
    mode: value.mode,
    profile_name: profileName,
    generate_overlap_events: value.generate_overlap_events,
  };
}

function normalizeAssetPin(value, pointer, code) {
  exactKeys(
    value,
    ["snapshot_id", "asset_id", "ue_path", "confidence", "verified"],
    ["snapshot_id", "asset_id", "ue_path", "confidence", "verified"],
    pointer,
    code,
  );
  if (value.verified !== true) fail(code, `${pointer}.verified must be true`, { details: { pointer } });
  return {
    snapshot_id: requireString(value.snapshot_id, `${pointer}.snapshot_id`, { pattern: SAFE_REVISION_RE, max: 160, code }),
    asset_id: requireString(value.asset_id, `${pointer}.asset_id`, { pattern: SAFE_REVISION_RE, max: 160, code }),
    ue_path: requireString(value.ue_path, `${pointer}.ue_path`, { pattern: UE_OBJECT_PATH_RE, max: 512, code }),
    confidence: requireNumber(value.confidence, `${pointer}.confidence`, { min: 0, max: 1, code }),
    verified: true,
  };
}

function validateAssetClass(kind, uePath, classPath, pointer, code) {
  if (!ASSET_KINDS.has(kind)) fail(code, `${pointer}.asset_kind is invalid`, { details: { pointer } });
  const checkedClass = requireString(classPath, `${pointer}.class_path`, {
    pattern: /^(?:\/Script\/[A-Za-z0-9_./-]+|\/Game\/[A-Za-z0-9_./-]+_C)$/, max: 512, code,
  });
  if (kind === "static_mesh" && checkedClass !== STATIC_MESH_ACTOR_CLASS) {
    fail(code, `${pointer} static meshes must pin StaticMeshActor`, { details: { pointer, class_path: checkedClass } });
  }
  if (kind === "blueprint_class" && (!UE_GAME_CLASS_RE.test(uePath) || checkedClass !== uePath)) {
    fail(code, `${pointer} blueprint path and generated class must be the same exact _C path`, { details: { pointer } });
  }
  return checkedClass;
}

function isBasicGeometry(assetOrPath) {
  const raw = typeof assetOrPath === "string"
    ? assetOrPath
    : String((assetOrPath && (assetOrPath.ue_path || assetOrPath.path || assetOrPath.asset_id)) || "");
  const normalized = raw.toLowerCase();
  if (normalized.includes("/engine/basicshapes/") || normalized.includes("/basicshapes/")) return true;
  const leaf = normalized.split(/[/.]/).filter(Boolean).pop() || "";
  return /^(?:sm_)?(?:cube|plane|sphere|cylinder|cone)$/.test(leaf);
}

function normalizePlacement(value, pointer) {
  const code = "SCENE_BUILD_LAYOUT_INVALID";
  exactKeys(
    value,
    [
      "actor_id", "source_entity_id", "component_id", "asset_source", "asset_kind",
      "class_path", "asset_pin", "transform", "mobility", "collision",
    ],
    [
      "actor_id", "source_entity_id", "component_id", "asset_source", "asset_kind",
      "class_path", "asset_pin", "transform", "mobility", "collision",
    ],
    pointer,
    code,
  );
  const actorId = requireString(value.actor_id, `${pointer}.actor_id`, { pattern: SAFE_ID_RE, max: 120, code });
  const sourceEntityId = requireString(value.source_entity_id, `${pointer}.source_entity_id`, { pattern: SAFE_ID_RE, max: 120, code });
  const componentId = value.component_id === null
    ? null
    : requireString(value.component_id, `${pointer}.component_id`, { pattern: SAFE_ID_RE, max: 120, code });
  if (!ASSET_SOURCES.has(value.asset_source)) fail(code, `${pointer}.asset_source is invalid`, { details: { pointer } });
  if (!ASSET_KINDS.has(value.asset_kind)) fail(code, `${pointer}.asset_kind is invalid`, { details: { pointer } });
  const classPath = requireString(value.class_path, `${pointer}.class_path`, {
    pattern: /^(?:\/Script\/[A-Za-z0-9_./-]+|\/Game\/[A-Za-z0-9_./-]+_C)$/, max: 512, code,
  });
  let assetPin = null;
  if (value.asset_source === "scene_binding") {
    if (componentId !== null || value.asset_pin !== null) {
      fail(code, `${pointer} scene_binding cannot override a component asset`, { details: { pointer } });
    }
  } else {
    if (componentId === null || value.asset_pin === null) {
      fail(code, `${pointer} curated_component requires component_id and asset_pin`, { details: { pointer } });
    }
    assetPin = normalizeAssetPin(value.asset_pin, `${pointer}.asset_pin`, code);
    validateAssetClass(value.asset_kind, assetPin.ue_path, classPath, pointer, code);
  }
  if (!MOBILITIES.has(value.mobility)) fail(code, `${pointer}.mobility is invalid`, { details: { pointer } });
  return {
    actor_id: actorId,
    source_entity_id: sourceEntityId,
    component_id: componentId,
    asset_source: value.asset_source,
    asset_kind: value.asset_kind,
    class_path: classPath,
    asset_pin: assetPin,
    transform: normalizeTransform(value.transform, `${pointer}.transform`, code),
    mobility: value.mobility,
    collision: normalizeCollision(value.collision, `${pointer}.collision`, code),
  };
}

function normalizeInfrastructure(value, pointer) {
  const code = "SCENE_BUILD_LAYOUT_INVALID";
  exactKeys(
    value,
    ["actor_id", "infrastructure_kind", "asset_kind", "class_path", "asset_pin", "transform", "mobility", "collision"],
    ["actor_id", "infrastructure_kind", "asset_kind", "class_path", "asset_pin", "transform", "mobility", "collision"],
    pointer,
    code,
  );
  const actorId = requireString(value.actor_id, `${pointer}.actor_id`, { pattern: SAFE_ID_RE, max: 120, code });
  if (!INFRASTRUCTURE_KINDS.has(value.infrastructure_kind)) {
    fail(code, `${pointer}.infrastructure_kind is invalid`, { details: { pointer } });
  }
  const pin = normalizeAssetPin(value.asset_pin, `${pointer}.asset_pin`, code);
  const classPath = validateAssetClass(value.asset_kind, pin.ue_path, value.class_path, pointer, code);
  if (isBasicGeometry(pin)) {
    const allowedGround = value.infrastructure_kind === "runtime_ground"
      && value.asset_kind === "static_mesh"
      && pin.ue_path === ENGINE_RUNTIME_GROUND
      && value.mobility === "static"
      && value.collision && value.collision.mode === "query_and_physics"
      && value.collision.profile_name === "BlockAll";
    if (!allowedGround) {
      fail("SCENE_BUILD_BASIC_GEOMETRY_FORBIDDEN", "Basic geometry is allowed only for the fixed runtime ground infrastructure role", {
        details: { actor_id: actorId, ue_path: pin.ue_path },
      });
    }
  }
  if (!MOBILITIES.has(value.mobility)) fail(code, `${pointer}.mobility is invalid`, { details: { pointer } });
  return {
    actor_id: actorId,
    infrastructure_kind: value.infrastructure_kind,
    asset_kind: value.asset_kind,
    class_path: classPath,
    asset_pin: pin,
    transform: normalizeTransform(value.transform, `${pointer}.transform`, code),
    mobility: value.mobility,
    collision: normalizeCollision(value.collision, `${pointer}.collision`, code),
  };
}

function normalizePlayerStart(value, pointer, code) {
  exactKeys(value, ["actor_name", "class_path", "pawn_class_path", "transform"], ["actor_name", "class_path", "pawn_class_path", "transform"], pointer, code);
  const actorName = requireString(value.actor_name, `${pointer}.actor_name`, { pattern: PLAYER_NAME_RE, max: 120, code });
  if (value.class_path !== PLAYER_START_CLASS) fail(code, `${pointer}.class_path must pin PlayerStart`, { details: { pointer } });
  const pawnClassPath = requireString(value.pawn_class_path, `${pointer}.pawn_class_path`, { pattern: UE_GAME_CLASS_RE, max: 512, code });
  return {
    actor_name: actorName,
    class_path: PLAYER_START_CLASS,
    pawn_class_path: pawnClassPath,
    transform: normalizeTransform(value.transform, `${pointer}.transform`, code),
  };
}

function normalizeCamera(value, pointer, code) {
  exactKeys(
    value,
    ["perspective", "fov_deg", "near_clip_cm", "eye_height_cm", "relative_rotation_deg", "target_actor_id"],
    ["perspective", "fov_deg", "near_clip_cm", "eye_height_cm", "relative_rotation_deg", "target_actor_id"],
    pointer,
    code,
  );
  if (value.perspective !== "first_person") fail(code, `${pointer}.perspective must be first_person`, { details: { pointer } });
  return {
    perspective: "first_person",
    fov_deg: requireNumber(value.fov_deg, `${pointer}.fov_deg`, { min: 30, max: 150, code }),
    near_clip_cm: requireNumber(value.near_clip_cm, `${pointer}.near_clip_cm`, { min: 0, max: 100, exclusiveMin: true, code }),
    eye_height_cm: requireNumber(value.eye_height_cm, `${pointer}.eye_height_cm`, { min: 50, max: 300, code }),
    relative_rotation_deg: normalizeVector(value.relative_rotation_deg, `${pointer}.relative_rotation_deg`, {
      min: -360000, max: 360000, code,
    }),
    target_actor_id: value.target_actor_id === null
      ? null
      : requireString(value.target_actor_id, `${pointer}.target_actor_id`, { pattern: SAFE_ID_RE, max: 120, code }),
  };
}

function normalizeEvidenceRequest(value, pointer, code) {
  exactKeys(value, ["evidence_id", "kind", "required"], ["evidence_id", "kind", "required"], pointer, code);
  const evidenceId = requireString(value.evidence_id, `${pointer}.evidence_id`, { pattern: SAFE_ID_RE, max: 120, code });
  if (!EVIDENCE_KINDS.has(value.kind)) fail(code, `${pointer}.kind is invalid`, { details: { pointer } });
  if (typeof value.required !== "boolean") fail(code, `${pointer}.required must be boolean`, { details: { pointer } });
  return { evidence_id: evidenceId, kind: value.kind, required: value.required };
}

function validateVistaSceneLayoutProfile(profile) {
  const code = "SCENE_BUILD_LAYOUT_INVALID";
  exactKeys(
    profile,
    [
      "schema", "profile_id", "source_visual_id", "layout_revision", "asset_snapshot_id",
      "content_revision", "verification_revision", "placements", "infrastructure",
      "player_start", "camera", "evidence_requests",
    ],
    [
      "schema", "profile_id", "source_visual_id", "layout_revision", "asset_snapshot_id",
      "content_revision", "verification_revision", "placements", "infrastructure",
      "player_start", "camera", "evidence_requests",
    ],
    "layoutProfile",
    code,
  );
  if (profile.schema !== LAYOUT_PROFILE_SCHEMA) fail(code, "Unsupported scene layout profile schema");
  const normalized = {
    schema: LAYOUT_PROFILE_SCHEMA,
    profile_id: requireString(profile.profile_id, "layoutProfile.profile_id", { pattern: SAFE_ID_RE, max: 120, code }),
    source_visual_id: requireString(profile.source_visual_id, "layoutProfile.source_visual_id", { pattern: /^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/, max: 80, code }),
    layout_revision: requireString(profile.layout_revision, "layoutProfile.layout_revision", { pattern: SAFE_REVISION_RE, max: 160, code }),
    asset_snapshot_id: requireString(profile.asset_snapshot_id, "layoutProfile.asset_snapshot_id", { pattern: SAFE_REVISION_RE, max: 160, code }),
    content_revision: requireString(profile.content_revision, "layoutProfile.content_revision", { pattern: SAFE_REVISION_RE, max: 160, code }),
    verification_revision: requireString(profile.verification_revision, "layoutProfile.verification_revision", { pattern: SAFE_REVISION_RE, max: 160, code }),
  };
  if (!Array.isArray(profile.placements) || !profile.placements.length || profile.placements.length > MAX_ACTORS) {
    fail(code, "layoutProfile.placements must be a non-empty bounded array");
  }
  if (!Array.isArray(profile.infrastructure) || profile.infrastructure.length > MAX_ACTORS) {
    fail(code, "layoutProfile.infrastructure must be a bounded array");
  }
  normalized.placements = profile.placements.map((item, index) => normalizePlacement(item, `layoutProfile.placements[${index}]`));
  normalized.infrastructure = profile.infrastructure.map((item, index) => normalizeInfrastructure(item, `layoutProfile.infrastructure[${index}]`));
  if (normalized.placements.length + normalized.infrastructure.length > MAX_ACTORS) {
    fail(code, `Scene layout exceeds ${MAX_ACTORS} actors`);
  }
  const actorIds = new Set();
  const sourceComponents = new Set();
  for (const actor of [...normalized.placements, ...normalized.infrastructure]) {
    if (actorIds.has(actor.actor_id)) fail(code, "Layout actor_id values must be unique", { details: { actor_id: actor.actor_id } });
    actorIds.add(actor.actor_id);
    if (actor.source_entity_id) {
      const key = `${actor.source_entity_id}\u0000${actor.component_id || "__primary__"}`;
      if (sourceComponents.has(key)) fail(code, "Layout source entity/component bindings must be unique", { details: { source_entity_id: actor.source_entity_id, component_id: actor.component_id } });
      sourceComponents.add(key);
    }
  }
  normalized.player_start = normalizePlayerStart(profile.player_start, "layoutProfile.player_start", code);
  normalized.camera = normalizeCamera(profile.camera, "layoutProfile.camera", code);
  if (normalized.camera.target_actor_id !== null && !actorIds.has(normalized.camera.target_actor_id)) {
    fail(code, "Camera target_actor_id does not reference a layout actor", { details: { target_actor_id: normalized.camera.target_actor_id } });
  }
  if (!Array.isArray(profile.evidence_requests) || profile.evidence_requests.length > 32) {
    fail(code, "layoutProfile.evidence_requests must be a bounded array");
  }
  normalized.evidence_requests = profile.evidence_requests
    .map((item, index) => normalizeEvidenceRequest(item, `layoutProfile.evidence_requests[${index}]`, code));
  const evidenceIds = new Set();
  for (const request of normalized.evidence_requests) {
    if (evidenceIds.has(request.evidence_id)) fail(code, "Evidence request ids must be unique", { details: { evidence_id: request.evidence_id } });
    evidenceIds.add(request.evidence_id);
  }
  normalized.placements.sort((left, right) => left.actor_id.localeCompare(right.actor_id));
  normalized.infrastructure.sort((left, right) => left.actor_id.localeCompare(right.actor_id));
  normalized.evidence_requests.sort((left, right) => left.evidence_id.localeCompare(right.evidence_id));
  return deepFreeze(normalized);
}

function validateSceneInput(scene) {
  if (!isPlainObject(scene) || scene.schema !== "vista-simworld-scene/v1") {
    fail("SCENE_BUILD_SCENE_INVALID", "Expected a vista-simworld-scene/v1 SceneSpec");
  }
  const sceneId = requireString(scene.scene_id, "scene.scene_id", { pattern: SCENE_ID_RE, max: 160, code: "SCENE_BUILD_SCENE_INVALID" });
  const sourceChecksum = scene.source && scene.source.source_checksum;
  if (typeof sourceChecksum !== "string" || !SHA256_RE.test(sourceChecksum)) {
    fail("SCENE_BUILD_SCENE_INVALID", "scene.source.source_checksum is invalid");
  }
  if (scene.provenance && scene.provenance.source_checksum !== sourceChecksum) {
    fail("SCENE_BUILD_SCENE_INVALID", "Scene provenance and source checksums differ");
  }
  const visualId = requireString(scene.source && scene.source.visual_id, "scene.source.visual_id", {
    pattern: /^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/, max: 80, code: "SCENE_BUILD_SCENE_INVALID",
  });
  if (!Array.isArray(scene.entities) || !scene.entities.length || scene.entities.length > MAX_ACTORS) {
    fail("SCENE_BUILD_SCENE_INVALID", "scene.entities must be a non-empty bounded array");
  }
  const entities = new Map();
  for (let index = 0; index < scene.entities.length; index += 1) {
    const entity = scene.entities[index];
    if (!isPlainObject(entity)) fail("SCENE_BUILD_SCENE_INVALID", `scene.entities[${index}] must be an object`);
    const id = requireString(entity.id, `scene.entities[${index}].id`, { pattern: SAFE_ID_RE, max: 120, code: "SCENE_BUILD_SCENE_INVALID" });
    if (entities.has(id)) fail("SCENE_BUILD_SCENE_INVALID", "Scene entity ids must be unique", { details: { entity_id: id } });
    if (typeof entity.required !== "boolean") fail("SCENE_BUILD_SCENE_INVALID", `scene.entities[${index}].required must be boolean`);
    entities.set(id, entity);
  }
  if (!Array.isArray(scene.unresolved)) fail("SCENE_BUILD_SCENE_INVALID", "scene.unresolved must be an array");
  const unresolvedAssets = new Set();
  for (const item of scene.unresolved) {
    if (!isPlainObject(item) || item.kind !== "asset" || item.blocking !== true) continue;
    if (typeof item.mapping_id === "string" && item.mapping_id.startsWith("unresolved-asset-")) {
      unresolvedAssets.add(item.mapping_id.slice("unresolved-asset-".length));
    }
  }
  const constraints = scene.camera && Array.isArray(scene.camera.constraints)
    ? [...new Set(scene.camera.constraints.map((value) => String(value)).filter(Boolean))].sort()
    : [];
  return { scene_id: sceneId, source_checksum: sourceChecksum, visual_id: visualId, entities, unresolved_assets: unresolvedAssets, camera_constraints: constraints };
}

function normalizeSceneBinding(binding, pointer, expectedSnapshot) {
  if (!isPlainObject(binding)) fail("SCENE_BUILD_ASSET_UNRESOLVED", `${pointer} has no selected real asset`, { details: { pointer } });
  const snapshotId = requireString(binding.snapshot_id, `${pointer}.snapshot_id`, { pattern: SAFE_REVISION_RE, max: 160, code: "SCENE_BUILD_ASSET_UNRESOLVED" });
  if (snapshotId !== expectedSnapshot) {
    fail("SCENE_BUILD_ASSET_SNAPSHOT_MISMATCH", `${pointer} does not use the layout asset snapshot`, {
      details: { pointer, expected_snapshot_id: expectedSnapshot, actual_snapshot_id: snapshotId },
    });
  }
  return {
    snapshot_id: snapshotId,
    asset_id: requireString(binding.asset_id, `${pointer}.asset_id`, { pattern: SAFE_REVISION_RE, max: 160, code: "SCENE_BUILD_ASSET_UNRESOLVED" }),
    ue_path: requireString(binding.ue_path, `${pointer}.ue_path`, { pattern: UE_OBJECT_PATH_RE, max: 512, code: "SCENE_BUILD_ASSET_UNRESOLVED" }),
    confidence: requireNumber(binding.confidence, `${pointer}.confidence`, { min: 0, max: 1, code: "SCENE_BUILD_ASSET_UNRESOLVED" }),
    verified: true,
  };
}

function safeActorSegment(value, max = 48) {
  const segment = String(value || "actor").replace(/[^A-Za-z0-9_]/g, "_").replace(/_+/g, "_").replace(/^_+|_+$/g, "") || "actor";
  return segment.slice(0, max);
}

function deterministicActorName(sceneId, actorId) {
  requireString(sceneId, "sceneId", { pattern: SCENE_ID_RE, max: 160, code: "SCENE_BUILD_PLAN_INVALID" });
  requireString(actorId, "actorId", { pattern: SAFE_ID_RE, max: 120, code: "SCENE_BUILD_PLAN_INVALID" });
  const [visualId, revision] = sceneId.split("@");
  const prefix = `VISTA_${safeActorSegment(visualId, 32)}_${revision.slice(0, 8)}_`;
  let suffix = safeActorSegment(actorId, 64);
  let name = `${prefix}${suffix}`;
  if (name.length > 120) {
    suffix = `${suffix.slice(0, Math.max(1, 120 - prefix.length - 13))}_${digest(actorId).slice(0, 12)}`;
    name = `${prefix}${suffix}`;
  }
  if (!ACTOR_NAME_RE.test(name)) fail("SCENE_BUILD_PLAN_INVALID", "Could not derive a safe deterministic actor name", { details: { actor_id: actorId } });
  return name;
}

function buildAsset({ bindingSource, assetKind, pin, classPath, profile }) {
  return {
    binding_source: bindingSource,
    kind: assetKind,
    snapshot_id: pin.snapshot_id,
    asset_id: pin.asset_id,
    ue_path: pin.ue_path,
    class_path: classPath,
    confidence: pin.confidence,
    content_revision: profile.content_revision,
    verification_revision: profile.verification_revision,
    verified: true,
  };
}

function actorWithFingerprint(actor) {
  return { ...actor, fingerprint: `vsa-${digest(actor).slice(0, 24)}` };
}

function compileVistaSceneBuildPlan(sceneSpec, layoutProfileInput) {
  const scene = validateSceneInput(sceneSpec);
  const profile = validateVistaSceneLayoutProfile(layoutProfileInput);
  if (profile.source_visual_id !== scene.visual_id) {
    fail("SCENE_BUILD_LAYOUT_MISMATCH", "Layout profile targets a different VISTA sample", {
      details: { expected_visual_id: scene.visual_id, actual_visual_id: profile.source_visual_id },
    });
  }
  const coveredEntities = new Set();
  const actors = [];
  for (const placement of profile.placements) {
    const entity = scene.entities.get(placement.source_entity_id);
    if (!entity) {
      fail("SCENE_BUILD_LAYOUT_INVALID", "Layout placement references an unknown SceneSpec entity", {
        details: { actor_id: placement.actor_id, source_entity_id: placement.source_entity_id },
      });
    }
    coveredEntities.add(placement.source_entity_id);
    let pin;
    if (placement.asset_source === "scene_binding") {
      if (scene.unresolved_assets.has(placement.source_entity_id)) {
        fail("SCENE_BUILD_ASSET_UNRESOLVED", "SceneSpec still marks a required scene binding unresolved", {
          details: { source_entity_id: placement.source_entity_id },
        });
      }
      pin = normalizeSceneBinding(entity.asset_binding, `scene.entities.${placement.source_entity_id}.asset_binding`, profile.asset_snapshot_id);
      validateAssetClass(placement.asset_kind, pin.ue_path, placement.class_path, `layoutProfile.placements.${placement.actor_id}`, "SCENE_BUILD_ASSET_UNRESOLVED");
    } else {
      pin = placement.asset_pin;
      if (pin.snapshot_id !== profile.asset_snapshot_id) {
        fail("SCENE_BUILD_ASSET_SNAPSHOT_MISMATCH", "Curated component does not use the layout asset snapshot", {
          details: { actor_id: placement.actor_id, expected_snapshot_id: profile.asset_snapshot_id, actual_snapshot_id: pin.snapshot_id },
        });
      }
    }
    if (isBasicGeometry(pin)) {
      fail("SCENE_BUILD_BASIC_GEOMETRY_FORBIDDEN", "Semantic scene entities cannot use basic geometry fallback", {
        details: { actor_id: placement.actor_id, source_entity_id: placement.source_entity_id, ue_path: pin.ue_path },
      });
    }
    const actor = {
      actor_id: placement.actor_id,
      actor_name: deterministicActorName(scene.scene_id, placement.actor_id),
      role: "scene_entity",
      source_entity_id: placement.source_entity_id,
      component_id: placement.component_id,
      infrastructure_kind: null,
      spawn_tool: placement.asset_kind === "static_mesh" ? "spawn_actor" : "spawn_blueprint_actor",
      asset: buildAsset({
        bindingSource: placement.asset_source,
        assetKind: placement.asset_kind,
        pin,
        classPath: placement.class_path,
        profile,
      }),
      transform: placement.transform,
      mobility: placement.mobility,
      collision: placement.collision,
    };
    actors.push(actorWithFingerprint(actor));
  }
  for (const [entityId, entity] of scene.entities) {
    if (entity.required === true && !coveredEntities.has(entityId)) {
      fail("SCENE_BUILD_LAYOUT_INCOMPLETE", "A required SceneSpec entity has no numeric placement", {
        details: { source_entity_id: entityId },
      });
    }
  }
  for (const infrastructure of profile.infrastructure) {
    const actor = {
      actor_id: infrastructure.actor_id,
      actor_name: deterministicActorName(scene.scene_id, infrastructure.actor_id),
      role: "infrastructure",
      source_entity_id: null,
      component_id: null,
      infrastructure_kind: infrastructure.infrastructure_kind,
      spawn_tool: infrastructure.asset_kind === "static_mesh" ? "spawn_actor" : "spawn_blueprint_actor",
      asset: buildAsset({
        bindingSource: "infrastructure",
        assetKind: infrastructure.asset_kind,
        pin: infrastructure.asset_pin,
        classPath: infrastructure.class_path,
        profile,
      }),
      transform: infrastructure.transform,
      mobility: infrastructure.mobility,
      collision: infrastructure.collision,
    };
    actors.push(actorWithFingerprint(actor));
  }
  actors.sort((left, right) => left.actor_id.localeCompare(right.actor_id));
  const planBody = {
    schema: BUILD_PLAN_SCHEMA,
    scene_id: scene.scene_id,
    scene_revision: scene.source_checksum,
    profile: "static_reconstruction",
    privilege: "reconstruction_only",
    layout_revision: profile.layout_revision,
    asset_snapshot_id: profile.asset_snapshot_id,
    content_revision: profile.content_revision,
    verification_revision: profile.verification_revision,
    coordinate_system: "unreal_centimeters_z_up",
    compiler: { name: COMPILER_NAME, version: COMPILER_VERSION },
    mutation_policy: {
      preflight: "strict",
      fail_before_mutation: true,
      actor_conflicts: "fail",
      rollback: "delete_new_actors_reverse_then_restore_player_start",
    },
    actors,
    player_start: profile.player_start,
    camera: {
      ...profile.camera,
      source_constraints: scene.camera_constraints,
    },
    evidence_requests: profile.evidence_requests,
  };
  const plan = { ...planBody, plan_id: `vsp-${digest(planBody).slice(0, 24)}` };
  validateVistaSceneBuildPlan(plan);
  return deepFreeze(plan);
}

function validatePlanAsset(asset, pointer, code) {
  exactKeys(
    asset,
    [
      "binding_source", "kind", "snapshot_id", "asset_id", "ue_path", "class_path",
      "confidence", "content_revision", "verification_revision", "verified",
    ],
    [
      "binding_source", "kind", "snapshot_id", "asset_id", "ue_path", "class_path",
      "confidence", "content_revision", "verification_revision", "verified",
    ],
    pointer,
    code,
  );
  if (!new Set(["scene_binding", "curated_component", "infrastructure"]).has(asset.binding_source)) fail(code, `${pointer}.binding_source is invalid`);
  if (!ASSET_KINDS.has(asset.kind)) fail(code, `${pointer}.kind is invalid`);
  const pin = normalizeAssetPin({
    snapshot_id: asset.snapshot_id,
    asset_id: asset.asset_id,
    ue_path: asset.ue_path,
    confidence: asset.confidence,
    verified: asset.verified,
  }, pointer, code);
  const classPath = validateAssetClass(asset.kind, pin.ue_path, asset.class_path, pointer, code);
  return {
    binding_source: asset.binding_source,
    kind: asset.kind,
    ...pin,
    class_path: classPath,
    content_revision: requireString(asset.content_revision, `${pointer}.content_revision`, { pattern: SAFE_REVISION_RE, max: 160, code }),
    verification_revision: requireString(asset.verification_revision, `${pointer}.verification_revision`, { pattern: SAFE_REVISION_RE, max: 160, code }),
  };
}

function validatePlanActor(actor, pointer, sceneId, plan) {
  const code = "SCENE_BUILD_PLAN_INVALID";
  exactKeys(
    actor,
    [
      "actor_id", "actor_name", "fingerprint", "role", "source_entity_id", "component_id",
      "infrastructure_kind", "spawn_tool", "asset", "transform", "mobility", "collision",
    ],
    [
      "actor_id", "actor_name", "fingerprint", "role", "source_entity_id", "component_id",
      "infrastructure_kind", "spawn_tool", "asset", "transform", "mobility", "collision",
    ],
    pointer,
    code,
  );
  const actorId = requireString(actor.actor_id, `${pointer}.actor_id`, { pattern: SAFE_ID_RE, max: 120, code });
  const actorName = requireString(actor.actor_name, `${pointer}.actor_name`, { pattern: ACTOR_NAME_RE, max: 120, code });
  if (actorName !== deterministicActorName(sceneId, actorId)) fail(code, `${pointer}.actor_name is not deterministic`, { details: { actor_id: actorId } });
  if (!/^vsa-[a-f0-9]{24}$/.test(actor.fingerprint)) fail(code, `${pointer}.fingerprint is invalid`);
  if (!new Set(["scene_entity", "infrastructure"]).has(actor.role)) fail(code, `${pointer}.role is invalid`);
  let sourceEntityId = null;
  let componentId = null;
  let infrastructureKind = null;
  if (actor.role === "scene_entity") {
    sourceEntityId = requireString(actor.source_entity_id, `${pointer}.source_entity_id`, { pattern: SAFE_ID_RE, max: 120, code });
    componentId = actor.component_id === null ? null : requireString(actor.component_id, `${pointer}.component_id`, { pattern: SAFE_ID_RE, max: 120, code });
    if (actor.infrastructure_kind !== null) fail(code, `${pointer}.infrastructure_kind must be null`);
  } else {
    if (actor.source_entity_id !== null || actor.component_id !== null || !INFRASTRUCTURE_KINDS.has(actor.infrastructure_kind)) {
      fail(code, `${pointer} infrastructure provenance is invalid`);
    }
    infrastructureKind = actor.infrastructure_kind;
  }
  const asset = validatePlanAsset(actor.asset, `${pointer}.asset`, code);
  if (asset.content_revision !== plan.content_revision || asset.verification_revision !== plan.verification_revision) {
    fail(code, `${pointer}.asset revisions do not match the plan`, { details: { actor_id: actorId } });
  }
  if (actor.role === "scene_entity") {
    if (asset.binding_source === "infrastructure" || asset.snapshot_id !== plan.asset_snapshot_id) {
      fail(code, `${pointer}.asset does not use the semantic asset snapshot`, { details: { actor_id: actorId } });
    }
    if (
      (asset.binding_source === "scene_binding" && componentId !== null)
      || (asset.binding_source === "curated_component" && componentId === null)
    ) {
      fail(code, `${pointer} component provenance does not match asset binding_source`, { details: { actor_id: actorId } });
    }
  } else if (asset.binding_source !== "infrastructure") {
    fail(code, `${pointer} infrastructure must use an infrastructure asset binding`, { details: { actor_id: actorId } });
  }
  const expectedTool = asset.kind === "static_mesh" ? "spawn_actor" : "spawn_blueprint_actor";
  if (actor.spawn_tool !== expectedTool) fail(code, `${pointer}.spawn_tool does not match the pinned asset class`);
  if (!MOBILITIES.has(actor.mobility)) fail(code, `${pointer}.mobility is invalid`);
  const transform = normalizeTransform(actor.transform, `${pointer}.transform`, code);
  const collision = normalizeCollision(actor.collision, `${pointer}.collision`, code);
  if (isBasicGeometry(asset)) {
    const allowedGround = actor.role === "infrastructure"
      && infrastructureKind === "runtime_ground"
      && asset.ue_path === ENGINE_RUNTIME_GROUND
      && asset.kind === "static_mesh"
      && actor.mobility === "static"
      && collision.mode === "query_and_physics"
      && collision.profile_name === "BlockAll";
    if (!allowedGround) fail("SCENE_BUILD_BASIC_GEOMETRY_FORBIDDEN", "BuildPlan contains forbidden basic geometry", { details: { actor_id: actorId } });
  }
  const normalized = {
    actor_id: actorId,
    actor_name: actorName,
    role: actor.role,
    source_entity_id: sourceEntityId,
    component_id: componentId,
    infrastructure_kind: infrastructureKind,
    spawn_tool: actor.spawn_tool,
    asset,
    transform,
    mobility: actor.mobility,
    collision,
  };
  const expectedFingerprint = `vsa-${digest(normalized).slice(0, 24)}`;
  if (actor.fingerprint !== expectedFingerprint) fail(code, `${pointer}.fingerprint does not match actor contents`, { details: { actor_id: actorId } });
  return { ...normalized, fingerprint: actor.fingerprint };
}

function validateVistaSceneBuildPlan(plan) {
  const code = "SCENE_BUILD_PLAN_INVALID";
  exactKeys(
    plan,
    [
      "schema", "plan_id", "scene_id", "scene_revision", "profile", "privilege",
      "layout_revision", "asset_snapshot_id", "content_revision", "verification_revision",
      "coordinate_system", "compiler", "mutation_policy", "actors", "player_start",
      "camera", "evidence_requests",
    ],
    [
      "schema", "plan_id", "scene_id", "scene_revision", "profile", "privilege",
      "layout_revision", "asset_snapshot_id", "content_revision", "verification_revision",
      "coordinate_system", "compiler", "mutation_policy", "actors", "player_start",
      "camera", "evidence_requests",
    ],
    "plan",
    code,
  );
  if (plan.schema !== BUILD_PLAN_SCHEMA || plan.profile !== "static_reconstruction" || plan.privilege !== "reconstruction_only") {
    fail(code, "Unsupported BuildPlan schema, profile, or privilege");
  }
  const sceneId = requireString(plan.scene_id, "plan.scene_id", { pattern: SCENE_ID_RE, max: 160, code });
  requireString(plan.scene_revision, "plan.scene_revision", { pattern: SHA256_RE, max: 64, code });
  requireString(plan.layout_revision, "plan.layout_revision", { pattern: SAFE_REVISION_RE, max: 160, code });
  requireString(plan.asset_snapshot_id, "plan.asset_snapshot_id", { pattern: SAFE_REVISION_RE, max: 160, code });
  requireString(plan.content_revision, "plan.content_revision", { pattern: SAFE_REVISION_RE, max: 160, code });
  requireString(plan.verification_revision, "plan.verification_revision", { pattern: SAFE_REVISION_RE, max: 160, code });
  if (plan.coordinate_system !== "unreal_centimeters_z_up") fail(code, "BuildPlan coordinate system is unsupported");
  exactKeys(plan.compiler, ["name", "version"], ["name", "version"], "plan.compiler", code);
  if (plan.compiler.name !== COMPILER_NAME || plan.compiler.version !== COMPILER_VERSION) fail(code, "BuildPlan compiler identity is unsupported");
  exactKeys(
    plan.mutation_policy,
    ["preflight", "fail_before_mutation", "actor_conflicts", "rollback"],
    ["preflight", "fail_before_mutation", "actor_conflicts", "rollback"],
    "plan.mutation_policy",
    code,
  );
  if (
    plan.mutation_policy.preflight !== "strict"
    || plan.mutation_policy.fail_before_mutation !== true
    || plan.mutation_policy.actor_conflicts !== "fail"
    || plan.mutation_policy.rollback !== "delete_new_actors_reverse_then_restore_player_start"
  ) fail(code, "BuildPlan mutation policy is unsafe");
  if (!Array.isArray(plan.actors) || !plan.actors.length || plan.actors.length > MAX_ACTORS) fail(code, "BuildPlan actors must be a non-empty bounded array");
  const actorIds = new Set();
  const actorNames = new Set();
  let previousActorId = null;
  const normalizedActors = [];
  for (let index = 0; index < plan.actors.length; index += 1) {
    const actor = validatePlanActor(plan.actors[index], `plan.actors[${index}]`, sceneId, plan);
    if (actorIds.has(actor.actor_id) || actorNames.has(actor.actor_name)) fail(code, "BuildPlan actor ids and names must be unique");
    if (previousActorId !== null && previousActorId.localeCompare(actor.actor_id) >= 0) fail(code, "BuildPlan actors must be sorted by actor_id");
    actorIds.add(actor.actor_id);
    actorNames.add(actor.actor_name);
    previousActorId = actor.actor_id;
    normalizedActors.push(actor);
  }
  normalizePlayerStart(plan.player_start, "plan.player_start", code);
  exactKeys(
    plan.camera,
    ["perspective", "fov_deg", "near_clip_cm", "eye_height_cm", "relative_rotation_deg", "target_actor_id", "source_constraints"],
    ["perspective", "fov_deg", "near_clip_cm", "eye_height_cm", "relative_rotation_deg", "target_actor_id", "source_constraints"],
    "plan.camera",
    code,
  );
  const camera = normalizeCamera({
    perspective: plan.camera.perspective,
    fov_deg: plan.camera.fov_deg,
    near_clip_cm: plan.camera.near_clip_cm,
    eye_height_cm: plan.camera.eye_height_cm,
    relative_rotation_deg: plan.camera.relative_rotation_deg,
    target_actor_id: plan.camera.target_actor_id,
  }, "plan.camera", code);
  if (camera.target_actor_id !== null && !actorIds.has(camera.target_actor_id)) fail(code, "BuildPlan camera target does not reference an actor");
  if (!Array.isArray(plan.camera.source_constraints) || plan.camera.source_constraints.length > 64) fail(code, "BuildPlan camera constraints are invalid");
  const constraints = plan.camera.source_constraints.map((value, index) => requireString(value, `plan.camera.source_constraints[${index}]`, { max: 200, code }));
  if (new Set(constraints).size !== constraints.length || [...constraints].sort().some((value, index) => value !== constraints[index])) {
    fail(code, "BuildPlan camera constraints must be unique and sorted");
  }
  if (!Array.isArray(plan.evidence_requests) || plan.evidence_requests.length > 32) fail(code, "BuildPlan evidence requests are invalid");
  const evidenceIds = new Set();
  let previousEvidenceId = null;
  for (let index = 0; index < plan.evidence_requests.length; index += 1) {
    const request = normalizeEvidenceRequest(plan.evidence_requests[index], `plan.evidence_requests[${index}]`, code);
    if (evidenceIds.has(request.evidence_id)) fail(code, "BuildPlan evidence ids must be unique");
    if (previousEvidenceId !== null && previousEvidenceId.localeCompare(request.evidence_id) >= 0) fail(code, "BuildPlan evidence requests must be sorted by evidence_id");
    evidenceIds.add(request.evidence_id);
    previousEvidenceId = request.evidence_id;
  }
  const { plan_id: _planId, ...body } = plan;
  const expectedPlanId = `vsp-${digest(body).slice(0, 24)}`;
  if (plan.plan_id !== expectedPlanId) fail(code, "BuildPlan plan_id does not match its canonical contents");
  return plan;
}

module.exports = {
  BUILD_PLAN_SCHEMA,
  LAYOUT_PROFILE_SCHEMA,
  COMPILER_NAME,
  COMPILER_VERSION,
  STATIC_MESH_ACTOR_CLASS,
  PLAYER_START_CLASS,
  ENGINE_RUNTIME_GROUND,
  VistaSceneBuildError,
  compileVistaSceneBuildPlan,
  deterministicActorName,
  isBasicGeometry,
  validateVistaSceneBuildPlan,
  validateVistaSceneLayoutProfile,
};
