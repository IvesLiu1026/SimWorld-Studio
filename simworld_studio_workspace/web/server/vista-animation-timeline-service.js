"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const { digest, validateContentProfile } = require("./vista-animation-contract");
const { validateAnimationEvidenceManifest } = require("./vista-animation-runtime");
const { validateVistaSceneBuildPlan } = require("./vista-scene-build-plan");
const {
  compileVistaTimeline,
  validateBindings,
} = require("./vista-timeline-compiler");
const {
  createVistaTimelineScheduler,
  validateTimelineRunArtifact,
} = require("./vista-timeline-scheduler");
const { normalizeSceneProof } = require("./vista-runtime-broker");

const ANIMATION_PREFLIGHT_SERVICE_SCHEMA = "vista-animation-timeline-preflight-service/v1";
const ANIMATION_START_SERVICE_SCHEMA = "vista-animation-timeline-start/v1";
const ANIMATION_STATUS_SERVICE_SCHEMA = "vista-animation-timeline-status/v1";
const ANIMATION_RUN_RECORD_SCHEMA = "vista-animation-timeline-run-record/v1";
const ANIMATION_PUBLIC_RUN_SCHEMA = "vista-animation-timeline-run-status/v1";
const ANIMATION_SLOT_BINDING_SCHEMA = "vista-animation-ue-slot-binding/v1";
const RECORD_REVISION = 1;
const IMPORT_ID_RE = /^vim_[a-f0-9]{64}$/;
const PLAN_ID_RE = /^vsp-[a-f0-9]{24}$/;
const PREFLIGHT_ID_RE = /^vap-[a-f0-9]{24}$/;
const TIMELINE_ID_RE = /^vtl-[a-f0-9]{24}$/;
const PROGRAM_ID_RE = /^vag-[a-f0-9]{24}$/;
const RUN_ID_RE = /^vtr-[a-z0-9_-]{8,120}$/;
const PRINCIPAL_RE = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$/;
const PROFILE_ID_RE = /^[a-z0-9][a-z0-9._-]{0,79}$/;
const SHA256_RE = /^[a-f0-9]{64}$/;
const SAFE_CODE_RE = /^(?:ANIMATION|TIMELINE|SCENE_BUILD|VISTA)_[A-Z0-9_]{2,120}$/;
const TERMINAL_STATES = new Set(["completed", "failed", "cancelled"]);
const RECORD_STATES = new Set(["pending", "running", "stopping", ...TERMINAL_STATES]);
const DEFAULT_MAX_RECORD_BYTES = 32 * 1024 * 1024;
const DEFAULT_PREFLIGHT_TTL_MS = 60_000;
const DEFAULT_READINESS_MAX_AGE_MS = 30_000;
const DEFAULT_MAX_PREPARED = 128;

class VistaAnimationTimelineServiceError extends Error {
  constructor(code, message, options = {}) {
    super(message);
    this.name = "VistaAnimationTimelineServiceError";
    this.code = code;
    this.status = Number.isInteger(options.status) ? options.status : 400;
    this.retryable = options.retryable === true;
    if (typeof options.causeCode === "string" && SAFE_CODE_RE.test(options.causeCode)) {
      this.causeCode = options.causeCode;
    }
  }
}

function fail(code, message, options) {
  throw new VistaAnimationTimelineServiceError(code, message, options);
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function exactKeys(value, allowed, required, pointer = "request") {
  if (!isPlainObject(value)) fail("ANIMATION_TIMELINE_REQUEST_INVALID", `${pointer} must be an object`);
  const allow = new Set(allowed);
  const unknown = Object.keys(value).filter((key) => !allow.has(key));
  const missing = required.filter((key) => !Object.prototype.hasOwnProperty.call(value, key));
  if (unknown.length || missing.length) {
    fail("ANIMATION_TIMELINE_REQUEST_INVALID", `${pointer} has an invalid shape`);
  }
  return value;
}

function normalizePattern(value, pattern, code, message) {
  const normalized = typeof value === "string" ? value.trim() : "";
  if (!pattern.test(normalized)) fail(code, message);
  return normalized;
}

function normalizeImportId(value) {
  return normalizePattern(value, IMPORT_ID_RE, "ANIMATION_IMPORT_ID_INVALID", "VISTA import artifact id is invalid");
}

function normalizePlanId(value) {
  return normalizePattern(value, PLAN_ID_RE, "ANIMATION_PLAN_ID_INVALID", "Scene BuildPlan id is invalid");
}

function normalizePreflightId(value) {
  return normalizePattern(value, PREFLIGHT_ID_RE, "ANIMATION_PREFLIGHT_ID_INVALID", "Animation preflight id is invalid");
}

function normalizeTimelineId(value) {
  return normalizePattern(value, TIMELINE_ID_RE, "ANIMATION_TIMELINE_ID_INVALID", "Animation timeline id is invalid");
}

function normalizeProgramId(value) {
  return normalizePattern(value, PROGRAM_ID_RE, "ANIMATION_PROGRAM_ID_INVALID", "Animation program id is invalid");
}

function normalizeRunId(value) {
  return normalizePattern(value, RUN_ID_RE, "ANIMATION_RUN_ID_INVALID", "Animation run id is invalid");
}

function normalizeProfileId(value, { optional = false } = {}) {
  if (optional && (value === undefined || value === null || value === "")) return null;
  return normalizePattern(value, PROFILE_ID_RE, "ANIMATION_PROFILE_ID_INVALID", "Scene layout profile id is invalid");
}

function normalizeIdentity(context) {
  if (!isPlainObject(context)) fail("ANIMATION_ACCESS_INVALID", "Animation access context is required");
  const ownerId = typeof context.ownerId === "string" ? context.ownerId.trim() : "";
  const sessionId = typeof context.sessionId === "string" ? context.sessionId.trim() : "";
  const leaseId = typeof context.leaseId === "string" ? context.leaseId.trim() : "";
  const slotId = Number(context.slotId);
  const mcpPort = Number(context.mcpPort);
  if (!PRINCIPAL_RE.test(ownerId) || !PRINCIPAL_RE.test(sessionId) || !PRINCIPAL_RE.test(leaseId)
      || !Number.isSafeInteger(slotId) || slotId < 0 || slotId > 1023
      || !Number.isSafeInteger(mcpPort) || mcpPort < 1 || mcpPort > 65535) {
    fail("ANIMATION_ACCESS_INVALID", "Animation access context is invalid");
  }
  return { ownerId, sessionId, leaseId, slotId, mcpPort };
}

function sameIdentity(left, right) {
  return left.ownerId === right.ownerId
    && left.sessionId === right.sessionId
    && left.leaseId === right.leaseId
    && left.slotId === right.slotId
    && left.mcpPort === right.mcpPort;
}

function leaseDigest(leaseId) {
  return crypto.createHash("sha256").update(leaseId, "utf8").digest("hex");
}

function ownerDigest(ownerId) {
  return crypto.createHash("sha256").update(ownerId, "utf8").digest("hex").slice(0, 24);
}

function nowMilliseconds(clock) {
  let value;
  try {
    value = clock();
  } catch {
    fail("ANIMATION_CLOCK_INVALID", "Animation service clock failed", { status: 500 });
  }
  const date = value instanceof Date ? value : new Date(value);
  if (!Number.isFinite(date.getTime())) fail("ANIMATION_CLOCK_INVALID", "Animation service clock is invalid", { status: 500 });
  return date.getTime();
}

function nowIso(clock) {
  return new Date(nowMilliseconds(clock)).toISOString();
}

function safeDependencyError(error, fallbackCode, fallbackMessage, fallbackStatus = 503) {
  if (error instanceof VistaAnimationTimelineServiceError) return error;
  const code = error && typeof error.code === "string" && SAFE_CODE_RE.test(error.code)
    ? error.code
    : fallbackCode;
  const status = [error && error.status, error && error.statusCode]
    .find((value) => Number.isInteger(value) && value >= 400 && value <= 599) || fallbackStatus;
  return new VistaAnimationTimelineServiceError(code, String(error && error.message || fallbackMessage).slice(0, 500), {
    status,
    retryable: Boolean(error && error.retryable),
  });
}

function isArtifactJournalFailure(error) {
  return Boolean(error && (error.name === "ArtifactJournalRuntimeError"
    || /^ARTIFACT_JOURNAL_/.test(String(error.code || ""))));
}

function cloneJson(value) {
  return JSON.parse(JSON.stringify(value));
}

function deepFreeze(value) {
  if (!value || typeof value !== "object" || Object.isFrozen(value)) return value;
  Object.freeze(value);
  for (const child of Object.values(value)) deepFreeze(child);
  return value;
}

function validateRuntime(runtime) {
  if (!runtime || typeof runtime !== "object") fail("ANIMATION_RUNTIME_UNAVAILABLE", "Animation runtime is not configured", { status: 503, retryable: true });
  for (const method of ["preflight", "compileProgram", "finalizeEvidence", "discardEvidence"]) {
    if (typeof runtime[method] !== "function") fail("ANIMATION_RUNTIME_UNAVAILABLE", "Animation runtime is incomplete", { status: 503, retryable: true });
  }
  try {
    validateContentProfile(runtime.contentProfile);
  } catch (error) {
    throw safeDependencyError(error, "ANIMATION_RUNTIME_UNAVAILABLE", "Animation content profile is invalid", 503);
  }
  return runtime;
}

function validateRuntimeBundle(value) {
  if (!isPlainObject(value)
      || typeof value.probeReadiness !== "function"
      || typeof value.engineTimeSampler !== "function") {
    fail("ANIMATION_RUNTIME_UNAVAILABLE", "Trusted animation runtime provider is unavailable", { status: 503, retryable: true });
  }
  return {
    runtime: validateRuntime(value.runtime),
    probeReadiness: value.probeReadiness,
    engineTimeSampler: value.engineTimeSampler,
  };
}

function expectedSlotBindingDigest(identity, sceneRevision) {
  return digest({
    schema: ANIMATION_SLOT_BINDING_SCHEMA,
    owner_id: identity.ownerId,
    session_id: identity.sessionId,
    slot_id: String(identity.slotId),
    scene_revision: sceneRevision,
  });
}

function normalizeReadiness(result, runtime, identity, sceneRevision, clockMs, maxAgeMs) {
  if (!isPlainObject(result) || result.status !== "ready" || !Array.isArray(result.causes)
      || result.causes.length !== 0 || !isPlainObject(result.revision)) {
    const cause = result && Array.isArray(result.causes) && result.causes[0];
    fail("ANIMATION_RUNTIME_NOT_READY", "Trusted animation runtime did not pass its live readiness probe", {
      status: 503,
      retryable: Boolean(cause && cause.retryable),
      causeCode: cause && cause.code,
    });
  }
  const revision = cloneJson(result.revision);
  const profile = runtime.contentProfile;
  const required = [
    "profile_id", "profile_revision", "content_revision", "content_digest",
    "slot_binding_digest", "process_instance_id", "operation_allowlist_digest",
    "checked_at", "verification",
  ];
  if (required.some((field) => typeof revision[field] !== "string" || revision[field].length === 0)
      || revision.profile_id !== profile.profile_id
      || revision.profile_revision !== profile.revision
      || revision.content_revision !== profile.content_revision
      || revision.content_digest !== profile.content_digest
      || !SHA256_RE.test(revision.content_digest)
      || !SHA256_RE.test(revision.slot_binding_digest)
      || !SHA256_RE.test(revision.operation_allowlist_digest)
      || revision.slot_binding_digest !== expectedSlotBindingDigest(identity, sceneRevision)
      || revision.verification !== "live_plugin_challenge") {
    fail("ANIMATION_RUNTIME_REVISION_MISMATCH", "Animation runtime readiness proof does not match this session, scene, or content revision", { status: 409 });
  }
  const checkedAt = Date.parse(revision.checked_at);
  if (!Number.isFinite(checkedAt) || checkedAt > clockMs + 5_000 || clockMs - checkedAt > maxAgeMs) {
    fail("ANIMATION_RUNTIME_READINESS_STALE", "Animation runtime readiness proof is stale", { status: 409, retryable: true });
  }
  const stable = { ...revision };
  delete stable.checked_at;
  return deepFreeze({
    revision: deepFreeze(revision),
    stableDigest: digest(stable),
  });
}

function validateSuccessfulSceneBuild(status, scene) {
  if (!isPlainObject(status)
      || status.schema !== "vista-scene-build-plan-response/v1"
      || !new Set(["succeeded", "already_applied"]).has(status.state)
      || !isPlainObject(status.plan)
      || !isPlainObject(status.last_result)) {
    fail("ANIMATION_SCENE_BUILD_REQUIRED", "A successful exact VISTA scene build is required before animation", { status: 409 });
  }
  const buildOperationId = typeof status.operation_id === "string" ? status.operation_id.trim() : "";
  if (!PRINCIPAL_RE.test(buildOperationId)) {
    fail("ANIMATION_SCENE_BUILD_INVALID", "Scene build response operation identity is missing", { status: 500 });
  }
  const buildLineage = status.artifact_lineage;
  if (!isPlainObject(buildLineage) || buildLineage.kind !== "vista-scene-build"
      || buildLineage.revision !== buildOperationId
      || !SHA256_RE.test(String(buildLineage.content_digest || ""))) {
    fail("ANIMATION_SCENE_BUILD_INVALID", "Scene build artifact lineage is missing or invalid", { status: 500 });
  }
  let plan;
  try {
    plan = validateVistaSceneBuildPlan(status.plan);
  } catch (error) {
    throw safeDependencyError(error, "ANIMATION_SCENE_BUILD_INVALID", "Stored scene BuildPlan is invalid", 500);
  }
  const result = status.last_result;
  if (status.plan_id !== undefined && status.plan_id !== plan.plan_id) {
    fail("ANIMATION_SCENE_BUILD_INVALID", "Scene build response plan identity is inconsistent", { status: 500 });
  }
  if (plan.scene_id !== scene.scene_id
      || plan.scene_revision !== scene.source.source_checksum
      || result.schema !== "vista-scene-build-result/v1"
      || result.plan_id !== plan.plan_id
      || result.scene_id !== plan.scene_id
      || !new Set(["succeeded", "already_applied"]).has(result.status)
      || !Array.isArray(result.actor_manifest)
      || result.actor_manifest.length !== plan.actors.length) {
    fail("ANIMATION_SCENE_BUILD_INVALID", "Scene build result does not prove the exact committed scene", { status: 409 });
  }
  const manifest = new Map(result.actor_manifest.map((entry) => [entry && entry.actor_id, entry]));
  for (const actor of plan.actors) {
    const actual = manifest.get(actor.actor_id);
    if (!isPlainObject(actual)
        || actual.actor_name !== actor.actor_name
        || actual.runtime_actor_name !== actor.actor_name
        || actual.fingerprint !== actor.fingerprint
        || actual.source_entity_id !== actor.source_entity_id
        || !new Set(["spawned", "reused"]).has(actual.disposition)
        || typeof actual.object_guid !== "string" || !PRINCIPAL_RE.test(actual.object_guid)
        || !isPlainObject(actual.asset)
        || actual.asset.asset_id !== actor.asset.asset_id
        || actual.asset.ue_path !== actor.asset.ue_path
        || actual.asset.content_revision !== actor.asset.content_revision
        || actual.asset.verification_revision !== actor.asset.verification_revision) {
      fail("ANIMATION_SCENE_BUILD_INVALID", "Scene build actor manifest is incomplete or stale", { status: 409 });
    }
  }
  if (!isPlainObject(result.player_start)
      || !new Set(["updated", "reused"]).has(result.player_start.disposition)
      || !isPlainObject(result.player_start.applied)
      || result.player_start.applied.transform !== true) {
    fail("ANIMATION_SCENE_BUILD_INVALID", "Scene build did not prove the required PlayerStart transform", { status: 409 });
  }
  if (!Array.isArray(result.evidence) || !isPlainObject(result.rollback)
      || result.rollback.state !== "not_required") {
    fail("ANIMATION_SCENE_BUILD_INVALID", "Scene build did not retain successful verification evidence", { status: 409 });
  }
  const buildEvidence = new Map(result.evidence.map((entry) => [entry && entry.evidence_id, entry]));
  for (const request of plan.evidence_requests.filter((entry) => entry.required)) {
    const actual = buildEvidence.get(request.evidence_id);
    if (!isPlainObject(actual)
        || actual.kind !== request.kind
        || actual.required !== true
        || actual.status !== "captured"
        || actual.artifact === null || actual.artifact === undefined
        || actual.error !== null) {
      fail("ANIMATION_SCENE_BUILD_INVALID", "Scene build required evidence is missing or stale", { status: 409 });
    }
  }
  if (buildLineage.artifact_id !== plan.plan_id) {
    fail("ANIMATION_SCENE_BUILD_INVALID", "Scene build artifact lineage does not match the BuildPlan", { status: 500 });
  }
  return { plan, result, buildOperationId, buildContentDigest: buildLineage.content_digest };
}

function recordPath(root, runId, ownerId) {
  const checked = normalizeRunId(runId);
  const target = path.resolve(root, `${checked}-${ownerDigest(ownerId)}.json`);
  if (path.dirname(target) !== root) fail("ANIMATION_STORAGE_UNAVAILABLE", "Animation record path is invalid", { status: 500 });
  return target;
}

async function ensureRecordRoot(root, { create }) {
  try {
    if (create) await fs.promises.mkdir(root, { recursive: true, mode: 0o700 });
    const stat = await fs.promises.lstat(root);
    if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error("record root is not a private directory");
    if (create && (stat.mode & 0o077) !== 0) await fs.promises.chmod(root, 0o700);
    return true;
  } catch (error) {
    if (!create && error && error.code === "ENOENT") return false;
    fail("ANIMATION_STORAGE_UNAVAILABLE", "Animation record storage is unavailable", { status: 503, retryable: true });
  }
}

function validateStoredRecord(record, expectedRunId, expectedOwnerId) {
  if (!isPlainObject(record)
      || record.schema !== ANIMATION_RUN_RECORD_SCHEMA
      || record.revision !== RECORD_REVISION
      || record.run_id !== expectedRunId
      || record.operation_id !== expectedRunId
      || !RECORD_STATES.has(record.status)
      || !isPlainObject(record.access)
      || record.access.owner_id !== expectedOwnerId
      || !isPlainObject(record.identity)
      || !isPlainObject(record.runtime)
      || !isPlainObject(record.operation)
      || !new Set(["running", "terminal"]).has(record.operation.state)
      || (TERMINAL_STATES.has(record.status) !== (record.operation.state === "terminal"))
      || (record.run !== null && !isPlainObject(record.run))
      || (record.evidence !== null && !isPlainObject(record.evidence))
      || (record.error !== null && !isPlainObject(record.error))) {
    fail("ANIMATION_RECORD_CORRUPT", "Stored animation run record failed validation", { status: 500 });
  }
  const identity = record.identity;
  const access = record.access;
  const runtime = record.runtime;
  if (!IMPORT_ID_RE.test(identity.import_artifact_id)
      || !PROFILE_ID_RE.test(identity.profile_id)
      || !PLAN_ID_RE.test(identity.plan_id)
      || typeof identity.scene_id !== "string" || identity.scene_id.length > 160
      || !PREFLIGHT_ID_RE.test(identity.preflight_id)
      || !TIMELINE_ID_RE.test(identity.timeline_id)
      || !PROGRAM_ID_RE.test(identity.program_id)
      || !PRINCIPAL_RE.test(identity.scene_build_operation_id)
      || !SHA256_RE.test(identity.scene_build_content_digest)
      || !PRINCIPAL_RE.test(access.last_session_id)
      || !Number.isSafeInteger(access.slot_id) || access.slot_id < 0 || access.slot_id > 1023
      || !SHA256_RE.test(access.lease_id_sha256)
      || !Number.isSafeInteger(access.mcp_port) || access.mcp_port < 1 || access.mcp_port > 65535
      || !PROFILE_ID_RE.test(runtime.profile_id)
      || !PRINCIPAL_RE.test(runtime.profile_revision)
      || !PRINCIPAL_RE.test(runtime.content_revision)
      || !SHA256_RE.test(runtime.content_digest)
      || !SHA256_RE.test(runtime.runtime_revision_digest)
      || typeof record.created_at !== "string" || !Number.isFinite(Date.parse(record.created_at))
      || typeof record.updated_at !== "string" || !Number.isFinite(Date.parse(record.updated_at))
      || (record.operation.replay_of !== null && !RUN_ID_RE.test(record.operation.replay_of))) {
    fail("ANIMATION_RECORD_CORRUPT", "Stored animation run record identity failed validation", { status: 500 });
  }
  if (record.error !== null
      && (!SAFE_CODE_RE.test(record.error.code) || typeof record.error.retryable !== "boolean")) {
    fail("ANIMATION_RECORD_CORRUPT", "Stored animation run error failed validation", { status: 500 });
  }
  if (record.run !== null) {
    try {
      validateTimelineRunArtifact(record.run);
    } catch {
      fail("ANIMATION_RECORD_CORRUPT", "Stored animation timeline run failed validation", { status: 500 });
    }
    if (record.run.run_id !== expectedRunId
        || record.run.owner_id !== expectedOwnerId
        || record.run.timeline_id !== identity.timeline_id
        || record.run.scene_revision !== identity.scene_id) {
      fail("ANIMATION_RECORD_CORRUPT", "Stored animation timeline provenance failed validation", { status: 500 });
    }
  }
  if (record.evidence !== null) {
    try {
      validateAnimationEvidenceManifest(record.evidence);
    } catch {
      fail("ANIMATION_RECORD_CORRUPT", "Stored animation evidence failed validation", { status: 500 });
    }
    if (record.evidence.run_id !== expectedRunId
        || record.evidence.program_id !== identity.program_id
        || record.evidence.timeline_id !== identity.timeline_id
        || record.evidence.scene_revision !== identity.scene_id) {
      fail("ANIMATION_RECORD_CORRUPT", "Stored animation evidence provenance failed validation", { status: 500 });
    }
  }
  return record;
}

function schedulerStateToRecord(state) {
  if (TERMINAL_STATES.has(state)) return state;
  if (new Set(["queued", "preflighting", "ready"]).has(state)) return "pending";
  return state === "stopping" ? "stopping" : "running";
}

function publicRun(run) {
  if (!run) return null;
  return {
    schema: ANIMATION_PUBLIC_RUN_SCHEMA,
    run_id: run.run_id,
    timeline_id: run.timeline_id,
    scene_revision: run.scene_revision,
    duration_sec: run.duration_sec,
    state: run.state,
    created_at: run.created_at,
    started_at: run.started_at,
    ended_at: run.ended_at,
    events: run.events.map((event) => ({
      event_id: event.event_id,
      planned_sec: event.planned_sec,
      actual_sec: event.actual_sec,
      engine_time: event.engine_time,
      drift_ms: event.drift_ms,
      state: event.state,
      attempt: event.attempt,
      started_at: event.started_at,
      ended_at: event.ended_at,
      cleanup_state: event.cleanup_state,
    })),
    cleanup: {
      state: run.cleanup.state,
      pending_items: [...run.cleanup.pending_items],
      confirmed_stopped: run.cleanup.confirmed_stopped,
      ended_pie: run.cleanup.ended_pie,
    },
  };
}

class VistaAnimationTimelineService {
  constructor(options = {}) {
    if (!options.importService || typeof options.importService.status !== "function") {
      throw new TypeError("VistaAnimationTimelineService requires importService.status");
    }
    if (!options.sceneBuildService || typeof options.sceneBuildService.status !== "function") {
      throw new TypeError("VistaAnimationTimelineService requires sceneBuildService.status");
    }
    if (typeof options.runtimeProvider !== "function") {
      throw new TypeError("VistaAnimationTimelineService requires runtimeProvider");
    }
    if (typeof options.bindingResolver !== "function") {
      throw new TypeError("VistaAnimationTimelineService requires bindingResolver");
    }
    if (typeof options.recordRoot !== "string" || !options.recordRoot.trim()) {
      throw new TypeError("VistaAnimationTimelineService requires recordRoot");
    }
    const recordRoot = path.resolve(options.recordRoot);
    if (recordRoot === path.parse(recordRoot).root) throw new TypeError("recordRoot cannot be a filesystem root");
    this.importService = options.importService;
    this.sceneBuildService = options.sceneBuildService;
    this.runtimeProvider = options.runtimeProvider;
    this.bindingResolver = options.bindingResolver;
    this.runtimeLifecycle = options.runtimeLifecycle || null;
    if (options.artifactRecorder !== undefined && options.artifactRecorder !== null
        && typeof options.artifactRecorder.ensureTimelineTerminal !== "function") {
      throw new TypeError("artifactRecorder must expose ensureTimelineTerminal");
    }
    this.artifactRecorder = options.artifactRecorder || null;
    const lifecycleAllowedKeys = new Set([
      "controllerFor", "exactIdentityFromRequest", "sceneProofFor",
      "startForIdentity", "stateForIdentity", "stopForIdentity",
    ]);
    if (this.runtimeLifecycle !== null && (!isPlainObject(this.runtimeLifecycle)
      || Object.keys(this.runtimeLifecycle).some((key) => !lifecycleAllowedKeys.has(key))
      || typeof this.runtimeLifecycle.startForIdentity !== "function"
      || typeof this.runtimeLifecycle.stateForIdentity !== "function"
      || typeof this.runtimeLifecycle.stopForIdentity !== "function")) {
      throw new TypeError("runtimeLifecycle must expose only exact start/state/stop identity methods");
    }
    this.recordRoot = recordRoot;
    this.clock = typeof options.clock === "function" ? options.clock : () => new Date();
    this.randomBytes = typeof options.randomBytes === "function" ? options.randomBytes : crypto.randomBytes;
    this.createScheduler = typeof options.createScheduler === "function"
      ? options.createScheduler
      : createVistaTimelineScheduler;
    this.schedulerOptions = isPlainObject(options.schedulerOptions) ? { ...options.schedulerOptions } : {};
    for (const forbidden of ["capabilityRegistry", "engineTimeSampler", "idFactory", "wallClock"]) {
      if (Object.prototype.hasOwnProperty.call(this.schedulerOptions, forbidden)) {
        throw new TypeError(`schedulerOptions.${forbidden} is service-owned`);
      }
    }
    this.preflightTtlMs = Number.isInteger(options.preflightTtlMs)
      ? options.preflightTtlMs : DEFAULT_PREFLIGHT_TTL_MS;
    this.readinessMaxAgeMs = Number.isInteger(options.readinessMaxAgeMs)
      ? options.readinessMaxAgeMs : DEFAULT_READINESS_MAX_AGE_MS;
    this.maxPrepared = Number.isInteger(options.maxPrepared) ? options.maxPrepared : DEFAULT_MAX_PREPARED;
    this.maxRecordBytes = Number.isInteger(options.maxRecordBytes)
      ? options.maxRecordBytes : DEFAULT_MAX_RECORD_BYTES;
    if (this.preflightTtlMs < 1_000 || this.preflightTtlMs > 600_000
        || this.readinessMaxAgeMs < 1_000 || this.readinessMaxAgeMs > 300_000
        || this.maxPrepared < 1 || this.maxPrepared > 10_000
        || this.maxRecordBytes < 1024 || this.maxRecordBytes > 128 * 1024 * 1024) {
      throw new TypeError("Animation service bounds are invalid");
    }
    this.prepared = new Map();
    this.activeRuns = new Map();
    this.activeSlots = new Map();
    this.startingSlots = new Set();
  }

  async preflight(importArtifactId, request = {}, context = {}) {
    exactKeys(request, ["plan_id", "profile_id"], ["plan_id"]);
    const identity = normalizeIdentity(context);
    const importId = normalizeImportId(importArtifactId);
    const planId = normalizePlanId(request.plan_id);
    const profileId = normalizeProfileId(request.profile_id, { optional: true });
    const resolved = await this._resolveScene(importId, planId, profileId, identity);
    const compiled = await this._compile(resolved, identity, context.signal);
    const nowMs = nowMilliseconds(this.clock);
    const prepared = {
      importId,
      profileId: resolved.profileId,
      planId,
      sceneId: resolved.scene.scene_id,
      identity,
      preflightId: compiled.preflight.artifact.preflight_id,
      timelineId: compiled.timeline.timeline_id,
      programId: compiled.program.program_id,
      runtimeRevisionDigest: compiled.readiness.stableDigest,
      sceneRuntimeProofDigest: resolved.buildRuntimeProof
        ? digest(resolved.buildRuntimeProof) : null,
      createdAtMs: nowMs,
      expiresAtMs: nowMs + this.preflightTtlMs,
      startedRunId: null,
    };
    this._prunePrepared(nowMs);
    this.prepared.set(this._preparedKey(identity, prepared.preflightId), prepared);
    return this._preflightResponse(prepared, compiled);
  }

  async start(importArtifactId, request = {}, context = {}) {
    return this._start(importArtifactId, request, context, null);
  }

  async status(importArtifactId, runId, context = {}) {
    const identity = normalizeIdentity(context);
    const importId = normalizeImportId(importArtifactId);
    const checkedRunId = normalizeRunId(runId);
    const key = this._runKey(identity.ownerId, checkedRunId);
    const active = this.activeRuns.get(key);
    if (active) {
      if (active.importId !== importId) fail("ANIMATION_RUN_NOT_FOUND", "Animation run was not found", { status: 404 });
      const run = active.scheduler.getRun(active.schedulerAccess);
      if (TERMINAL_STATES.has(run.state) && active.completion) {
        await active.completion.catch(() => {});
        const finalized = await this._readRecord(checkedRunId, identity.ownerId, { allowMissing: false });
        await this._ensureTerminalJournal(finalized);
        return this._statusResponse(finalized, finalized.run, finalized.evidence);
      }
      return this._statusResponse(active.record, run, null);
    }
    let record = await this._readRecord(checkedRunId, identity.ownerId, { allowMissing: false });
    if (record.identity.import_artifact_id !== importId) fail("ANIMATION_RUN_NOT_FOUND", "Animation run was not found", { status: 404 });
    if (!TERMINAL_STATES.has(record.status)) record = await this._markOrphaned(record);
    await this._ensureTerminalJournal(record);
    return this._statusResponse(record, record.run, record.evidence);
  }

  async stop(importArtifactId, runId, request = {}, context = {}) {
    exactKeys(request, [], []);
    const identity = normalizeIdentity(context);
    const importId = normalizeImportId(importArtifactId);
    const checkedRunId = normalizeRunId(runId);
    const key = this._runKey(identity.ownerId, checkedRunId);
    const active = this.activeRuns.get(key);
    if (!active) return this.status(importId, checkedRunId, identity);
    if (active.importId !== importId) fail("ANIMATION_RUN_NOT_FOUND", "Animation run was not found", { status: 404 });
    const currentRun = active.scheduler.getRun(active.schedulerAccess);
    if (TERMINAL_STATES.has(currentRun.state) && active.completion) {
      await active.completion.catch(() => {});
      return this.status(importId, checkedRunId, identity);
    }
    if (!sameIdentity(active.identity, identity)) {
      fail("ANIMATION_RUN_IDENTITY_STALE", "Only the exact active Studio lease may stop this animation run", { status: 409 });
    }
    const stopPromise = active.scheduler.stop({
      ...active.schedulerAccess,
      reason: "user_stop_requested",
    });
    Promise.resolve(stopPromise).catch(() => {});
    const run = active.scheduler.getRun(active.schedulerAccess);
    if (TERMINAL_STATES.has(run.state) && active.completion) {
      await active.completion.catch(() => {});
      return this.status(importId, checkedRunId, identity);
    }
    return this._statusResponse(active.record, run, null);
  }

  async replay(importArtifactId, runId, request = {}, context = {}) {
    const identity = normalizeIdentity(context);
    const importId = normalizeImportId(importArtifactId);
    const checkedRunId = normalizeRunId(runId);
    let original = await this._readRecord(checkedRunId, identity.ownerId, { allowMissing: false });
    if (original.identity.import_artifact_id !== importId) fail("ANIMATION_RUN_NOT_FOUND", "Animation run was not found", { status: 404 });
    if (!TERMINAL_STATES.has(original.status)) {
      if (this.activeRuns.has(this._runKey(identity.ownerId, checkedRunId))) {
        fail("ANIMATION_REPLAY_NOT_READY", "Only a terminal animation run can be replayed", { status: 409 });
      }
      original = await this._markOrphaned(original);
    }
    await this._ensureTerminalJournal(original);
    exactKeys(request, ["plan_id", "preflight_id", "timeline_id", "program_id", "confirm"], ["plan_id", "preflight_id", "timeline_id", "program_id", "confirm"]);
    if (request.confirm !== true
        || normalizePlanId(request.plan_id) !== original.identity.plan_id
        || normalizePreflightId(request.preflight_id) !== original.identity.preflight_id
        || normalizeTimelineId(request.timeline_id) !== original.identity.timeline_id
        || normalizeProgramId(request.program_id) !== original.identity.program_id) {
      fail("ANIMATION_REPLAY_REVISION_MISMATCH", "Replay confirmation does not match the terminal run revision", { status: 409 });
    }
    const response = await this._start(importId, request, context, checkedRunId);
    if (response.plan_id !== original.identity.plan_id
        || response.scene_id !== original.identity.scene_id
        || response.preflight_id !== original.identity.preflight_id
        || response.timeline_id !== original.identity.timeline_id
        || response.program_id !== original.identity.program_id) {
      fail("ANIMATION_REPLAY_REVISION_MISMATCH", "Replay confirmation does not match the terminal run revision", { status: 409 });
    }
    return response;
  }

  async _start(importArtifactId, request, context, replayOf) {
    exactKeys(request, ["plan_id", "preflight_id", "timeline_id", "program_id", "confirm"], ["plan_id", "preflight_id", "timeline_id", "program_id", "confirm"]);
    if (request.confirm !== true) fail("ANIMATION_CONFIRMATION_REQUIRED", "Exact animation preflight and BuildPlan confirmation is required", { status: 428 });
    const identity = normalizeIdentity(context);
    const importId = normalizeImportId(importArtifactId);
    const confirmation = {
      planId: normalizePlanId(request.plan_id),
      preflightId: normalizePreflightId(request.preflight_id),
      timelineId: normalizeTimelineId(request.timeline_id),
      programId: normalizeProgramId(request.program_id),
    };
    const prepared = this._lookupPrepared(identity, confirmation);
    if (prepared.importId !== importId) fail("ANIMATION_PREFLIGHT_STALE", "Animation preflight belongs to a different import artifact", { status: 409 });
    if (prepared.startedRunId) {
      if (replayOf !== null) fail("ANIMATION_PREFLIGHT_CONSUMED", "Replay requires a fresh exact animation preflight", { status: 409 });
      const active = this.activeRuns.get(this._runKey(identity.ownerId, prepared.startedRunId));
      const record = active
        ? active.record
        : await this._readRecord(prepared.startedRunId, identity.ownerId, { allowMissing: false });
      if (TERMINAL_STATES.has(record.status)) await this._ensureTerminalJournal(record);
      return this._startResponse(record);
    }
    const slotKey = this._slotKey(identity);
    if (this.activeSlots.has(slotKey) || this.startingSlots.has(slotKey)) {
      fail("ANIMATION_SLOT_BUSY", "This Studio slot already has an active animation run", { status: 409 });
    }
    this.startingSlots.add(slotKey);
    try {
      const resolved = await this._resolveScene(importId, confirmation.planId, prepared.profileId, identity);
      if (this.runtimeLifecycle
          && digest(resolved.buildRuntimeProof) !== prepared.sceneRuntimeProofDigest) {
        fail("ANIMATION_PREFLIGHT_STALE", "Verified scene runtime proof changed after preflight", { status: 409 });
      }
      const compiled = await this._compile(resolved, identity, context.signal);
      if (compiled.preflight.artifact.preflight_id !== confirmation.preflightId
          || compiled.timeline.timeline_id !== confirmation.timelineId
          || compiled.program.program_id !== confirmation.programId
          || compiled.readiness.stableDigest !== prepared.runtimeRevisionDigest) {
        fail("ANIMATION_PREFLIGHT_STALE", "Animation runtime, content, binding, or timeline revision changed after preflight", { status: 409 });
      }
      const response = await this._launch({
        importId,
        resolved,
        identity,
        prepared,
        compiled,
        replayOf,
      });
      prepared.startedRunId = response.run_id;
      return response;
    } finally {
      this.startingSlots.delete(slotKey);
    }
  }

  async _resolveScene(importId, planId, profileId, identity) {
    let artifact;
    try {
      artifact = await this.importService.status(importId, {
        ownerId: identity.ownerId,
        sessionId: identity.sessionId,
      });
    } catch (error) {
      throw safeDependencyError(error, "ANIMATION_IMPORT_UNAVAILABLE", "Committed VISTA import artifact is unavailable", 503);
    }
    if (!isPlainObject(artifact) || artifact.status !== "committed" || !isPlainObject(artifact.scene_spec)
        || artifact.artifact_id !== importId || !isPlainObject(artifact.access)
        || artifact.access.owner_id !== identity.ownerId) {
      fail("ANIMATION_IMPORT_INVALID", "Committed owner-bound VISTA import artifact is invalid", { status: 422 });
    }
    const scene = artifact.scene_spec;
    if (typeof scene.scene_id !== "string" || !isPlainObject(scene.source) || !SHA256_RE.test(scene.source.source_checksum)
        || !Array.isArray(scene.timeline) || scene.timeline.length === 0) {
      fail("ANIMATION_IMPORT_INVALID", "Committed VISTA SceneSpec is invalid", { status: 422 });
    }
    let buildStatus;
    try {
      buildStatus = await this.sceneBuildService.status(importId, profileId ? { profile_id: profileId } : {}, {
        ...identity,
      });
    } catch (error) {
      throw safeDependencyError(error, "ANIMATION_SCENE_BUILD_UNAVAILABLE", "Scene build status is unavailable", 503);
    }
    const { plan, result, buildOperationId, buildContentDigest } = validateSuccessfulSceneBuild(buildStatus, scene);
    if (plan.plan_id !== planId) fail("ANIMATION_PLAN_STALE", "Confirmed Scene BuildPlan is no longer current", { status: 409 });
    let buildRuntimeProof = null;
    if (this.runtimeLifecycle) {
      try {
        buildRuntimeProof = normalizeSceneProof(
          this.sceneBuildService.resolveActiveRuntimeProof(identity),
        );
      } catch (error) {
        throw safeDependencyError(
          error,
          "ANIMATION_SCENE_BUILD_RUNTIME_PROOF_INVALID",
          "Live scene runtime proof is unavailable",
          409,
        );
      }
      if (!isPlainObject(buildRuntimeProof) || buildRuntimeProof.schema !== "vista-runtime-scene-proof/v1"
          || buildRuntimeProof.plan_id !== plan.plan_id || buildRuntimeProof.scene_id !== scene.scene_id
          || buildRuntimeProof.start_allowed !== true) {
        fail(
          "ANIMATION_SCENE_BUILD_RUNTIME_PROOF_REQUIRED",
          "Rebuild the verified scene in this active Studio lease before animation",
          { status: 409 },
        );
      }
    }
    return {
      artifact,
      scene,
      plan,
      buildResult: result,
      buildOperationId,
      buildContentDigest,
      buildRuntimeProof,
      profileId: normalizeProfileId(buildStatus.profile_id),
    };
  }

  async _compile(resolved, identity, signal) {
    let bundle;
    try {
      bundle = validateRuntimeBundle(await this.runtimeProvider(Object.freeze({
        ownerId: identity.ownerId,
        sessionId: identity.sessionId,
        slotId: identity.slotId,
        leaseId: identity.leaseId,
        mcpPort: identity.mcpPort,
        sceneRevision: resolved.scene.scene_id,
        planId: resolved.plan.plan_id,
      })));
    } catch (error) {
      throw safeDependencyError(error, "ANIMATION_RUNTIME_UNAVAILABLE", "Trusted animation runtime is unavailable", 503);
    }
    const clockMs = nowMilliseconds(this.clock);
    let readinessResult;
    try {
      readinessResult = await bundle.probeReadiness({ signal });
    } catch (error) {
      throw safeDependencyError(error, "ANIMATION_RUNTIME_NOT_READY", "Animation runtime readiness probe failed", 503);
    }
    const readiness = normalizeReadiness(
      readinessResult,
      bundle.runtime,
      identity,
      resolved.scene.scene_id,
      clockMs,
      this.readinessMaxAgeMs,
    );
    let bindings;
    try {
      bindings = validateBindings(await this.bindingResolver(Object.freeze({
        sceneSpec: deepFreeze(cloneJson(resolved.scene)),
        buildPlan: deepFreeze(cloneJson(resolved.plan)),
        buildResult: deepFreeze(cloneJson(resolved.buildResult)),
        contentProfile: bundle.runtime.contentProfile,
        identity: Object.freeze({ ...identity }),
      })));
    } catch (error) {
      throw safeDependencyError(error, "ANIMATION_BINDINGS_UNAVAILABLE", "Verified animation bindings are unavailable", 503);
    }
    const requestedActions = [...new Set(resolved.scene.timeline.map((event) => event.action))].sort();
    let preflight;
    let timeline;
    let program;
    try {
      preflight = await bundle.runtime.preflight({
        sceneRevision: resolved.scene.scene_id,
        bindings,
        requestedActions,
        signal,
      });
      if (!preflight || !preflight.artifact || preflight.artifact.start_allowed !== true) {
        fail("ANIMATION_PREFLIGHT_BLOCKED", "Animation runtime preflight did not allow every verified event", { status: 409 });
      }
      timeline = compileVistaTimeline(resolved.scene, {
        policy: "strict",
        bindings,
        capabilityRegistry: preflight.capabilityRegistry,
      });
      if (!timeline.start_allowed || timeline.events.some((event) => event.disposition !== "execute")) {
        fail("ANIMATION_PREFLIGHT_BLOCKED", "Strict animation timeline has blocked events", { status: 409 });
      }
      program = bundle.runtime.compileProgram(timeline, preflight.artifact);
    } catch (error) {
      throw safeDependencyError(error, "ANIMATION_PREFLIGHT_FAILED", "Animation preflight failed", 409);
    }
    return { bundle, readiness, bindings, preflight, timeline, program };
  }

  async _launch({ importId, resolved, identity, prepared, compiled, replayOf }) {
    const suffix = this.randomBytes(12).toString("hex");
    if (!/^[a-f0-9]{24}$/.test(suffix)) fail("ANIMATION_RANDOM_INVALID", "Animation run random source is invalid", { status: 500 });
    const runId = `vtr-${suffix}`;
    const key = this._runKey(identity.ownerId, runId);
    if (this.activeRuns.has(key) || await this._readRecord(runId, identity.ownerId, { allowMissing: true })) {
      fail("ANIMATION_RUN_ID_CONFLICT", "Animation run id already exists", { status: 500 });
    }
    const timestamp = nowIso(this.clock);
    const record = {
      schema: ANIMATION_RUN_RECORD_SCHEMA,
      revision: RECORD_REVISION,
      run_id: runId,
      operation_id: runId,
      status: "pending",
      identity: {
        import_artifact_id: importId,
        profile_id: resolved.profileId,
        plan_id: resolved.plan.plan_id,
        scene_id: resolved.scene.scene_id,
        preflight_id: prepared.preflightId,
        timeline_id: prepared.timelineId,
        program_id: prepared.programId,
        scene_build_operation_id: resolved.buildOperationId,
        scene_build_content_digest: resolved.buildContentDigest,
      },
      access: {
        owner_id: identity.ownerId,
        last_session_id: identity.sessionId,
        slot_id: identity.slotId,
        lease_id_sha256: leaseDigest(identity.leaseId),
        mcp_port: identity.mcpPort,
      },
      runtime: {
        profile_id: compiled.bundle.runtime.contentProfile.profile_id,
        profile_revision: compiled.bundle.runtime.contentProfile.revision,
        content_revision: compiled.bundle.runtime.contentProfile.content_revision,
        content_digest: compiled.bundle.runtime.contentProfile.content_digest,
        runtime_revision_digest: compiled.readiness.stableDigest,
      },
      operation: { state: "running", replay_of: replayOf },
      created_at: timestamp,
      updated_at: timestamp,
      run: null,
      evidence: null,
      error: null,
    };
    const persisted = await this._persistRecord(record);
    let scheduler;
    try {
      scheduler = this.createScheduler({
        ...this.schedulerOptions,
        capabilityRegistry: compiled.preflight.capabilityRegistry,
        engineTimeSampler: (eventContext) => compiled.bundle.engineTimeSampler(Object.freeze({
          ...eventContext,
          owner_id: identity.ownerId,
          session_id: identity.sessionId,
          slot_id: String(identity.slotId),
          lease_id: identity.leaseId,
          mcp_port: identity.mcpPort,
          scene_revision: resolved.scene.scene_id,
        })),
        idFactory: () => suffix,
        wallClock: () => nowIso(this.clock),
        ...(this.runtimeLifecycle ? {
          lifecycle: {
            start: async ({ signal }) => {
              const receipt = await this.runtimeLifecycle.startForIdentity(identity, {
                sceneProof: resolved.buildRuntimeProof,
                signal,
              });
              if (!isPlainObject(receipt) || receipt.schema !== "vista-runtime-setup/v2"
                  || receipt.phase !== "live" || receipt.pie !== true || receipt.possessed !== true
                  || receipt.scene_proof_digest !== resolved.buildRuntimeProof.actor_manifest_digest) {
                fail("ANIMATION_RUNTIME_LIFECYCLE_INVALID", "Backend PIE Start was not confirmed for the exact scene", {
                  status: 502,
                });
              }
              const state = await this.runtimeLifecycle.stateForIdentity(identity, {
                sceneProof: resolved.buildRuntimeProof,
                signal,
                force: true,
              });
              if (!isPlainObject(state) || state.schema !== "vista-runtime-state/v2"
                  || state.pie !== true || state.possessed !== true) {
                fail("ANIMATION_RUNTIME_LIFECYCLE_INVALID", "Backend PIE state did not confirm the exact live scene", {
                  status: 502,
                });
              }
              return { confirmed_live: true };
            },
            stop: async ({ signal }) => {
              const receipt = await this.runtimeLifecycle.stopForIdentity(identity, { signal });
              if (!isPlainObject(receipt) || receipt.schema !== "vista-runtime-stop/v2"
                  || receipt.phase !== "stopped" || receipt.confirmed_stopped !== true
                  || typeof receipt.ended_pie !== "boolean") {
                fail("ANIMATION_RUNTIME_LIFECYCLE_INVALID", "Backend PIE Stop was not confirmed", {
                  status: 502,
                });
              }
              const state = await this.runtimeLifecycle.stateForIdentity(identity, {
                sceneProof: resolved.buildRuntimeProof,
                signal,
                force: true,
              });
              if (!isPlainObject(state) || state.schema !== "vista-runtime-state/v2"
                  || state.pie !== false || state.possessed !== false) {
                fail("ANIMATION_RUNTIME_LIFECYCLE_INVALID", "Backend PIE state did not confirm Stop", {
                  status: 502,
                });
              }
              return { confirmed_stopped: true, ended_pie: receipt.ended_pie };
            },
          },
        } : {}),
      });
      for (const method of ["start", "stop", "getRun", "waitForRun"]) {
        if (!scheduler || typeof scheduler[method] !== "function") throw new Error(`scheduler.${method} is unavailable`);
      }
    } catch (error) {
      const failed = await this._terminalFailureRecord(persisted, error, "ANIMATION_SCHEDULER_UNAVAILABLE");
      throw new VistaAnimationTimelineServiceError(failed.error.code, "Animation scheduler is unavailable", { status: 503, retryable: true });
    }
    const schedulerAccess = {
      runId,
      ownerId: identity.ownerId,
      sessionId: identity.sessionId,
    };
    let initial;
    try {
      initial = scheduler.start({
        timeline: compiled.timeline,
        ownerId: identity.ownerId,
        sessionId: identity.sessionId,
        slotId: String(identity.slotId),
        correlationId: `animation:${runId}`,
      });
      validateTimelineRunArtifact(initial);
    } catch (error) {
      const failed = await this._terminalFailureRecord(persisted, error, "ANIMATION_START_FAILED");
      compiled.bundle.runtime.discardEvidence(runId);
      throw new VistaAnimationTimelineServiceError(failed.error.code, "Animation scheduler could not start", { status: 503, retryable: true });
    }
    const active = {
      importId,
      identity,
      record: persisted,
      scheduler,
      schedulerAccess,
      runtime: compiled.bundle.runtime,
      program: compiled.program,
      completion: null,
    };
    this.activeRuns.set(key, active);
    this.activeSlots.set(this._slotKey(identity), key);
    active.completion = Promise.resolve(scheduler.waitForRun(schedulerAccess))
      .then((run) => this._finalizeRun(active, run))
      .catch((error) => {
        if (isArtifactJournalFailure(error)) throw error;
        return this._terminalFailureRecord(active.record, error, "ANIMATION_RUN_FAILED");
      })
      .finally(() => {
        compiled.bundle.runtime.discardEvidence(runId);
        this.activeRuns.delete(key);
        if (this.activeSlots.get(this._slotKey(identity)) === key) this.activeSlots.delete(this._slotKey(identity));
      });
    return this._startResponse(persisted);
  }

  async _finalizeRun(active, run) {
    validateTimelineRunArtifact(run);
    let evidence = null;
    let status = schedulerStateToRecord(run.state);
    let error = null;
    try {
      evidence = await active.runtime.finalizeEvidence({ program: active.program, run });
    } catch (rawError) {
      const normalized = safeDependencyError(rawError, "ANIMATION_EVIDENCE_FINALIZE_FAILED", "Animation evidence could not be finalized", 502);
      status = "failed";
      error = { code: normalized.code, retryable: normalized.retryable };
    }
    const terminal = {
      ...active.record,
      status,
      operation: { ...active.record.operation, state: "terminal" },
      updated_at: nowIso(this.clock),
      run: cloneJson(run),
      evidence: evidence ? cloneJson(evidence) : null,
      error,
    };
    active.record = await this._persistRecord(terminal);
    await this._ensureTerminalJournal(active.record);
    return active.record;
  }

  async _terminalFailureRecord(record, rawError, fallbackCode) {
    const error = safeDependencyError(rawError, fallbackCode, "Animation run failed", 503);
    const failed = {
      ...record,
      status: "failed",
      operation: { ...record.operation, state: "terminal" },
      updated_at: nowIso(this.clock),
      error: { code: error.code, retryable: error.retryable },
    };
    const persisted = await this._persistRecord(failed);
    await this._ensureTerminalJournal(persisted);
    return persisted;
  }

  async _markOrphaned(record) {
    const persisted = await this._persistRecord({
      ...record,
      status: "failed",
      operation: { ...record.operation, state: "terminal" },
      updated_at: nowIso(this.clock),
      error: { code: "ANIMATION_RUN_RECOVERY_REQUIRED", retryable: false },
    });
    await this._ensureTerminalJournal(persisted);
    return persisted;
  }

  async _ensureTerminalJournal(record) {
    if (!this.artifactRecorder || !record || !TERMINAL_STATES.has(record.status)) return;
    await this.artifactRecorder.ensureTimelineTerminal({ record });
  }

  _lookupPrepared(identity, confirmation) {
    const prepared = this.prepared.get(this._preparedKey(identity, confirmation.preflightId));
    if (!prepared || !sameIdentity(prepared.identity, identity)) {
      fail("ANIMATION_PREFLIGHT_NOT_FOUND", "Exact animation preflight was not found for this active Studio lease", { status: 404 });
    }
    if (prepared.planId !== confirmation.planId
        || prepared.timelineId !== confirmation.timelineId
        || prepared.programId !== confirmation.programId) {
      fail("ANIMATION_PREFLIGHT_STALE", "Animation preflight confirmation ids do not match", { status: 409 });
    }
    if (nowMilliseconds(this.clock) > prepared.expiresAtMs) {
      this.prepared.delete(this._preparedKey(identity, confirmation.preflightId));
      fail("ANIMATION_PREFLIGHT_EXPIRED", "Animation preflight expired", { status: 409, retryable: true });
    }
    return prepared;
  }

  _prunePrepared(nowMs) {
    for (const [key, prepared] of this.prepared) {
      if (prepared.expiresAtMs < nowMs) this.prepared.delete(key);
    }
    while (this.prepared.size >= this.maxPrepared) {
      const first = this.prepared.keys().next().value;
      this.prepared.delete(first);
    }
  }

  _preparedKey(identity, preflightId) {
    return `${ownerDigest(identity.ownerId)}:${identity.sessionId}:${identity.slotId}:${leaseDigest(identity.leaseId)}:${preflightId}`;
  }

  _slotKey(identity) {
    return `${ownerDigest(identity.ownerId)}:${identity.slotId}`;
  }

  _runKey(ownerId, runId) {
    return `${ownerDigest(ownerId)}:${runId}`;
  }

  _preflightResponse(prepared, compiled) {
    return {
      schema: ANIMATION_PREFLIGHT_SERVICE_SCHEMA,
      import_artifact_id: prepared.importId,
      profile_id: prepared.profileId,
      plan_id: prepared.planId,
      scene_id: prepared.sceneId,
      preflight_id: prepared.preflightId,
      timeline_id: prepared.timelineId,
      program_id: prepared.programId,
      ready: true,
      expires_at: new Date(prepared.expiresAtMs).toISOString(),
      runtime_revision_digest: prepared.runtimeRevisionDigest,
      fps: compiled.program.fps,
      duration_sec: compiled.program.duration_sec,
      events: compiled.program.events.map((event) => ({
        event_id: event.event_id,
        order: event.order,
        at_sec: event.at_sec,
        at_frame: event.at_frame,
        frame_order: event.frame_order,
        action: event.action,
      })),
    };
  }

  _startResponse(record) {
    return {
      schema: ANIMATION_START_SERVICE_SCHEMA,
      import_artifact_id: record.identity.import_artifact_id,
      profile_id: record.identity.profile_id,
      plan_id: record.identity.plan_id,
      scene_id: record.identity.scene_id,
      preflight_id: record.identity.preflight_id,
      timeline_id: record.identity.timeline_id,
      program_id: record.identity.program_id,
      operation_id: record.operation_id,
      run_id: record.run_id,
      status: record.status,
      replay_of: record.operation.replay_of,
    };
  }

  _statusResponse(record, run, evidence) {
    return {
      schema: ANIMATION_STATUS_SERVICE_SCHEMA,
      import_artifact_id: record.identity.import_artifact_id,
      profile_id: record.identity.profile_id,
      plan_id: record.identity.plan_id,
      scene_id: record.identity.scene_id,
      preflight_id: record.identity.preflight_id,
      timeline_id: record.identity.timeline_id,
      program_id: record.identity.program_id,
      operation_id: record.operation_id,
      run_id: record.run_id,
      status: run && !TERMINAL_STATES.has(record.status) ? schedulerStateToRecord(run.state) : record.status,
      replay_of: record.operation.replay_of,
      run: publicRun(run),
      evidence: evidence ? cloneJson(evidence) : null,
      error: record.error ? { ...record.error } : null,
      created_at: record.created_at,
      updated_at: record.updated_at,
    };
  }

  async _readRecord(runId, ownerId, { allowMissing }) {
    const rootExists = await ensureRecordRoot(this.recordRoot, { create: false });
    if (!rootExists) {
      if (allowMissing) return null;
      fail("ANIMATION_RUN_NOT_FOUND", "Animation run was not found", { status: 404 });
    }
    const target = recordPath(this.recordRoot, runId, ownerId);
    let handle;
    try {
      handle = await fs.promises.open(target, fs.constants.O_RDONLY | Number(fs.constants.O_NOFOLLOW || 0));
      const stat = await handle.stat();
      if (!stat.isFile() || stat.size < 2 || stat.size > this.maxRecordBytes) {
        fail("ANIMATION_RECORD_CORRUPT", "Stored animation run record failed validation", { status: 500 });
      }
      let value;
      try {
        value = JSON.parse(await handle.readFile("utf8"));
      } catch {
        fail("ANIMATION_RECORD_CORRUPT", "Stored animation run record failed validation", { status: 500 });
      }
      return validateStoredRecord(value, runId, ownerId);
    } catch (error) {
      if (error && error.code === "ENOENT" && allowMissing) return null;
      if (error && error.code === "ENOENT") fail("ANIMATION_RUN_NOT_FOUND", "Animation run was not found", { status: 404 });
      if (error instanceof VistaAnimationTimelineServiceError) throw error;
      fail("ANIMATION_STORAGE_UNAVAILABLE", "Animation record storage is unavailable", { status: 503, retryable: true });
    } finally {
      if (handle) await handle.close().catch(() => {});
    }
  }

  async _persistRecord(record) {
    const serialized = `${JSON.stringify(record, null, 2)}\n`;
    if (Buffer.byteLength(serialized, "utf8") > this.maxRecordBytes) {
      fail("ANIMATION_RECORD_TOO_LARGE", "Animation run record exceeds its size limit", { status: 500 });
    }
    await ensureRecordRoot(this.recordRoot, { create: true });
    const target = recordPath(this.recordRoot, record.run_id, record.access.owner_id);
    const suffix = this.randomBytes(12).toString("hex");
    if (!/^[a-f0-9]{24}$/.test(suffix)) fail("ANIMATION_RANDOM_INVALID", "Animation record random source is invalid", { status: 500 });
    const temporary = path.resolve(this.recordRoot, `.tmp-${path.basename(target)}-${suffix}`);
    let handle;
    try {
      handle = await fs.promises.open(temporary, "wx", 0o600);
      await handle.writeFile(serialized, "utf8");
      await handle.sync();
      await handle.close();
      handle = null;
      await fs.promises.rename(temporary, target);
      return validateStoredRecord(record, record.run_id, record.access.owner_id);
    } catch (error) {
      if (error instanceof VistaAnimationTimelineServiceError) throw error;
      fail("ANIMATION_STORAGE_UNAVAILABLE", "Animation record storage is unavailable", { status: 503, retryable: true });
    } finally {
      if (handle) await handle.close().catch(() => {});
      await fs.promises.unlink(temporary).catch(() => {});
    }
  }
}

function createVistaAnimationTimelineService(options) {
  return new VistaAnimationTimelineService(options);
}

module.exports = {
  ANIMATION_PREFLIGHT_SERVICE_SCHEMA,
  ANIMATION_PUBLIC_RUN_SCHEMA,
  ANIMATION_RUN_RECORD_SCHEMA,
  ANIMATION_START_SERVICE_SCHEMA,
  ANIMATION_STATUS_SERVICE_SCHEMA,
  VistaAnimationTimelineService,
  VistaAnimationTimelineServiceError,
  createVistaAnimationTimelineService,
};
