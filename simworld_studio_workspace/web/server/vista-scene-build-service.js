"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const { compileVistaSceneBuildPlan } = require("./vista-scene-build-plan");
const { createVistaRuntimeSceneProof } = require("./vista-runtime-broker");

const PLAN_RESPONSE_SCHEMA = "vista-scene-build-plan-response/v1";
const PREFLIGHT_RESPONSE_SCHEMA = "vista-scene-build-service-preflight/v1";
const EXECUTION_RESPONSE_SCHEMA = "vista-scene-build-service-execution/v1";
const BUILD_RECORD_SCHEMA = "vista-scene-build-record/v1";
const PROFILE_ID_PATTERN = /^[a-z0-9][a-z0-9._-]{0,79}$/;
const PLAN_ID_PATTERN = /^vsp-[a-f0-9]{24}$/;
const PRINCIPAL_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$/;
const MAX_RECORD_BYTES = 32 * 1024 * 1024;
const TERMINAL_BUILD_STATES = new Set(["succeeded", "already_applied", "failed"]);

class VistaSceneBuildServiceError extends Error {
  constructor(code, message, options = {}) {
    super(message);
    this.name = "VistaSceneBuildServiceError";
    this.code = code;
    this.status = Number.isInteger(options.status) ? options.status : 400;
    this.retryable = options.retryable === true;
    if (options.result) this.result = options.result;
  }
}

function fail(code, message, options) {
  throw new VistaSceneBuildServiceError(code, message, options);
}

function isPlainObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function exactKeys(value, allowed, required, pointer = "request") {
  if (!isPlainObject(value)) fail("SCENE_BUILD_REQUEST_INVALID", `${pointer} must be an object`);
  const allowedSet = new Set(allowed);
  const unknown = Object.keys(value).filter((key) => !allowedSet.has(key));
  const missing = required.filter((key) => !Object.prototype.hasOwnProperty.call(value, key));
  if (unknown.length || missing.length) {
    fail("SCENE_BUILD_REQUEST_INVALID", `${pointer} has an invalid shape`);
  }
  return value;
}

function normalizeProfileId(value, { optional = false } = {}) {
  if ((value === undefined || value === null || value === "") && optional) return null;
  const profileId = typeof value === "string" ? value.trim() : "";
  if (!PROFILE_ID_PATTERN.test(profileId)) {
    fail("SCENE_BUILD_PROFILE_INVALID", "Scene layout profile id is invalid");
  }
  return profileId;
}

function normalizePlanId(value) {
  const planId = typeof value === "string" ? value.trim() : "";
  if (!PLAN_ID_PATTERN.test(planId)) fail("SCENE_BUILD_PLAN_ID_INVALID", "Scene BuildPlan id is invalid");
  return planId;
}

function normalizeAccess(context) {
  if (!isPlainObject(context)) fail("SCENE_BUILD_ACCESS_INVALID", "Scene build access context is required", { status: 400 });
  const ownerId = typeof context.ownerId === "string" ? context.ownerId.trim() : "";
  const sessionId = typeof context.sessionId === "string" ? context.sessionId.trim() : "";
  const leaseId = typeof context.leaseId === "string" ? context.leaseId.trim() : "";
  const slotId = Number(context.slotId);
  const mcpPort = Number(context.mcpPort);
  if (!PRINCIPAL_PATTERN.test(ownerId) || !PRINCIPAL_PATTERN.test(sessionId)
      || !PRINCIPAL_PATTERN.test(leaseId)
      || !Number.isSafeInteger(slotId) || slotId < 0 || slotId > 1023
      || !Number.isSafeInteger(mcpPort) || mcpPort < 1 || mcpPort > 65535) {
    fail("SCENE_BUILD_ACCESS_INVALID", "Scene build access context is invalid", { status: 400 });
  }
  return { ownerId, sessionId, leaseId, slotId, mcpPort };
}

function liveProofKey(access) {
  return crypto.createHash("sha256").update(JSON.stringify({
    owner_id: access.ownerId,
    session_id: access.sessionId,
    slot_id: access.slotId,
    lease_id: access.leaseId,
    mcp_port: access.mcpPort,
  }), "utf8").digest("hex");
}

function proofDigest(value) {
  return crypto.createHash("sha256").update(JSON.stringify(value), "utf8").digest("hex");
}

function realGameAsset(value) {
  return typeof value === "string"
    && /^\/Game\/[A-Za-z0-9_./-]+$/.test(value)
    && !/\/Engine\/BasicShapes\//i.test(value)
    && !/(?:^|[._/-])(?:cube|plane|sphere|cylinder|cone|runtime_ground|fallback|placeholder)(?:$|[._/-])/i.test(value);
}

function productionActorProvenance(resolved, planned) {
  if (!isPlainObject(planned) || !isPlainObject(planned.asset)
      || planned.asset.verified !== true
      || !realGameAsset(planned.asset.ue_path)
      || planned.asset.content_revision !== resolved.plan.content_revision
      || planned.asset.verification_revision !== resolved.plan.verification_revision) {
    fail("SCENE_BUILD_RUNTIME_PROOF_INVALID", "BuildPlan contains an unverified or fallback production asset", {
      status: 409,
    });
  }
  if (planned.role === "infrastructure") {
    if (planned.asset.binding_source !== "infrastructure"
        || planned.source_entity_id !== null
        || typeof planned.infrastructure_kind !== "string") {
      fail("SCENE_BUILD_RUNTIME_PROOF_INVALID", "BuildPlan infrastructure provenance is invalid", { status: 409 });
    }
    return {
      source_kind: "verified_layout_infrastructure",
      source_entity_id: null,
      infrastructure_kind: planned.infrastructure_kind,
      snapshot_id: planned.asset.snapshot_id,
      asset_id: planned.asset.asset_id,
      ue_path: planned.asset.ue_path,
      selected_by: "verified_layout_profile",
    };
  }
  const entities = resolved.artifact.scene_spec.entities;
  const entity = entities.find((candidate) => candidate && candidate.id === planned.source_entity_id);
  const resolution = entity && entity.asset_resolution;
  const binding = entity && entity.asset_binding;
  if (!entity || !isPlainObject(resolution) || !isPlainObject(binding)
      || !isPlainObject(resolution.selected_binding)
      || resolution.selected_binding.asset_id !== binding.asset_id
      || resolution.selected_binding.ue_path !== binding.ue_path
      || resolution.selected_binding.snapshot_id !== binding.snapshot_id
      || !realGameAsset(binding.ue_path)
      || !new Set(["automatic", "manual_override"]).has(resolution.selected_by)
      || (resolution.selected_by === "automatic"
        && (!Array.isArray(resolution.candidates)
          || !resolution.candidates.some((candidate) => candidate.origin === "search"
            && candidate.asset_id === binding.asset_id && candidate.ue_path === binding.ue_path)))
      || (resolution.selected_by === "manual_override"
        && (!isPlainObject(resolution.manual_override)
          || resolution.manual_override.confirmed !== true
          || resolution.manual_override.asset_id !== binding.asset_id
          || resolution.manual_override.ue_path !== binding.ue_path))) {
    fail("SCENE_BUILD_RUNTIME_PROOF_INVALID", "BuildPlan actor lacks a verified non-fallback semantic binding", {
      status: 409,
    });
  }
  return {
    source_kind: planned.asset.binding_source === "curated_component"
      ? "verified_semantic_component"
      : "verified_semantic_binding",
    source_entity_id: planned.source_entity_id,
    infrastructure_kind: null,
    snapshot_id: binding.snapshot_id,
    asset_id: binding.asset_id,
    ue_path: binding.ue_path,
    selected_by: resolution.selected_by,
  };
}

function assertProductionRuntimePlan(resolved) {
  if (!isPlainObject(resolved) || !isPlainObject(resolved.plan)
      || !isPlainObject(resolved.artifact) || !isPlainObject(resolved.artifact.scene_spec)
      || !Array.isArray(resolved.artifact.scene_spec.entities)
      || !Array.isArray(resolved.plan.actors) || resolved.plan.actors.length < 1) {
    fail("SCENE_BUILD_RUNTIME_PROOF_INVALID", "BuildPlan cannot establish production scene provenance", {
      status: 409,
    });
  }
  return new Map(resolved.plan.actors.map((planned) => [
    planned.actor_id,
    productionActorProvenance(resolved, planned),
  ]));
}

function runtimeProofFromBuild(resolved, result) {
  if (!isPlainObject(resolved.artifact) || !isPlainObject(resolved.artifact.scene_spec)
      || !Array.isArray(resolved.artifact.scene_spec.entities)
      || !Array.isArray(result.actor_manifest) || result.actor_manifest.length !== resolved.plan.actors.length) {
    fail("SCENE_BUILD_RUNTIME_PROOF_INVALID", "Successful scene build lacks an exact verified actor manifest", {
      status: 500,
    });
  }
  const provenanceById = assertProductionRuntimePlan(resolved);
  const plannedById = new Map(resolved.plan.actors.map((actor) => [actor.actor_id, actor]));
  const contentRevisions = new Set();
  const verificationRevisions = new Set();
  const assetEvidence = [];
  const semanticEvidence = [];
  for (const entry of result.actor_manifest) {
    const planned = plannedById.get(entry && entry.actor_id);
    const provenance = planned && provenanceById.get(planned.actor_id);
    if (!planned || !isPlainObject(entry.asset)
        || entry.actor_name !== planned.actor_name
        || entry.fingerprint !== planned.fingerprint
        || entry.runtime_actor_name !== planned.actor_name
        || !/^vso-[a-f0-9]{24}$/.test(String(entry.operation_id || ""))
        || !/^[A-Fa-f0-9-]{16,64}$/.test(String(entry.object_guid || ""))
        || !new Set(["spawned", "reused"]).has(entry.disposition)
        || entry.asset.asset_id !== planned.asset.asset_id
        || entry.asset.ue_path !== planned.asset.ue_path
        || entry.asset.binding_source !== planned.asset.binding_source
        || entry.asset.snapshot_id !== planned.asset.snapshot_id
        || entry.asset.content_revision !== resolved.plan.content_revision
        || entry.asset.verification_revision !== resolved.plan.verification_revision
        || entry.asset.verified !== true
        || !realGameAsset(entry.asset.ue_path)
        || !provenance) {
      fail("SCENE_BUILD_RUNTIME_PROOF_INVALID", "Runtime actor is not backed by a verified non-fallback semantic /Game asset", {
        status: 409,
      });
    }
    contentRevisions.add(entry.asset.content_revision);
    verificationRevisions.add(entry.asset.verification_revision);
    assetEvidence.push({
      actor_id: entry.actor_id,
      asset_id: entry.asset.asset_id,
      ue_path: entry.asset.ue_path,
      content_revision: entry.asset.content_revision,
      verification_revision: entry.asset.verification_revision,
    });
    semanticEvidence.push({ actor_id: planned.actor_id, ...provenance });
  }
  if (contentRevisions.size !== 1 || verificationRevisions.size !== 1) {
    fail("SCENE_BUILD_RUNTIME_PROOF_INVALID", "Successful scene build has mixed or missing content revisions", {
      status: 500,
    });
  }
  const evidenceByKind = new Map((result.evidence || []).map((entry) => [entry && entry.kind, entry]));
  for (const kind of ["actor_snapshot", "collision_report", "floating_report", "screenshot"]) {
    const entry = evidenceByKind.get(kind);
    if (!isPlainObject(entry) || entry.required !== true || entry.status !== "captured"
        || !isPlainObject(entry.artifact) || entry.error !== null) {
      fail("SCENE_BUILD_RUNTIME_PROOF_INVALID", `Required ${kind} evidence did not pass`, { status: 409 });
    }
  }
  const actorEvidence = evidenceByKind.get("actor_snapshot").artifact;
  const collisionEvidence = evidenceByKind.get("collision_report").artifact;
  const floatingEvidence = evidenceByKind.get("floating_report").artifact;
  const screenshotEvidence = evidenceByKind.get("screenshot").artifact;
  if (actorEvidence.schema !== "vista-scene-actor-snapshot/v1"
      || !Array.isArray(actorEvidence.actors) || actorEvidence.actors.length !== result.actor_manifest.length
      || !/^[a-f0-9]{64}$/.test(actorEvidence.scene_digest)
      || collisionEvidence.schema !== "vista-scene-collision-report/v1"
      || collisionEvidence.scene_digest !== actorEvidence.scene_digest
      || collisionEvidence.collision_count !== 0
      || !Array.isArray(collisionEvidence.collisions) || collisionEvidence.collisions.length !== 0
      || floatingEvidence.schema !== "vista-scene-floating-report/v1"
      || floatingEvidence.scene_digest !== actorEvidence.scene_digest
      || floatingEvidence.floating_count !== 0
      || !Array.isArray(floatingEvidence.floating) || floatingEvidence.floating.length !== 0
      || screenshotEvidence.schema !== "vista-scene-screenshot/v1"
      || screenshotEvidence.scene_digest !== actorEvidence.scene_digest
      || !/^[a-f0-9]{64}$/.test(screenshotEvidence.sha256)
      || !Number.isSafeInteger(screenshotEvidence.bytes) || screenshotEvidence.bytes < 8) {
    fail("SCENE_BUILD_RUNTIME_PROOF_INVALID", "Scene validation evidence is incomplete or inconsistent", {
      status: 409,
    });
  }
  const manifestByName = new Map(result.actor_manifest.map((entry) => [entry.actor_name, entry]));
  const materialNames = new Set();
  const liveSurfaceActors = [];
  const materialEvidence = actorEvidence.actors.map((actor) => {
    const manifestActor = actor && manifestByName.get(actor.actor_name);
    if (!isPlainObject(actor) || !manifestActor || materialNames.has(actor.actor_name)
        || actor.fingerprint !== manifestActor.fingerprint
        || actor.operation_id !== manifestActor.operation_id
        || actor.object_guid !== manifestActor.object_guid
        || actor.class_path !== manifestActor.asset.class_path
        || actor.asset_path !== manifestActor.asset.ue_path
        || !realGameAsset(actor.asset_path)
        || !Array.isArray(actor.materials) || actor.materials.length < 1
        || actor.materials.some((material) => !isPlainObject(material)
          || material.pbr_eligible !== true
          || !realGameAsset(material.material_path)
          || !Number.isSafeInteger(material.slot_index) || material.slot_index < 0
          || typeof material.component !== "string" || !material.component
          || typeof material.material_class !== "string" || !material.material_class)) {
      fail("SCENE_BUILD_RUNTIME_PROOF_INVALID", "Every runtime actor must pass real /Game material/PBR evidence", {
        status: 409,
      });
    }
    materialNames.add(actor.actor_name);
    const liveMaterials = actor.materials.map((material) => ({
      component: material.component,
      material_class: material.material_class,
      material_path: material.material_path,
      pbr_eligible: true,
      slot_index: material.slot_index,
    })).sort((left, right) => (
      left.component.localeCompare(right.component)
      || left.slot_index - right.slot_index
      || left.material_path.localeCompare(right.material_path)
      || left.material_class.localeCompare(right.material_class)
    ));
    liveSurfaceActors.push({
      actor_name: actor.actor_name,
      asset_path: actor.asset_path,
      class_path: actor.class_path,
      fingerprint: actor.fingerprint,
      materials: liveMaterials,
      object_guid: actor.object_guid,
      operation_id: actor.operation_id,
    });
    return {
      actor_name: actor.actor_name,
      materials: actor.materials.map((material) => ({
        component: material.component,
        slot_index: material.slot_index,
        material_path: material.material_path,
        material_class: material.material_class,
        pbr_eligible: true,
      })),
    };
  });
  if (materialNames.size !== manifestByName.size) {
    fail("SCENE_BUILD_RUNTIME_PROOF_INVALID", "Material/PBR evidence does not cover the exact actor manifest", {
      status: 409,
    });
  }
  const receipt = actorEvidence.content_receipt;
  if (!isPlainObject(receipt)
      || receipt.schema !== "simworld-ue-content-receipt/v1"
      || receipt.content_revision !== resolved.plan.content_revision
      || receipt.verification_revision !== resolved.plan.verification_revision
      || !/^[a-f0-9]{64}$/.test(String(receipt.receipt_sha256 || ""))) {
    fail("SCENE_BUILD_RUNTIME_PROOF_INVALID", "Immutable content revision receipt evidence is invalid", {
      status: 409,
    });
  }
  liveSurfaceActors.sort((left, right) => (
    left.actor_name.localeCompare(right.actor_name)
    || left.fingerprint.localeCompare(right.fingerprint)
    || left.operation_id.localeCompare(right.operation_id)
    || left.object_guid.localeCompare(right.object_guid)
  ));
  const liveSurface = {
    actors: liveSurfaceActors,
    content_receipt: {
      content_revision: receipt.content_revision,
      receipt_sha256: receipt.receipt_sha256,
      schema: receipt.schema,
      verification_revision: receipt.verification_revision,
    },
  };
  assetEvidence.sort((left, right) => left.actor_id.localeCompare(right.actor_id));
  semanticEvidence.sort((left, right) => left.actor_id.localeCompare(right.actor_id));
  materialEvidence.sort((left, right) => left.actor_name.localeCompare(right.actor_name));
  const evidenceBundle = {
    scene_digest: actorEvidence.scene_digest,
    screenshot_sha256: screenshotEvidence.sha256,
    collision_count: 0,
    floating_count: 0,
  };
  return createVistaRuntimeSceneProof({
    planId: resolved.plan.plan_id,
    sceneId: resolved.plan.scene_id,
    actorManifest: result.actor_manifest,
    contentRevision: [...contentRevisions][0],
    verificationRevision: [...verificationRevisions][0],
    assetEvidenceDigest: proofDigest(assetEvidence),
    semanticBindingDigest: proofDigest(semanticEvidence),
    materialPbrEvidenceDigest: proofDigest(materialEvidence),
    evidenceBundleDigest: proofDigest(evidenceBundle),
    liveSurfaceDigest: proofDigest(liveSurface),
  });
}

function normalizeRegistry(registry) {
  if (!isPlainObject(registry)) throw new TypeError("layoutProfiles must be an object keyed by profile id");
  const normalized = new Map();
  for (const [rawId, profile] of Object.entries(registry)) {
    const profileId = normalizeProfileId(rawId);
    if (!isPlainObject(profile) || profile.profile_id !== profileId) {
      throw new TypeError(`layoutProfiles.${profileId} must contain the matching profile_id`);
    }
    if (normalized.has(profileId)) throw new TypeError(`Duplicate scene layout profile '${profileId}'`);
    normalized.set(profileId, profile);
  }
  return normalized;
}

function recordPath(root, planId, ownerId) {
  const checked = normalizePlanId(planId);
  const ownerDigest = crypto.createHash("sha256").update(String(ownerId), "utf8").digest("hex").slice(0, 24);
  const target = path.resolve(root, `${checked}-${ownerDigest}.json`);
  if (path.dirname(target) !== root) fail("SCENE_BUILD_STORAGE_UNAVAILABLE", "Scene build record path is invalid", { status: 500 });
  return target;
}

function nowIso(clock) {
  const value = clock();
  const date = value instanceof Date ? value : new Date(value);
  if (!Number.isFinite(date.getTime())) fail("SCENE_BUILD_CLOCK_INVALID", "Scene build clock is invalid", { status: 500 });
  return date.toISOString();
}

function safeExecutionError(error) {
  if (error instanceof VistaSceneBuildServiceError) return error;
  const code = error && typeof error.code === "string" && /^[A-Z0-9_]{3,100}$/.test(error.code)
    ? error.code
    : "SCENE_BUILD_EXECUTION_FAILED";
  return new VistaSceneBuildServiceError(code, String(error && error.message || "Scene build execution failed").slice(0, 500), {
    status: Number.isInteger(error && error.status) ? error.status : 503,
    retryable: Boolean(error && error.retryable),
    result: error && error.result,
  });
}

function validateExecutorResult(result, plan) {
  if (!isPlainObject(result)
      || result.schema !== "vista-scene-build-result/v1"
      || result.plan_id !== plan.plan_id
      || result.scene_id !== plan.scene_id
      || !new Set(["succeeded", "already_applied", "failed"]).has(result.status)) {
    fail("SCENE_BUILD_RESULT_INVALID", "UE scene executor returned an invalid build result", { status: 502 });
  }
  return result;
}

function conservativeFailureResult(plan, error) {
  return {
    schema: "vista-scene-build-result/v1",
    build_id: `vsb-${plan.plan_id.slice(4)}`,
    plan_id: plan.plan_id,
    scene_id: plan.scene_id,
    status: "failed",
    mutation_count: 0,
    actor_manifest: [],
    player_start: { application_status: "unknown" },
    camera: { application_status: "unknown" },
    evidence: [],
    error: {
      code: error.code,
      retryable: error.retryable === true,
    },
    rollback: {
      state: "partial",
      deleted_actor_names: [],
      restored_player_start: false,
      failures: [{ operation: "execution", code: error.code }],
    },
  };
}

class VistaSceneBuildService {
  constructor(options = {}) {
    if (!options.importService || typeof options.importService.status !== "function") {
      throw new TypeError("VistaSceneBuildService requires importService.status");
    }
    if (typeof options.recordRoot !== "string" || !options.recordRoot.trim()) {
      throw new TypeError("VistaSceneBuildService requires recordRoot");
    }
    const recordRoot = path.resolve(options.recordRoot);
    if (recordRoot === path.parse(recordRoot).root) throw new TypeError("recordRoot cannot be a filesystem root");
    if (options.executor !== null && options.executor !== undefined
        && (typeof options.executor.preflight !== "function" || typeof options.executor.execute !== "function")) {
      throw new TypeError("executor must expose preflight and execute");
    }
    this.importService = options.importService;
    this.layoutProfiles = normalizeRegistry(options.layoutProfiles || {});
    this.recordRoot = recordRoot;
    this.executor = options.executor || null;
    if (options.artifactRecorder !== undefined && options.artifactRecorder !== null
        && (typeof options.artifactRecorder.ensureSceneBuildTerminal !== "function"
          || typeof options.artifactRecorder.sceneBuildLineage !== "function")) {
      throw new TypeError("artifactRecorder must expose scene build journal methods");
    }
    this.artifactRecorder = options.artifactRecorder || null;
    this.clock = typeof options.clock === "function" ? options.clock : () => new Date();
    this.randomBytes = typeof options.randomBytes === "function" ? options.randomBytes : crypto.randomBytes;
    this.maxRecordBytes = Number.isSafeInteger(options.maxRecordBytes) && options.maxRecordBytes > 0
      ? options.maxRecordBytes
      : MAX_RECORD_BYTES;
    this.activeExecutions = new Map();
    // Runtime Play is intentionally process-local: after a server restart the
    // caller must explicitly reconcile/rebuild rather than trusting a stale
    // disk record as proof of the editor world's current contents.
    this.liveRuntimeProofs = new Map();
  }

  async plan(importArtifactId, request = {}, context = {}) {
    exactKeys(request, ["profile_id"], []);
    const resolved = await this._resolvePlan(importArtifactId, request.profile_id, context);
    const record = await this._readRecord(resolved.plan.plan_id, resolved.access, { allowMissing: true });
    await this._ensureTerminalJournal(resolved, record);
    return this._planResponse(resolved, record);
  }

  async status(importArtifactId, request = {}, context = {}) {
    exactKeys(request, ["profile_id"], []);
    const resolved = await this._resolvePlan(importArtifactId, request.profile_id, context);
    const record = await this._readRecord(resolved.plan.plan_id, resolved.access, { allowMissing: true });
    await this._ensureTerminalJournal(resolved, record);
    return this._planResponse(resolved, record);
  }

  async preflight(importArtifactId, request = {}, context = {}) {
    exactKeys(request, ["plan_id", "profile_id"], ["plan_id"]);
    const resolved = await this._resolvePlan(importArtifactId, request.profile_id, context);
    this._assertPlanId(resolved.plan, request.plan_id);
    this._requireExecutor();
    assertProductionRuntimePlan(resolved);
    const result = await this.executor.preflight(resolved.plan, {
      signal: context.signal,
      ownerId: resolved.access.ownerId,
      sessionId: resolved.access.sessionId,
      slotId: resolved.access.slotId,
      leaseId: resolved.access.leaseId,
      mcpPort: resolved.access.mcpPort,
    });
    if (!isPlainObject(result)
        || result.schema !== "vista-scene-build-preflight-result/v1"
        || result.plan_id !== resolved.plan.plan_id
        || result.ready !== true) {
      fail("SCENE_BUILD_PREFLIGHT_RESULT_INVALID", "UE scene executor returned an invalid preflight result", { status: 502 });
    }
    return {
      schema: PREFLIGHT_RESPONSE_SCHEMA,
      import_artifact_id: resolved.importArtifactId,
      profile_id: resolved.profileId,
      plan_id: resolved.plan.plan_id,
      ready: result.ready === true,
      preflight: result,
    };
  }

  async start(importArtifactId, request = {}, context = {}) {
    const started = await this._beginExecution(importArtifactId, request, context);
    started.promise.catch(() => {});
    return started.response;
  }

  async execute(importArtifactId, request = {}, context = {}) {
    const started = await this._beginExecution(importArtifactId, request, context);
    return started.promise;
  }

  resolveActiveRuntimeProof(context = {}) {
    const access = normalizeAccess(context);
    return this.liveRuntimeProofs.get(liveProofKey(access)) || null;
  }

  async _beginExecution(importArtifactId, request = {}, context = {}) {
    exactKeys(request, ["plan_id", "profile_id", "confirm"], ["plan_id", "confirm"]);
    if (request.confirm !== true) fail("SCENE_BUILD_CONFIRMATION_REQUIRED", "Exact BuildPlan confirmation is required", { status: 428 });
    const resolved = await this._resolvePlan(importArtifactId, request.profile_id, context);
    this._assertPlanId(resolved.plan, request.plan_id);
    this._requireExecutor();
    assertProductionRuntimePlan(resolved);
    const activeKey = `${resolved.access.ownerId}:${resolved.access.slotId}:${resolved.plan.plan_id}`;
    const active = this.activeExecutions.get(activeKey);
    if (active) return active;
    const operationId = `vsj-${this.randomBytes(12).toString("hex")}`;
    if (!/^vsj-[a-f0-9]{24}$/.test(operationId)) {
      fail("SCENE_BUILD_RANDOM_INVALID", "Scene build random source is invalid", { status: 500 });
    }
    await this._writePending(resolved, operationId);
    const execution = {
      response: {
        schema: EXECUTION_RESPONSE_SCHEMA,
        import_artifact_id: resolved.importArtifactId,
        profile_id: resolved.profileId,
        plan_id: resolved.plan.plan_id,
        operation_id: operationId,
        status: "pending",
        result: null,
      },
      promise: null,
    };
    execution.promise = this._runExecution(resolved, operationId, context)
      .finally(() => this.activeExecutions.delete(activeKey));
    this.activeExecutions.set(activeKey, execution);
    return execution;
  }

  async _runExecution(resolved, operationId, context) {
    let result;
    let runtimeProof;
    try {
      result = validateExecutorResult(
        await this.executor.execute(resolved.plan, {
          signal: context.signal,
          ownerId: resolved.access.ownerId,
          sessionId: resolved.access.sessionId,
          slotId: resolved.access.slotId,
          leaseId: resolved.access.leaseId,
          mcpPort: resolved.access.mcpPort,
        }),
        resolved.plan,
      );
      runtimeProof = runtimeProofFromBuild(resolved, result);
    } catch (rawError) {
      this.liveRuntimeProofs.delete(liveProofKey(resolved.access));
      const error = safeExecutionError(rawError);
      let failureRecord = null;
      try {
        const failureResult = error.result
          ? validateExecutorResult(error.result, resolved.plan)
          : conservativeFailureResult(resolved.plan, error);
        failureRecord = await this._writeRecord(resolved, failureResult, operationId);
      } catch (_recordError) {}
      if (failureRecord) await this._ensureTerminalJournal(resolved, failureRecord);
      throw error;
    }

    const record = await this._writeRecord(resolved, result, operationId);
    // Durability is a separate terminal gate. A journal failure must not be
    // reclassified as a UE execution failure or publish a live runtime proof.
    await this._ensureTerminalJournal(resolved, record);
    this.liveRuntimeProofs.set(liveProofKey(resolved.access), runtimeProof);
    while (this.liveRuntimeProofs.size > 128) {
      this.liveRuntimeProofs.delete(this.liveRuntimeProofs.keys().next().value);
    }
    return {
      schema: EXECUTION_RESPONSE_SCHEMA,
      import_artifact_id: resolved.importArtifactId,
      profile_id: resolved.profileId,
      plan_id: resolved.plan.plan_id,
      operation_id: operationId,
      status: record.status,
      result: record.result,
    };
  }

  async _ensureTerminalJournal(resolved, record) {
    if (!this.artifactRecorder || !record || !TERMINAL_BUILD_STATES.has(record.status)) return;
    await this.artifactRecorder.ensureSceneBuildTerminal({
      record,
      importArtifact: resolved.artifact,
    });
  }

  _planResponse(resolved, record) {
    return {
      schema: PLAN_RESPONSE_SCHEMA,
      import_artifact_id: resolved.importArtifactId,
      profile_id: resolved.profileId,
      state: record ? record.status : "planned",
      operation_id: record ? record.operation.operation_id : null,
      artifact_lineage: record && TERMINAL_BUILD_STATES.has(record.status) && this.artifactRecorder
        ? this.artifactRecorder.sceneBuildLineage({ record }) : null,
      plan: resolved.plan,
      last_result: record ? record.result : null,
      updated_at: record ? record.updated_at : null,
    };
  }

  _assertPlanId(plan, requestedPlanId) {
    if (normalizePlanId(requestedPlanId) !== plan.plan_id) {
      fail("SCENE_BUILD_PLAN_STALE", "The confirmed BuildPlan is no longer current", { status: 409 });
    }
  }

  _requireExecutor() {
    if (!this.executor) {
      fail("SCENE_BUILD_RUNTIME_UNAVAILABLE", "UE scene build runtime is not configured", {
        status: 503,
        retryable: true,
      });
    }
  }

  async _resolvePlan(importArtifactId, rawProfileId, context) {
    const access = normalizeAccess(context);
    const artifact = await this.importService.status(importArtifactId, {
      ownerId: access.ownerId,
      sessionId: access.sessionId,
    });
    if (!isPlainObject(artifact) || artifact.status !== "committed" || !isPlainObject(artifact.scene_spec)) {
      fail("SCENE_BUILD_IMPORT_INVALID", "Committed VISTA import artifact is invalid", { status: 422 });
    }
    const visualId = artifact.scene_spec.source && artifact.scene_spec.source.visual_id;
    const requestedProfileId = normalizeProfileId(rawProfileId, { optional: true });
    let profileId = requestedProfileId;
    if (!profileId) {
      const matches = [...this.layoutProfiles.entries()]
        .filter(([, profile]) => profile.source_visual_id === visualId)
        .map(([id]) => id)
        .sort();
      if (matches.length === 0) fail("SCENE_BUILD_PROFILE_UNAVAILABLE", "No verified layout profile is configured for this VISTA sample", { status: 503 });
      if (matches.length > 1) fail("SCENE_BUILD_PROFILE_REQUIRED", "More than one verified layout profile is available", { status: 409 });
      [profileId] = matches;
    }
    const profile = this.layoutProfiles.get(profileId);
    if (!profile || profile.source_visual_id !== visualId) {
      fail("SCENE_BUILD_PROFILE_UNAVAILABLE", "The selected layout profile is not available for this VISTA sample", { status: 404 });
    }
    const plan = compileVistaSceneBuildPlan(artifact.scene_spec, profile);
    return { access, artifact, importArtifactId: artifact.artifact_id || importArtifactId, profileId, plan };
  }

  async _readRecord(planId, access, { allowMissing }) {
    const target = recordPath(this.recordRoot, planId, access.ownerId);
    let handle;
    try {
      handle = await fs.promises.open(target, fs.constants.O_RDONLY | Number(fs.constants.O_NOFOLLOW || 0));
      const stat = await handle.stat();
      if (!stat.isFile() || stat.size > this.maxRecordBytes) throw new Error("invalid record");
      const record = JSON.parse(await handle.readFile("utf8"));
      if (!isPlainObject(record) || record.schema !== BUILD_RECORD_SCHEMA || record.plan_id !== planId
          || !isPlainObject(record.access)
          || !new Set(["pending", "succeeded", "already_applied", "failed"]).has(record.status)
          || (record.status === "pending" ? record.result !== null : !isPlainObject(record.result))) {
        throw new Error("invalid record");
      }
      if (record.access.owner_id !== access.ownerId) {
        fail("SCENE_BUILD_ACCESS_DENIED", "Scene build record is not available to this owner", { status: 403 });
      }
      return record;
    } catch (error) {
      if (error && error.code === "ENOENT" && allowMissing) return null;
      if (error instanceof VistaSceneBuildServiceError) throw error;
      fail("SCENE_BUILD_STORAGE_UNAVAILABLE", "Scene build record storage is unavailable", { status: 503, retryable: true });
    } finally {
      if (handle) await handle.close().catch(() => {});
    }
  }

  async _writePending(resolved, operationId) {
    return this._persistRecord(resolved, "pending", null, operationId);
  }

  async _writeRecord(resolved, result, operationId) {
    return this._persistRecord(resolved, result.status, result, operationId);
  }

  async _persistRecord(resolved, status, result, operationId) {
    const timestamp = nowIso(this.clock);
    const existing = await this._readRecord(resolved.plan.plan_id, resolved.access, { allowMissing: true });
    const record = {
      schema: BUILD_RECORD_SCHEMA,
      plan_id: resolved.plan.plan_id,
      import_artifact_id: resolved.importArtifactId,
      profile_id: resolved.profileId,
      status,
      access: {
        owner_id: resolved.access.ownerId,
        last_session_id: resolved.access.sessionId,
        slot_id: resolved.access.slotId,
        lease_id_sha256: crypto.createHash("sha256").update(resolved.access.leaseId, "utf8").digest("hex"),
      },
      operation: {
        operation_id: operationId,
        state: status === "pending" ? "running" : "terminal",
      },
      created_at: existing ? existing.created_at : timestamp,
      updated_at: timestamp,
      result,
    };
    const serialized = `${JSON.stringify(record, null, 2)}\n`;
    if (Buffer.byteLength(serialized, "utf8") > this.maxRecordBytes) {
      fail("SCENE_BUILD_RECORD_TOO_LARGE", "Scene build record exceeds its size limit", { status: 500 });
    }
    await fs.promises.mkdir(this.recordRoot, { recursive: true, mode: 0o700 });
    const suffix = this.randomBytes(12).toString("hex");
    if (!/^[a-f0-9]{24}$/.test(suffix)) fail("SCENE_BUILD_RANDOM_INVALID", "Scene build random source is invalid", { status: 500 });
    const target = recordPath(this.recordRoot, resolved.plan.plan_id, resolved.access.ownerId);
    const temporary = path.resolve(this.recordRoot, `.tmp-${path.basename(target)}-${suffix}`);
    let handle;
    try {
      handle = await fs.promises.open(temporary, "wx", 0o600);
      await handle.writeFile(serialized, "utf8");
      await handle.sync();
      await handle.close();
      handle = null;
      await fs.promises.rename(temporary, target);
      return record;
    } catch (error) {
      if (error instanceof VistaSceneBuildServiceError) throw error;
      fail("SCENE_BUILD_STORAGE_UNAVAILABLE", "Scene build record storage is unavailable", { status: 503, retryable: true });
    } finally {
      if (handle) await handle.close().catch(() => {});
      await fs.promises.unlink(temporary).catch(() => {});
    }
  }
}

function createVistaSceneBuildService(options) {
  return new VistaSceneBuildService(options);
}

module.exports = {
  BUILD_RECORD_SCHEMA,
  EXECUTION_RESPONSE_SCHEMA,
  PLAN_RESPONSE_SCHEMA,
  PREFLIGHT_RESPONSE_SCHEMA,
  VistaSceneBuildService,
  VistaSceneBuildServiceError,
  createVistaSceneBuildService,
};
