"use strict";

const {
  CAPABILITY_REGISTRY_SCHEMA,
  DEFAULT_FPS,
  VistaAnimationContractError,
  ACTION_DEFINITIONS,
  buildAnimationPreflightArtifact,
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
} = require("./vista-animation-contract");
const { validateBindings } = require("./vista-timeline-compiler");
const { validateTimelineRunArtifact } = require("./vista-timeline-scheduler");
const {
  AGENT_ACTION_AUTHORIZATION_SCHEMA,
  createVistaAnimationAgentActionPolicy,
  isVistaLegacyAgentActionProfile,
} = require("./vista-animation-agent-action-registry");

const ANIMATION_EVIDENCE_SCHEMA = "vista-animation-evidence/v1";
const SNAPSHOT_REQUEST_SCHEMA = "vista-animation-snapshot-request/v1";
const SNAPSHOT_RESPONSE_SCHEMA = "vista-animation-snapshot-response/v1";
const START_REQUEST_SCHEMA = "vista-animation-start-request/v1";
const START_RESPONSE_SCHEMA = "vista-animation-start-response/v1";
const WAIT_REQUEST_SCHEMA = "vista-animation-wait-request/v1";
const WAIT_RESPONSE_SCHEMA = "vista-animation-wait-response/v1";
const STOP_REQUEST_SCHEMA = "vista-animation-stop-request/v1";
const STOP_RESPONSE_SCHEMA = "vista-animation-stop-response/v1";
const RELEASE_REQUEST_SCHEMA = "vista-animation-release-request/v1";
const RELEASE_RESPONSE_SCHEMA = "vista-animation-release-response/v1";
const RESTORE_REQUEST_SCHEMA = "vista-animation-restore-request/v1";
const RESTORE_RESPONSE_SCHEMA = "vista-animation-restore-response/v1";

const SAFE_ID_RE = /^[a-z][a-z0-9_-]{0,119}$/;
const OPAQUE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,159}$/;
const SHA256_RE = /^[a-f0-9]{64}$/;
const ARTIFACT_REF_RE = /^[A-Za-z0-9][A-Za-z0-9._/@-]{0,511}$/;
const TERMINAL_RUN_STATES = new Set(["completed", "failed", "cancelled"]);
const CLEANUP_ROLLBACK_REASONS = new Set(["failed", "cancelled", "timed_out"]);
const INTERACTION_ACTIONS = new Set(["brace", "drag", "lift_foot", "pick_up"]);
const EVIDENCE_HOOKS = Object.freeze(["pose_snapshot", "interaction_state", "screenshot", "scene_validation"]);
const PHASE_ORDER = Object.freeze({ before: 0, after: 1, rollback: 2, terminal: 3 });

class VistaAnimationRuntimeError extends Error {
  constructor(code, message, { status = 400, retryable = false, details = {} } = {}) {
    super(message);
    this.name = "VistaAnimationRuntimeError";
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
  throw new VistaAnimationRuntimeError(code, message, options);
}

function exactKeys(value, allowed, required, pointer, code = "ANIMATION_RUNTIME_INPUT_INVALID") {
  if (!isPlainObject(value)) fail(code, `${pointer} must be an object`);
  const allowedSet = new Set(allowed);
  const unknown = Object.keys(value).filter((key) => !allowedSet.has(key));
  const missing = required.filter((key) => !Object.prototype.hasOwnProperty.call(value, key));
  if (unknown.length || missing.length) fail(code, `${pointer} has an invalid shape`, { details: { pointer, unknown, missing } });
}

function requireString(value, pointer, { pattern = null, max = 512, code = "ANIMATION_RUNTIME_INPUT_INVALID" } = {}) {
  if (typeof value !== "string" || value.length === 0 || value.length > max || (pattern && !pattern.test(value))) {
    fail(code, `${pointer} is invalid`, { details: { pointer } });
  }
  return value;
}

function requireFinite(value, pointer, { min = -Infinity, max = Infinity, code = "ANIMATION_RUNTIME_PROTOCOL_INVALID" } = {}) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < min || value > max) fail(code, `${pointer} is invalid`, { status: 502 });
  return value;
}

function nowIso(clock) {
  const value = clock();
  if (typeof value !== "string" || Number.isNaN(Date.parse(value))) fail("ANIMATION_RUNTIME_CLOCK_INVALID", "Animation runtime clock returned an invalid date-time", { status: 500 });
  return value;
}

function throwIfAborted(signal) {
  if (!signal || !signal.aborted) return;
  fail("ANIMATION_ACTION_ABORTED", "Animation action was aborted", { status: 499 });
}

function normalizeTransportError(error, code, message) {
  if (error instanceof VistaAnimationRuntimeError || error instanceof VistaAnimationContractError) return error;
  if (error && (error.name === "AbortError" || error.code === "ABORT_ERR" || error.code === "UE_COMMAND_ABORTED")) {
    return new VistaAnimationRuntimeError("ANIMATION_ACTION_ABORTED", "Animation action was aborted", { status: 499 });
  }
  return new VistaAnimationRuntimeError(code, message, {
    status: 503,
    retryable: true,
    details: { cause: error && typeof error.code === "string" ? error.code : undefined },
  });
}

async function callBroker(broker, method, request, signal, code, message) {
  throwIfAborted(signal);
  try {
    const result = await broker[method](deepFreeze(cloneJson(request)), { signal });
    throwIfAborted(signal);
    return result;
  } catch (error) {
    throw normalizeTransportError(error, code, message);
  }
}

function validateSnapshotResponse(request, response) {
  const code = "ANIMATION_SNAPSHOT_PROTOCOL_INVALID";
  exactKeys(response, ["schema", "status", "snapshot_id", "action", "actor_binding_id", "target_binding_id", "engine_time", "state_digest"], ["schema", "status", "snapshot_id", "action", "actor_binding_id", "target_binding_id", "engine_time", "state_digest"], "snapshot response", code);
  if (
    response.schema !== SNAPSHOT_RESPONSE_SCHEMA
    || response.status !== "captured"
    || response.action !== request.action
    || response.actor_binding_id !== request.actor_binding_id
    || response.target_binding_id !== request.target_binding_id
  ) fail(code, "Animation snapshot response does not match the request", { status: 502 });
  return deepFreeze({
    schema: SNAPSHOT_RESPONSE_SCHEMA,
    status: "captured",
    snapshot_id: requireString(response.snapshot_id, "snapshot response.snapshot_id", { pattern: OPAQUE_ID_RE, max: 160, code }),
    action: request.action,
    actor_binding_id: request.actor_binding_id,
    target_binding_id: request.target_binding_id,
    engine_time: requireFinite(response.engine_time, "snapshot response.engine_time", { min: 0, max: 315_360_000, code }),
    state_digest: requireString(response.state_digest, "snapshot response.state_digest", { pattern: SHA256_RE, max: 64, code }),
  });
}

function validateStartResponse(request, response) {
  const code = "ANIMATION_START_PROTOCOL_INVALID";
  exactKeys(response, ["schema", "status", "action_handle", "bridge_action_id", "actor_binding_id", "target_binding_id", "engine_time"], ["schema", "status", "action_handle", "bridge_action_id", "actor_binding_id", "target_binding_id", "engine_time"], "start response", code);
  if (
    response.schema !== START_RESPONSE_SCHEMA
    || response.status !== "started"
    || response.bridge_action_id !== request.bridge_action_id
    || response.actor_binding_id !== request.actor_binding_id
    || response.target_binding_id !== request.target_binding_id
  ) fail(code, "Animation start response does not match the request", { status: 502 });
  return deepFreeze({
    schema: START_RESPONSE_SCHEMA,
    status: "started",
    action_handle: requireString(response.action_handle, "start response.action_handle", { pattern: OPAQUE_ID_RE, max: 160, code }),
    bridge_action_id: request.bridge_action_id,
    actor_binding_id: request.actor_binding_id,
    target_binding_id: request.target_binding_id,
    engine_time: requireFinite(response.engine_time, "start response.engine_time", { min: 0, max: 315_360_000, code }),
  });
}

function validateWaitResponse(request, response, expectedSignal) {
  const code = "ANIMATION_WAIT_PROTOCOL_INVALID";
  exactKeys(response, ["schema", "status", "action_handle", "completion_signal", "engine_time", "evidence_ids"], ["schema", "status", "action_handle", "completion_signal", "engine_time", "evidence_ids"], "wait response", code);
  if (response.schema !== WAIT_RESPONSE_SCHEMA || response.status !== "completed" || response.action_handle !== request.action_handle || response.completion_signal !== expectedSignal) {
    fail(code, "Animation completion response does not match the verified completion signal", { status: 502 });
  }
  if (!Array.isArray(response.evidence_ids) || response.evidence_ids.length > 32) fail(code, "Animation completion evidence ids are invalid", { status: 502 });
  const evidenceIds = response.evidence_ids.map((value, index) => requireString(value, `wait response.evidence_ids[${index}]`, { pattern: OPAQUE_ID_RE, max: 160, code }));
  if (new Set(evidenceIds).size !== evidenceIds.length) fail(code, "Animation completion evidence ids must be unique", { status: 502 });
  return deepFreeze({
    schema: WAIT_RESPONSE_SCHEMA,
    status: "completed",
    action_handle: request.action_handle,
    completion_signal: expectedSignal,
    engine_time: requireFinite(response.engine_time, "wait response.engine_time", { min: 0, max: 315_360_000, code }),
    evidence_ids: evidenceIds.sort(),
  });
}

function validateStopResponse(request, response) {
  const code = "ANIMATION_STOP_PROTOCOL_INVALID";
  exactKeys(response, ["schema", "status", "action_handle", "engine_time"], ["schema", "status", "action_handle", "engine_time"], "stop response", code);
  if (response.schema !== STOP_RESPONSE_SCHEMA || !new Set(["stopped", "already_stopped"]).has(response.status) || response.action_handle !== request.action_handle) {
    fail(code, "Animation stop response does not match the action handle", { status: 502 });
  }
  return deepFreeze({
    schema: STOP_RESPONSE_SCHEMA,
    status: response.status,
    action_handle: request.action_handle,
    engine_time: requireFinite(response.engine_time, "stop response.engine_time", { min: 0, max: 315_360_000, code }),
  });
}

function validateReleaseResponse(request, response) {
  const code = "ANIMATION_RELEASE_PROTOCOL_INVALID";
  exactKeys(response, ["schema", "status", "action_handle"], ["schema", "status", "action_handle"], "release response", code);
  if (response.schema !== RELEASE_RESPONSE_SCHEMA || !new Set(["released", "already_released"]).has(response.status) || response.action_handle !== request.action_handle) {
    fail(code, "Animation release response does not match the action handle", { status: 502 });
  }
  return deepFreeze({ schema: RELEASE_RESPONSE_SCHEMA, status: response.status, action_handle: request.action_handle });
}

function validateRestoreResponse(request, response) {
  const code = "ANIMATION_RESTORE_PROTOCOL_INVALID";
  exactKeys(response, ["schema", "status", "snapshot_id", "state_digest", "engine_time"], ["schema", "status", "snapshot_id", "state_digest", "engine_time"], "restore response", code);
  if (response.schema !== RESTORE_RESPONSE_SCHEMA || response.status !== "restored" || response.snapshot_id !== request.snapshot_id || response.state_digest !== request.state_digest) {
    fail(code, "Animation restore response does not prove exact snapshot restoration", { status: 502 });
  }
  return deepFreeze({
    schema: RESTORE_RESPONSE_SCHEMA,
    status: "restored",
    snapshot_id: request.snapshot_id,
    state_digest: request.state_digest,
    engine_time: requireFinite(response.engine_time, "restore response.engine_time", { min: 0, max: 315_360_000, code }),
  });
}

function validateEvidenceHooks(hooks) {
  exactKeys(hooks, EVIDENCE_HOOKS, EVIDENCE_HOOKS, "animation evidence hooks", "ANIMATION_EVIDENCE_CONFIG_INVALID");
  for (const name of EVIDENCE_HOOKS) {
    if (typeof hooks[name] !== "function") fail("ANIMATION_EVIDENCE_CONFIG_INVALID", `evidence hook '${name}' must be a function`, { status: 500 });
  }
  return Object.freeze({ ...hooks });
}

function validateEvidenceDescriptor(kind, value) {
  const code = "ANIMATION_EVIDENCE_INVALID";
  if (!EVIDENCE_HOOKS.includes(kind)) fail(code, `Unknown animation evidence kind '${kind}'`);
  exactKeys(value, ["evidence_id", "artifact_ref", "sha256", "assertion"], ["evidence_id", "artifact_ref", "sha256", "assertion"], `${kind} evidence`, code);
  const evidenceId = requireString(value.evidence_id, `${kind} evidence.evidence_id`, { pattern: OPAQUE_ID_RE, max: 160, code });
  const artifactRef = requireString(value.artifact_ref, `${kind} evidence.artifact_ref`, { pattern: ARTIFACT_REF_RE, max: 512, code });
  if (artifactRef.includes("..") || artifactRef.includes("//") || artifactRef.startsWith("/")) fail(code, `${kind} evidence.artifact_ref must be an opaque relative reference`);
  const sha256 = requireString(value.sha256, `${kind} evidence.sha256`, { pattern: SHA256_RE, max: 64, code });
  if (value.assertion !== null && !new Set(["pass", "fail"]).has(value.assertion)) fail(code, `${kind} evidence.assertion is invalid`);
  return deepFreeze({ kind, evidence_id: evidenceId, artifact_ref: artifactRef, sha256, assertion: value.assertion });
}

function createEvidenceRecorder({ hooks, clock, fps }) {
  const records = new Map();
  const createdAt = new Map();
  const manifests = new Map();

  function runRecords(runId) {
    let value = records.get(runId);
    if (!value) {
      value = new Map();
      records.set(runId, value);
      createdAt.set(runId, nowIso(clock));
    }
    return value;
  }

  async function invoke(kind, context) {
    let raw;
    try {
      raw = await hooks[kind](deepFreeze(cloneJson(context)));
    } catch (error) {
      throw normalizeTransportError(error, "ANIMATION_EVIDENCE_CAPTURE_FAILED", `Animation evidence hook '${kind}' failed`);
    }
    return validateEvidenceDescriptor(kind, raw);
  }

  async function capture({ phase, context, snapshot = null, actionHandle = null }) {
    if (!Object.prototype.hasOwnProperty.call(PHASE_ORDER, phase)) fail("ANIMATION_EVIDENCE_INPUT_INVALID", `Unknown animation evidence phase '${phase}'`);
    const runId = requireString(context.run_id, "evidence context.run_id", { pattern: OPAQUE_ID_RE, max: 160, code: "ANIMATION_EVIDENCE_INPUT_INVALID" });
    const eventId = phase === "terminal" ? null : requireString(context.event_id, "evidence context.event_id", { pattern: /^beat-[0-9]{4}(?:-[a-z0-9_-]+)?$/, max: 120, code: "ANIMATION_EVIDENCE_INPUT_INVALID" });
    const attempt = phase === "terminal" ? 0 : context.attempt;
    if (!Number.isInteger(attempt) || attempt < 0 || attempt > 100) fail("ANIMATION_EVIDENCE_INPUT_INVALID", "Evidence attempt is invalid");
    const key = `${eventId || "terminal"}:${attempt}:${phase}`;
    const existing = runRecords(runId).get(key);
    if (existing) return existing;
    const base = {
      schema: "vista-animation-evidence-hook-context/v1",
      run_id: runId,
      timeline_id: context.timeline_id,
      scene_revision: context.scene_revision,
      event_id: eventId,
      action: phase === "terminal" ? null : context.action,
      actor_binding_id: phase === "terminal" ? null : context.actor_binding_id,
      target_binding_id: phase === "terminal" ? null : context.target_binding_id,
      planned_sec: phase === "terminal" ? context.duration_sec : context.planned_sec,
      at_frame: Math.round((phase === "terminal" ? context.duration_sec : context.planned_sec) * fps),
      attempt,
      phase,
      snapshot_id: snapshot ? snapshot.snapshot_id : null,
      action_handle: actionHandle,
    };
    const kinds = phase === "before"
      ? ["pose_snapshot", ...(base.target_binding_id ? ["interaction_state"] : [])]
      : phase === "after"
        ? ["pose_snapshot", ...(INTERACTION_ACTIONS.has(base.action) ? ["interaction_state"] : []), "screenshot"]
        : phase === "rollback"
          ? ["pose_snapshot", ...(base.target_binding_id ? ["interaction_state"] : []), "screenshot"]
          : ["pose_snapshot", "screenshot", "scene_validation"];
    const evidence = [];
    for (const kind of kinds) evidence.push(await invoke(kind, base));
    const assertionEvidence = evidence.find((entry) => entry.kind === "interaction_state" && phase === "after");
    if (assertionEvidence && assertionEvidence.assertion !== "pass") {
      fail("ANIMATION_INTERACTION_ASSERTION_FAILED", `Action '${base.action}' did not prove its IK/object interaction`, { status: 502 });
    }
    const sceneEvidence = evidence.find((entry) => entry.kind === "scene_validation");
    if (sceneEvidence && sceneEvidence.assertion !== "pass") fail("ANIMATION_SCENE_VALIDATION_FAILED", "Terminal scene validation did not pass", { status: 502 });
    const record = deepFreeze({
      phase,
      event_id: eventId,
      action: base.action,
      attempt,
      planned_sec: base.planned_sec,
      at_frame: base.at_frame,
      captured_at: nowIso(clock),
      evidence,
    });
    runRecords(runId).set(key, record);
    return record;
  }

  async function finalize({ program, run }) {
    validateVistaAnimationProgram(program);
    validateTimelineRunArtifact(run);
    if (program.fps !== fps) fail("ANIMATION_EVIDENCE_REVISION_MISMATCH", "Evidence recorder and animation program FPS differ", { status: 409 });
    if (run.timeline_id !== program.timeline_id || run.scene_revision !== program.scene_revision || !TERMINAL_RUN_STATES.has(run.state)) {
      fail("ANIMATION_EVIDENCE_RUN_INVALID", "Only a matching terminal timeline run can be finalized", { status: 409 });
    }
    const runDigest = digest(run);
    const existingManifest = manifests.get(run.run_id);
    if (existingManifest) {
      if (existingManifest.program_id !== program.program_id || existingManifest.run_digest !== runDigest) fail("ANIMATION_EVIDENCE_REPLAY_MISMATCH", "A run id cannot be finalized against different program or run evidence", { status: 409 });
      return existingManifest.manifest;
    }
    await capture({
      phase: "terminal",
      context: {
        run_id: run.run_id,
        timeline_id: run.timeline_id,
        scene_revision: run.scene_revision,
        duration_sec: program.duration_sec,
      },
    });
    const eventProgram = new Map(program.events.map((entry) => [entry.event_id, entry]));
    const terminalProgram = program.checkpoints.find((entry) => entry.kind === "terminal");
    const checkpoints = [...(records.get(run.run_id) || new Map()).values()].map((record) => {
      const planned = record.phase === "terminal" ? terminalProgram : eventProgram.get(record.event_id);
      if (!planned) fail("ANIMATION_EVIDENCE_EVENT_UNKNOWN", "Evidence references an event outside the animation program", { status: 500 });
      if (record.planned_sec !== planned.at_sec || record.at_frame !== planned.at_frame) fail("ANIMATION_EVIDENCE_TIMING_MISMATCH", "Evidence timing does not match its animation program checkpoint", { status: 500 });
      return {
        checkpoint_id: `vek-${digest({ run_id: run.run_id, event_id: record.event_id, attempt: record.attempt, phase: record.phase }).slice(0, 20)}`,
        phase: record.phase,
        event_id: record.event_id,
        action: record.action,
        attempt: record.attempt,
        at_sec: record.planned_sec,
        at_frame: planned.at_frame,
        frame_order: planned.frame_order,
        captured_at: record.captured_at,
        evidence: record.evidence,
      };
    }).sort((left, right) => left.at_frame - right.at_frame
      || left.frame_order - right.frame_order
      || PHASE_ORDER[left.phase] - PHASE_ORDER[right.phase]
      || left.checkpoint_id.localeCompare(right.checkpoint_id));
    const checkpointKeys = new Set(checkpoints.map((entry) => `${entry.event_id || "terminal"}:${entry.attempt}:${entry.phase}`));
    const missing = [];
    for (const event of run.events) {
      if (event.state !== "completed") continue;
      for (const phase of ["before", "after"]) {
        if (!checkpointKeys.has(`${event.event_id}:${event.attempt}:${phase}`)) missing.push(`${event.event_id}:${phase}`);
      }
    }
    if (!checkpointKeys.has("terminal:0:terminal")) missing.push("terminal:terminal");
    if (run.state === "completed" && missing.length) fail("ANIMATION_EVIDENCE_INCOMPLETE", "Completed animation run is missing required evidence", { status: 502, details: { missing } });
    const completedEvents = run.events.filter((entry) => entry.state === "completed").length;
    const coverage = {
      required_event_count: program.events.length,
      completed_event_count: completedEvents,
      checkpoint_count: checkpoints.length,
      missing,
      complete: missing.length === 0 && completedEvents === program.events.length,
    };
    const identity = {
      program_id: program.program_id,
      run_id: run.run_id,
      run_digest: runDigest,
      checkpoints,
      coverage,
    };
    const manifest = {
      schema: ANIMATION_EVIDENCE_SCHEMA,
      manifest_id: `vae-${digest(identity).slice(0, 24)}`,
      program_id: program.program_id,
      run_id: run.run_id,
      timeline_id: program.timeline_id,
      scene_revision: program.scene_revision,
      content_revision: program.content_revision,
      fps: program.fps,
      duration_sec: program.duration_sec,
      run_state: run.state,
      created_at: createdAt.get(run.run_id),
      finalized_at: nowIso(clock),
      run_digest: runDigest,
      checkpoints,
      coverage,
    };
    const validated = deepFreeze(validateAnimationEvidenceManifest(manifest));
    manifests.set(run.run_id, { program_id: program.program_id, run_digest: runDigest, manifest: validated });
    return validated;
  }

  function discard(runId) {
    records.delete(runId);
    createdAt.delete(runId);
    manifests.delete(runId);
  }

  return Object.freeze({ capture, finalize, discard });
}

function validateAnimationEvidenceManifest(manifest) {
  const code = "ANIMATION_EVIDENCE_MANIFEST_INVALID";
  const keys = ["schema", "manifest_id", "program_id", "run_id", "timeline_id", "scene_revision", "content_revision", "fps", "duration_sec", "run_state", "created_at", "finalized_at", "run_digest", "checkpoints", "coverage"];
  exactKeys(manifest, keys, keys, "animation evidence manifest", code);
  if (manifest.schema !== ANIMATION_EVIDENCE_SCHEMA || !/^vae-[a-f0-9]{24}$/.test(manifest.manifest_id) || !/^vag-[a-f0-9]{24}$/.test(manifest.program_id) || !/^vtl-[a-f0-9]{24}$/.test(manifest.timeline_id)) fail(code, "Animation evidence manifest identity is invalid");
  requireString(manifest.run_id, "animation evidence.run_id", { pattern: OPAQUE_ID_RE, max: 160, code });
  requireString(manifest.scene_revision, "animation evidence.scene_revision", { pattern: OPAQUE_ID_RE, max: 160, code });
  requireString(manifest.content_revision, "animation evidence.content_revision", { pattern: OPAQUE_ID_RE, max: 160, code });
  requireString(manifest.run_digest, "animation evidence.run_digest", { pattern: SHA256_RE, max: 64, code });
  if (!Number.isInteger(manifest.fps) || manifest.fps < 1 || manifest.fps > 240 || !Number.isFinite(manifest.duration_sec) || manifest.duration_sec <= 0 || !TERMINAL_RUN_STATES.has(manifest.run_state)) fail(code, "Animation evidence timing/state is invalid");
  for (const field of ["created_at", "finalized_at"]) {
    if (typeof manifest[field] !== "string" || Number.isNaN(Date.parse(manifest[field]))) fail(code, `Animation evidence ${field} is invalid`);
  }
  if (!Array.isArray(manifest.checkpoints) || manifest.checkpoints.length === 0) fail(code, "Animation evidence checkpoints are missing");
  let previous = null;
  const ids = new Set();
  const evidenceIds = new Set();
  for (const checkpoint of manifest.checkpoints) {
    const checkpointKeys = ["checkpoint_id", "phase", "event_id", "action", "attempt", "at_sec", "at_frame", "frame_order", "captured_at", "evidence"];
    exactKeys(checkpoint, checkpointKeys, checkpointKeys, "animation evidence checkpoint", code);
    if (!/^vek-[a-f0-9]{20}$/.test(checkpoint.checkpoint_id) || ids.has(checkpoint.checkpoint_id) || !Object.prototype.hasOwnProperty.call(PHASE_ORDER, checkpoint.phase)) fail(code, "Animation evidence checkpoint identity is invalid");
    ids.add(checkpoint.checkpoint_id);
    if (!Number.isFinite(checkpoint.at_sec) || checkpoint.at_sec < 0 || checkpoint.at_sec > manifest.duration_sec || !Number.isInteger(checkpoint.at_frame) || checkpoint.at_frame < 0 || !Number.isInteger(checkpoint.frame_order) || checkpoint.frame_order < 0) fail(code, "Animation evidence checkpoint timing is invalid");
    if (checkpoint.at_frame !== Math.round(checkpoint.at_sec * manifest.fps) || typeof checkpoint.captured_at !== "string" || Number.isNaN(Date.parse(checkpoint.captured_at))) fail(code, "Animation evidence checkpoint time/frame proof is invalid");
    if (checkpoint.phase === "terminal") {
      if (checkpoint.event_id !== null || checkpoint.action !== null || checkpoint.attempt !== 0 || checkpoint.at_sec !== manifest.duration_sec) fail(code, "Terminal animation evidence checkpoint is invalid");
    } else {
      if (typeof checkpoint.event_id !== "string" || !/^beat-[0-9]{4}(?:-[a-z0-9_-]+)?$/.test(checkpoint.event_id) || !ACTION_DEFINITIONS[checkpoint.action] || !Number.isInteger(checkpoint.attempt) || checkpoint.attempt < 1 || checkpoint.attempt > 100) fail(code, "Event animation evidence checkpoint is invalid");
    }
    const tuple = [checkpoint.at_frame, checkpoint.frame_order, PHASE_ORDER[checkpoint.phase], checkpoint.checkpoint_id];
    if (previous && (tuple[0] < previous[0] || (tuple[0] === previous[0] && tuple[1] < previous[1]) || (tuple[0] === previous[0] && tuple[1] === previous[1] && tuple[2] < previous[2]))) fail(code, "Animation evidence checkpoints are not deterministically ordered");
    previous = tuple;
    if (!Array.isArray(checkpoint.evidence) || checkpoint.evidence.length === 0) fail(code, "Animation evidence checkpoint artifacts are missing");
    const kinds = new Set();
    checkpoint.evidence.forEach((entry) => {
      const validated = validateEvidenceDescriptor(entry.kind, {
        evidence_id: entry.evidence_id,
        artifact_ref: entry.artifact_ref,
        sha256: entry.sha256,
        assertion: entry.assertion,
      });
      if (kinds.has(validated.kind) || evidenceIds.has(validated.evidence_id)) fail(code, "Animation evidence kinds and ids must be unique");
      kinds.add(validated.kind);
      evidenceIds.add(validated.evidence_id);
    });
    const requiredKinds = checkpoint.phase === "before"
      ? ["pose_snapshot"]
      : checkpoint.phase === "terminal"
        ? ["pose_snapshot", "screenshot", "scene_validation"]
        : ["pose_snapshot", "screenshot"];
    if (requiredKinds.some((kind) => !kinds.has(kind))) fail(code, "Animation evidence checkpoint is missing required artifact kinds");
    if (checkpoint.phase === "after" && INTERACTION_ACTIONS.has(checkpoint.action)) {
      const interaction = checkpoint.evidence.find((entry) => entry.kind === "interaction_state");
      if (!interaction || interaction.assertion !== "pass") fail(code, "Animation interaction checkpoint is not proven");
    }
    if (checkpoint.phase === "terminal") {
      const scene = checkpoint.evidence.find((entry) => entry.kind === "scene_validation");
      if (!scene || scene.assertion !== "pass") fail(code, "Terminal scene validation is not proven");
    }
  }
  exactKeys(manifest.coverage, ["required_event_count", "completed_event_count", "checkpoint_count", "missing", "complete"], ["required_event_count", "completed_event_count", "checkpoint_count", "missing", "complete"], "animation evidence coverage", code);
  if (!Number.isInteger(manifest.coverage.required_event_count) || manifest.coverage.required_event_count < 1 || manifest.coverage.required_event_count > 1000 || !Number.isInteger(manifest.coverage.completed_event_count) || manifest.coverage.completed_event_count < 0 || manifest.coverage.completed_event_count > manifest.coverage.required_event_count || manifest.coverage.checkpoint_count !== manifest.checkpoints.length || !Array.isArray(manifest.coverage.missing) || new Set(manifest.coverage.missing).size !== manifest.coverage.missing.length || typeof manifest.coverage.complete !== "boolean") fail(code, "Animation evidence coverage is invalid");
  const computedComplete = manifest.coverage.missing.length === 0 && manifest.coverage.completed_event_count === manifest.coverage.required_event_count;
  if (manifest.coverage.complete !== computedComplete) fail(code, "Animation evidence coverage summary is inconsistent");
  if (manifest.run_state === "completed" && manifest.coverage.complete !== true) fail(code, "Completed animation evidence must have complete coverage");
  return manifest;
}

function stateKey(context) {
  return `${context.timeline_id}:${context.run_id}:${context.event_id}:${context.attempt}`;
}

function validateLifecycleContext(action, context, preflight) {
  const definition = ACTION_DEFINITIONS[action];
  if (!preflight.start_allowed) fail("ANIMATION_PREFLIGHT_BLOCKED", "Animation capability preflight does not allow runtime mutation", { status: 409 });
  requireString(context.run_id, "animation context.run_id", { pattern: OPAQUE_ID_RE, max: 160 });
  requireString(context.timeline_id, "animation context.timeline_id", { pattern: /^vtl-[a-f0-9]{24}$/, max: 28 });
  requireString(context.scene_revision, "animation context.scene_revision", { pattern: OPAQUE_ID_RE, max: 160 });
  if (context.scene_revision !== preflight.scene_revision) fail("ANIMATION_SCENE_REVISION_MISMATCH", "Animation event scene revision does not match its capability preflight", { status: 409 });
  requireString(context.event_id, "animation context.event_id", { pattern: /^beat-[0-9]{4}(?:-[a-z0-9_-]+)?$/, max: 120 });
  if (context.action !== action) fail("ANIMATION_CONTEXT_MISMATCH", "Scheduler action does not match the fixed adapter");
  requireString(context.actor_binding_id, "animation context.actor_binding_id", { pattern: SAFE_ID_RE, max: 120 });
  if (definition.target_policy === "required" && context.target_binding_id === null) fail("ANIMATION_TARGET_REQUIRED", `Action '${action}' requires a verified target`);
  if (definition.target_policy === "forbidden" && context.target_binding_id !== null) fail("ANIMATION_TARGET_FORBIDDEN", `Action '${action}' forbids a target`);
  if (context.target_binding_id !== null) requireString(context.target_binding_id, "animation context.target_binding_id", { pattern: SAFE_ID_RE, max: 120 });
  if (!Number.isInteger(context.attempt) || context.attempt < 1 || context.attempt > 100 || !Number.isFinite(context.planned_sec) || context.planned_sec < 0) fail("ANIMATION_CONTEXT_INVALID", "Animation attempt/timing is invalid");
  const actor = preflight.actors.find((entry) => entry.binding_id === context.actor_binding_id);
  const target = context.target_binding_id === null ? null : preflight.targets.find((entry) => entry.binding_id === context.target_binding_id);
  if (!actor || !actor.ready || !definition.actor_capabilities.every((capability) => actor.capabilities.includes(capability))) fail("ANIMATION_ACTOR_CAPABILITY_BLOCKED", `Actor cannot execute '${action}'`, { status: 409 });
  if (definition.target_policy === "required" && (!target || !target.ready || !definition.target_capabilities.every((capability) => target.capabilities.includes(capability)) || !definition.anchor_kinds.every((anchor) => target.anchor_kinds.includes(anchor)))) {
    fail("ANIMATION_TARGET_CAPABILITY_BLOCKED", `Target cannot execute '${action}' with its required IK/contact anchors`, { status: 409 });
  }
  return validateActionParameters(action, context.parameters);
}

function makeLifecycle({ broker, actionProfile, preflight, recorder, activeStates }) {
  const action = actionProfile.action;
  const bridgeActionId = actionProfile.bridge_action_id;

  async function precondition(context) {
    const parameters = validateLifecycleContext(action, context, preflight);
    const key = stateKey(context);
    if (activeStates.has(key)) fail("ANIMATION_EVENT_ALREADY_ACTIVE", `Animation event '${context.event_id}' is already active`, { status: 409 });
    const request = {
      schema: SNAPSHOT_REQUEST_SCHEMA,
      preflight_id: preflight.preflight_id,
      run_id: context.run_id,
      event_id: context.event_id,
      action,
      actor_binding_id: context.actor_binding_id,
      target_binding_id: context.target_binding_id,
    };
    const response = validateSnapshotResponse(request, await callBroker(broker, "snapshotAnimationState", request, context.signal, "ANIMATION_SNAPSHOT_UNAVAILABLE", "Animation state snapshot is unavailable"));
    const state = { snapshot: response, handle: null, startAttempted: false, stopped: false, parameters };
    activeStates.set(key, state);
    await recorder.capture({ phase: "before", context, snapshot: response });
    return { ok: true, snapshot_id: response.snapshot_id, state_digest: response.state_digest };
  }

  async function execute(context) {
    const key = stateKey(context);
    const state = activeStates.get(key);
    if (!state) fail("ANIMATION_SNAPSHOT_REQUIRED", "Animation action cannot start without its pre-action snapshot", { status: 409 });
    const request = {
      schema: START_REQUEST_SCHEMA,
      preflight_id: preflight.preflight_id,
      run_id: context.run_id,
      event_id: context.event_id,
      bridge_action_id: bridgeActionId,
      actor_binding_id: context.actor_binding_id,
      target_binding_id: context.target_binding_id,
      parameters: state.parameters,
    };
    state.startAttempted = true;
    const response = validateStartResponse(request, await callBroker(broker, "startAnimationAction", request, context.signal, "ANIMATION_START_UNAVAILABLE", `Animation action '${action}' could not start`));
    state.handle = response.action_handle;
    return { action_handle: response.action_handle, started_engine_time: response.engine_time };
  }

  async function completion(context) {
    const state = activeStates.get(stateKey(context));
    if (!state || !state.handle) fail("ANIMATION_HANDLE_REQUIRED", "Animation completion cannot be observed before start", { status: 409 });
    const request = {
      schema: WAIT_REQUEST_SCHEMA,
      preflight_id: preflight.preflight_id,
      run_id: context.run_id,
      event_id: context.event_id,
      action_handle: state.handle,
    };
    const response = validateWaitResponse(request, await callBroker(broker, "waitAnimationAction", request, context.signal, "ANIMATION_COMPLETION_UNAVAILABLE", `Animation action '${action}' completion is unavailable`), actionProfile.completion_signal);
    const checkpoint = await recorder.capture({ phase: "after", context, snapshot: state.snapshot, actionHandle: state.handle });
    return {
      completed: true,
      completion_signal: response.completion_signal,
      completed_engine_time: response.engine_time,
      broker_evidence_ids: response.evidence_ids,
      checkpoint_evidence_ids: checkpoint.evidence.map((entry) => entry.evidence_id),
    };
  }

  async function stopActive(context, reason) {
    const state = activeStates.get(stateKey(context));
    if (!state || !state.handle || state.stopped) return { stopped: false, reason: "not_active" };
    const request = {
      schema: STOP_REQUEST_SCHEMA,
      preflight_id: preflight.preflight_id,
      run_id: context.run_id,
      event_id: context.event_id,
      action_handle: state.handle,
      reason,
    };
    const response = validateStopResponse(request, await callBroker(broker, "stopAnimationAction", request, context.signal, "ANIMATION_STOP_UNAVAILABLE", `Animation action '${action}' could not be stopped`));
    state.stopped = true;
    return { stopped: true, status: response.status, engine_time: response.engine_time };
  }

  function timeout(context) {
    return stopActive(context, "timeout");
  }

  function cancel(context) {
    return stopActive(context, "cancel");
  }

  async function cleanup(context) {
    const key = stateKey(context);
    const state = activeStates.get(key);
    if (!state) return { cleaned: true, disposition: "not_started" };
    const failures = [];
    let released = false;
    let restored = false;
    try {
      const shouldRestore = CLEANUP_ROLLBACK_REASONS.has(context.reason);
      if (shouldRestore && state.startAttempted && !state.handle) {
        failures.push(new VistaAnimationRuntimeError(
          "ANIMATION_START_STATE_UNCERTAIN",
          `Animation action '${action}' may have started without a verified handle`,
          { status: 502 },
        ));
      }
      if (shouldRestore && state.handle && !state.stopped) {
        try {
          await stopActive(context, `cleanup_${context.reason}`);
        } catch (error) {
          failures.push(error);
        }
      }
      if (state.handle) {
        const releaseRequest = {
          schema: RELEASE_REQUEST_SCHEMA,
          preflight_id: preflight.preflight_id,
          run_id: context.run_id,
          event_id: context.event_id,
          action_handle: state.handle,
        };
        try {
          validateReleaseResponse(releaseRequest, await callBroker(broker, "releaseAnimationAction", releaseRequest, context.signal, "ANIMATION_RELEASE_UNAVAILABLE", `Animation action '${action}' transient controls could not be released`));
          released = true;
        } catch (error) {
          failures.push(error);
        }
      }
      if (shouldRestore) {
        const restoreRequest = {
          schema: RESTORE_REQUEST_SCHEMA,
          preflight_id: preflight.preflight_id,
          run_id: context.run_id,
          event_id: context.event_id,
          snapshot_id: state.snapshot.snapshot_id,
          state_digest: state.snapshot.state_digest,
        };
        try {
          validateRestoreResponse(restoreRequest, await callBroker(broker, "restoreAnimationState", restoreRequest, context.signal, "ANIMATION_RESTORE_UNAVAILABLE", `Animation action '${action}' rollback could not restore its snapshot`));
          restored = true;
          await recorder.capture({ phase: "rollback", context, snapshot: state.snapshot, actionHandle: state.handle });
        } catch (error) {
          failures.push(error);
        }
      }
    } finally {
      activeStates.delete(key);
    }
    if (failures.length) fail("ANIMATION_CLEANUP_INCOMPLETE", `Animation action '${action}' cleanup was incomplete`, { status: 502, details: { failures: failures.map((error) => error.code || error.message) } });
    return { cleaned: true, released, restored };
  }

  return { precondition, execute, completion, timeout, cancel, cleanup };
}

function validateBroker(broker) {
  if (!broker || typeof broker !== "object") fail("ANIMATION_RUNTIME_CONFIG_INVALID", "Animation broker must be injected", { status: 500 });
  const methods = [
    "preflightAnimation",
    "snapshotAnimationState",
    "startAnimationAction",
    "waitAnimationAction",
    "stopAnimationAction",
    "releaseAnimationAction",
    "restoreAnimationState",
  ];
  for (const method of methods) {
    if (typeof broker[method] !== "function") fail("ANIMATION_RUNTIME_CONFIG_INVALID", `Animation broker.${method} must be a function`, { status: 500 });
  }
  return broker;
}

function createVistaAnimationRuntime(options = {}) {
  exactKeys(options, ["broker", "contentProfile", "evidenceHooks", "clock", "fps"], ["broker", "contentProfile", "evidenceHooks"], "animation runtime options", "ANIMATION_RUNTIME_CONFIG_INVALID");
  const broker = validateBroker(options.broker);
  const contentProfile = validateContentProfile(options.contentProfile);
  const agentActionPolicy = isVistaLegacyAgentActionProfile(contentProfile)
    ? createVistaAnimationAgentActionPolicy({ contentProfile })
    : null;
  const hooks = validateEvidenceHooks(options.evidenceHooks);
  const clock = options.clock === undefined ? (() => new Date().toISOString()) : options.clock;
  if (typeof clock !== "function") fail("ANIMATION_RUNTIME_CONFIG_INVALID", "Animation runtime clock must be a function", { status: 500 });
  const fps = options.fps === undefined ? DEFAULT_FPS : options.fps;
  if (!Number.isInteger(fps) || fps < 1 || fps > 240) fail("ANIMATION_RUNTIME_CONFIG_INVALID", "Animation runtime fps must be an integer in [1, 240]", { status: 500 });
  const recorder = createEvidenceRecorder({ hooks, clock, fps });
  const activeStates = new Map();

  async function preflight(request) {
    exactKeys(request, ["sceneRevision", "bindings", "requestedActions", "signal"], ["sceneRevision", "bindings", "requestedActions"], "animation runtime preflight request");
    const bindings = validateBindings(request.bindings);
    if (agentActionPolicy) {
      agentActionPolicy.authorize({
        schema: AGENT_ACTION_AUTHORIZATION_SCHEMA,
        profile_id: contentProfile.profile_id,
        profile_revision: contentProfile.revision,
        content_digest: contentProfile.content_digest,
        requested_actions: request.requestedActions,
      });
    }
    const outbound = makeAnimationPreflightRequest({
      contentProfile,
      sceneRevision: request.sceneRevision,
      bindings,
      requestedActions: request.requestedActions,
    });
    const raw = await callBroker(broker, "preflightAnimation", outbound, request.signal, "ANIMATION_PREFLIGHT_UNAVAILABLE", "Animation runtime preflight is unavailable");
    const response = validateAnimationPreflightResponse(outbound, raw);
    const artifact = buildAnimationPreflightArtifact({
      request: outbound,
      response,
      contentProfile,
      bindings,
      registryBinding: agentActionPolicy ? agentActionPolicy.registry_binding : null,
    });
    validateAnimationPreflightArtifact(artifact);
    const profiles = new Map(contentProfile.actions.map((entry) => [entry.action, entry]));
    const capabilityRegistry = agentActionPolicy
      ? agentActionPolicy.createCapabilityRegistry({
        artifact,
        lifecycleFactory: (action) => makeLifecycle({
          broker,
          actionProfile: profiles.get(action),
          preflight: artifact,
          recorder,
          activeStates,
        }),
      })
      : Object.freeze({
        schema: CAPABILITY_REGISTRY_SCHEMA,
        revision: artifact.registry_revision,
        adapters: Object.freeze(artifact.supported_actions.map((action) => createAdapterDescriptor(
          profiles.get(action),
          makeLifecycle({ broker, actionProfile: profiles.get(action), preflight: artifact, recorder, activeStates }),
        ))),
      });
    return Object.freeze({ artifact, capabilityRegistry });
  }

  function compileProgram(timeline, preflightArtifact, programOptions = {}) {
    exactKeys(programOptions, ["fps"], [], "animation program options", "ANIMATION_PROGRAM_INPUT_INVALID");
    if (programOptions.fps !== undefined && programOptions.fps !== fps) fail("ANIMATION_PROGRAM_FPS_MISMATCH", "Animation program must use the runtime evidence FPS", { status: 409 });
    return compileVistaAnimationProgram(timeline, preflightArtifact, { fps });
  }

  function finalizeEvidence({ program, run }) {
    return recorder.finalize({ program, run });
  }

  function discardEvidence(runId) {
    requireString(runId, "discardEvidence.runId", { pattern: OPAQUE_ID_RE, max: 160 });
    recorder.discard(runId);
  }

  return Object.freeze({
    preflight,
    compileProgram,
    finalizeEvidence,
    discardEvidence,
    contentProfile,
    fps,
  });
}

module.exports = {
  ANIMATION_EVIDENCE_SCHEMA,
  EVIDENCE_HOOKS,
  RELEASE_REQUEST_SCHEMA,
  RELEASE_RESPONSE_SCHEMA,
  RESTORE_REQUEST_SCHEMA,
  RESTORE_RESPONSE_SCHEMA,
  SNAPSHOT_REQUEST_SCHEMA,
  SNAPSHOT_RESPONSE_SCHEMA,
  START_REQUEST_SCHEMA,
  START_RESPONSE_SCHEMA,
  STOP_REQUEST_SCHEMA,
  STOP_RESPONSE_SCHEMA,
  WAIT_REQUEST_SCHEMA,
  WAIT_RESPONSE_SCHEMA,
  VistaAnimationRuntimeError,
  createVistaAnimationRuntime,
  validateAnimationEvidenceManifest,
};
