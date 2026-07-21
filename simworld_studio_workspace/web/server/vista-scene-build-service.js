"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const { compileVistaSceneBuildPlan } = require("./vista-scene-build-plan");

const PLAN_RESPONSE_SCHEMA = "vista-scene-build-plan-response/v1";
const PREFLIGHT_RESPONSE_SCHEMA = "vista-scene-build-service-preflight/v1";
const EXECUTION_RESPONSE_SCHEMA = "vista-scene-build-service-execution/v1";
const BUILD_RECORD_SCHEMA = "vista-scene-build-record/v1";
const PROFILE_ID_PATTERN = /^[a-z0-9][a-z0-9._-]{0,79}$/;
const PLAN_ID_PATTERN = /^vsp-[a-f0-9]{24}$/;
const PRINCIPAL_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$/;
const MAX_RECORD_BYTES = 32 * 1024 * 1024;

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
    this.clock = typeof options.clock === "function" ? options.clock : () => new Date();
    this.randomBytes = typeof options.randomBytes === "function" ? options.randomBytes : crypto.randomBytes;
    this.maxRecordBytes = Number.isSafeInteger(options.maxRecordBytes) && options.maxRecordBytes > 0
      ? options.maxRecordBytes
      : MAX_RECORD_BYTES;
    this.activeExecutions = new Map();
  }

  async plan(importArtifactId, request = {}, context = {}) {
    exactKeys(request, ["profile_id"], []);
    const resolved = await this._resolvePlan(importArtifactId, request.profile_id, context);
    const record = await this._readRecord(resolved.plan.plan_id, resolved.access, { allowMissing: true });
    return this._planResponse(resolved, record);
  }

  async status(importArtifactId, request = {}, context = {}) {
    exactKeys(request, ["profile_id"], []);
    const resolved = await this._resolvePlan(importArtifactId, request.profile_id, context);
    const record = await this._readRecord(resolved.plan.plan_id, resolved.access, { allowMissing: true });
    return this._planResponse(resolved, record);
  }

  async preflight(importArtifactId, request = {}, context = {}) {
    exactKeys(request, ["plan_id", "profile_id"], ["plan_id"]);
    const resolved = await this._resolvePlan(importArtifactId, request.profile_id, context);
    this._assertPlanId(resolved.plan, request.plan_id);
    this._requireExecutor();
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

  async _beginExecution(importArtifactId, request = {}, context = {}) {
    exactKeys(request, ["plan_id", "profile_id", "confirm"], ["plan_id", "confirm"]);
    if (request.confirm !== true) fail("SCENE_BUILD_CONFIRMATION_REQUIRED", "Exact BuildPlan confirmation is required", { status: 428 });
    const resolved = await this._resolvePlan(importArtifactId, request.profile_id, context);
    this._assertPlanId(resolved.plan, request.plan_id);
    this._requireExecutor();
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
    try {
      const result = validateExecutorResult(
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
      const record = await this._writeRecord(resolved, result, operationId);
      return {
        schema: EXECUTION_RESPONSE_SCHEMA,
        import_artifact_id: resolved.importArtifactId,
        profile_id: resolved.profileId,
        plan_id: resolved.plan.plan_id,
        operation_id: operationId,
        status: record.status,
        result: record.result,
      };
    } catch (rawError) {
      const error = safeExecutionError(rawError);
      try {
        const result = error.result
          ? validateExecutorResult(error.result, resolved.plan)
          : conservativeFailureResult(resolved.plan, error);
        await this._writeRecord(resolved, result, operationId);
      } catch (_recordError) {}
      throw error;
    }
  }

  _planResponse(resolved, record) {
    return {
      schema: PLAN_RESPONSE_SCHEMA,
      import_artifact_id: resolved.importArtifactId,
      profile_id: resolved.profileId,
      state: record ? record.status : "planned",
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
    return { access, importArtifactId: artifact.artifact_id || importArtifactId, profileId, plan };
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
