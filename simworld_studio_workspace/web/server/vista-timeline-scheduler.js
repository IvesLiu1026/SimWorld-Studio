"use strict";

const crypto = require("node:crypto");
const { performance } = require("node:perf_hooks");

const {
  TIMELINE_RUN_SCHEMA,
  validateCapabilityRegistry,
  validateCompiledTimeline,
} = require("./vista-timeline-compiler");

const TERMINAL_STATES = new Set(["completed", "failed", "cancelled"]);
const OPAQUE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,159}$/;
const RUN_ID_RE = /^vtr-[a-z0-9_-]{8,120}$/;
const DEFAULT_MAX_QUEUE_SIZE = 256;
const DEFAULT_MAX_CONCURRENT_RUNS = 8;
const DEFAULT_MAX_RETAINED_RUNS = 128;
const DEFAULT_HOOK_TIMEOUT_MS = 5000;
const RUN_STATES = new Set(["queued", "preflighting", "ready", "running", "pausing", "paused", "stopping", "completed", "failed", "cancelled"]);
const RUN_EVENT_STATES = new Set(["pending", "dispatched", "running", "completed", "skipped", "failed", "cancelled", "timed_out"]);
const EVENT_CLEANUP_STATES = new Set(["not_required", "pending", "completed", "failed"]);
const RUN_CLEANUP_STATES = new Set(["not_required", "pending", "completed", "partial", "failed"]);
const RUN_TRANSITIONS = Object.freeze({
  ready: Object.freeze(["running", "stopping"]),
  running: Object.freeze(["stopping", "completed", "failed", "cancelled"]),
  stopping: Object.freeze(["cancelled", "failed"]),
  completed: Object.freeze([]),
  failed: Object.freeze([]),
  cancelled: Object.freeze([]),
});

class VistaTimelineSchedulerError extends Error {
  constructor(code, message, { status = 400, retryable = false, details = {} } = {}) {
    super(message);
    this.name = "VistaTimelineSchedulerError";
    this.code = code;
    this.status = status;
    this.retryable = retryable;
    this.details = sanitizeDetails(details);
  }
}

class TimelineEventTimeoutError extends Error {
  constructor(eventId, timeoutMs) {
    super(`Event '${eventId}' exceeded its ${timeoutMs}ms adapter timeout`);
    this.name = "TimelineEventTimeoutError";
    this.code = "TIMELINE_EVENT_TIMEOUT";
    this.eventId = eventId;
    this.timeoutMs = timeoutMs;
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

function fail(code, message, options) {
  throw new VistaTimelineSchedulerError(code, message, options);
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function exactKeys(value, allowed, required, pointer, code = "TIMELINE_SCHEDULER_INPUT_INVALID") {
  if (!isPlainObject(value)) fail(code, `${pointer} must be an object`);
  const allowedSet = new Set(allowed);
  const unknown = Object.keys(value).filter((key) => !allowedSet.has(key));
  const missing = required.filter((key) => !Object.prototype.hasOwnProperty.call(value, key));
  if (unknown.length || missing.length) {
    fail(code, `${pointer} has an invalid shape`, { details: { pointer, unknown, missing } });
  }
}

function requireOpaqueId(value, pointer) {
  if (typeof value !== "string" || !OPAQUE_ID_RE.test(value)) {
    fail("TIMELINE_SCHEDULER_INPUT_INVALID", `${pointer} is invalid`, { details: { pointer } });
  }
  return value;
}

function requirePositiveInteger(value, pointer, max) {
  if (!Number.isInteger(value) || value < 1 || value > max) {
    fail("TIMELINE_SCHEDULER_CONFIG_INVALID", `${pointer} must be an integer between 1 and ${max}`);
  }
  return value;
}

function errorMessage(error) {
  if (!error) return "Unknown scheduler failure";
  if (typeof error === "string") return error.slice(0, 1000);
  return String(error.message || error.code || "Unknown scheduler failure").slice(0, 1000);
}

function makeAbortError(reason = "Timeline run aborted") {
  const error = new Error(typeof reason === "string" ? reason : "Timeline run aborted");
  error.name = "AbortError";
  error.code = "TIMELINE_RUN_ABORTED";
  return error;
}

function isAbortError(error) {
  return error && (error.name === "AbortError" || error.code === "ABORT_ERR" || error.code === "TIMELINE_RUN_ABORTED");
}

function abortableTimer(ms, signal) {
  if (signal && signal.aborted) return Promise.reject(makeAbortError(signal.reason));
  return new Promise((resolve, reject) => {
    const timer = setTimeout(finish, Math.max(0, ms));
    function cleanup() {
      if (signal) signal.removeEventListener("abort", onAbort);
    }
    function finish() {
      cleanup();
      resolve();
    }
    function onAbort() {
      clearTimeout(timer);
      cleanup();
      reject(makeAbortError(signal.reason));
    }
    if (signal) signal.addEventListener("abort", onAbort, { once: true });
  });
}

function createDefaultMonotonicClock() {
  return Object.freeze({
    now: () => performance.now(),
    delay: (ms, { signal } = {}) => abortableTimer(ms, signal),
    waitUntil(deadlineMs, { signal } = {}) {
      return abortableTimer(Math.max(0, deadlineMs - performance.now()), signal);
    },
  });
}

function validateClock(clock) {
  if (!clock || typeof clock.now !== "function" || typeof clock.waitUntil !== "function" || typeof clock.delay !== "function") {
    fail("TIMELINE_SCHEDULER_CONFIG_INVALID", "clock must provide now, waitUntil, and delay functions");
  }
  const now = clock.now();
  if (typeof now !== "number" || !Number.isFinite(now)) {
    fail("TIMELINE_SCHEDULER_CONFIG_INVALID", "clock.now() must return finite monotonic milliseconds");
  }
  return clock;
}

function validateSignal(signal, pointer) {
  if (signal === undefined || signal === null) return null;
  if (typeof signal.aborted !== "boolean" || typeof signal.addEventListener !== "function" || typeof signal.removeEventListener !== "function") {
    fail("TIMELINE_SCHEDULER_INPUT_INVALID", `${pointer} must be an AbortSignal`);
  }
  return signal;
}

function deepFreeze(value) {
  if (!value || typeof value !== "object" || Object.isFrozen(value)) return value;
  Object.freeze(value);
  for (const child of Object.values(value)) deepFreeze(child);
  return value;
}

function cloneArtifactValue(value, pointer = "result", depth = 0) {
  if (depth > 8) throw new Error(`${pointer} exceeds the artifact nesting limit`);
  if (value === undefined) return null;
  if (value === null || typeof value === "boolean" || typeof value === "string") return value;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (Array.isArray(value)) {
    if (value.length > 128) throw new Error(`${pointer} exceeds the artifact item limit`);
    return value.map((item, index) => cloneArtifactValue(item, `${pointer}[${index}]`, depth + 1));
  }
  if (!isPlainObject(value)) throw new Error(`${pointer} is not JSON-safe`);
  const keys = Object.keys(value);
  if (keys.length > 128) throw new Error(`${pointer} exceeds the artifact property limit`);
  const output = {};
  for (const key of keys.sort()) {
    if (["__proto__", "constructor", "prototype"].includes(key)) throw new Error(`${pointer} contains an unsafe key`);
    output[key] = cloneArtifactValue(value[key], `${pointer}.${key}`, depth + 1);
  }
  return output;
}

function normalizeHookResult(value, pointer) {
  const cloned = cloneArtifactValue(value, pointer);
  if (cloned === null) return {};
  if (isPlainObject(cloned)) return cloned;
  return { value: cloned };
}

function validateTimelineRunArtifact(artifact) {
  const keys = [
    "schema", "run_id", "timeline_id", "scene_revision", "correlation_id", "owner_id", "session_id", "slot_id",
    "duration_sec", "clock", "state", "created_at", "started_at", "ended_at", "events", "checkpoints", "cleanup",
  ];
  exactKeys(artifact, keys, keys, "timeline run artifact", "TIMELINE_RUN_ARTIFACT_INVALID");
  if (artifact.schema !== TIMELINE_RUN_SCHEMA || !RUN_ID_RE.test(artifact.run_id) || !/^vtl-[a-f0-9]{24}$/.test(artifact.timeline_id)) {
    fail("TIMELINE_RUN_ARTIFACT_INVALID", "Timeline run identity is invalid", { status: 500 });
  }
  for (const field of ["scene_revision", "correlation_id", "owner_id", "session_id", "slot_id"]) {
    if (typeof artifact[field] !== "string" || !artifact[field].length || artifact[field].length > 256) {
      fail("TIMELINE_RUN_ARTIFACT_INVALID", `Timeline run ${field} is invalid`, { status: 500 });
    }
  }
  if (typeof artifact.duration_sec !== "number" || !Number.isFinite(artifact.duration_sec) || artifact.duration_sec <= 0 || artifact.duration_sec > 3600) {
    fail("TIMELINE_RUN_ARTIFACT_INVALID", "Timeline run duration is invalid", { status: 500 });
  }
  if (artifact.clock !== "server_monotonic" || !RUN_STATES.has(artifact.state)) {
    fail("TIMELINE_RUN_ARTIFACT_INVALID", "Timeline run clock or state is invalid", { status: 500 });
  }
  for (const field of ["created_at", "started_at", "ended_at"]) {
    if (artifact[field] !== null && (typeof artifact[field] !== "string" || Number.isNaN(Date.parse(artifact[field])))) {
      fail("TIMELINE_RUN_ARTIFACT_INVALID", `Timeline run ${field} is invalid`, { status: 500 });
    }
  }
  if (artifact.created_at === null || !Array.isArray(artifact.events) || artifact.events.length === 0 || !Array.isArray(artifact.checkpoints)) {
    fail("TIMELINE_RUN_ARTIFACT_INVALID", "Timeline run evidence collections are invalid", { status: 500 });
  }
  const eventIds = new Set();
  let previousPlanned = -1;
  for (const event of artifact.events) {
    const eventKeys = [
      "event_id", "adapter_id", "planned_sec", "actual_sec", "engine_time", "drift_ms", "state", "attempt",
      "started_at", "ended_at", "result", "error", "cleanup_state",
    ];
    exactKeys(event, eventKeys, eventKeys, "timeline run event", "TIMELINE_RUN_ARTIFACT_INVALID");
    if (!/^beat-[0-9]{4}(?:-[a-z0-9_-]+)?$/.test(event.event_id) || eventIds.has(event.event_id)) {
      fail("TIMELINE_RUN_ARTIFACT_INVALID", "Timeline run event ids are invalid", { status: 500 });
    }
    eventIds.add(event.event_id);
    if (typeof event.planned_sec !== "number" || !Number.isFinite(event.planned_sec) || event.planned_sec < previousPlanned || event.planned_sec < 0 || event.planned_sec > artifact.duration_sec) {
      fail("TIMELINE_RUN_ARTIFACT_INVALID", "Timeline run event planned time is invalid", { status: 500 });
    }
    previousPlanned = event.planned_sec;
    for (const field of ["actual_sec", "engine_time", "drift_ms"]) {
      if (event[field] !== null && (typeof event[field] !== "number" || !Number.isFinite(event[field]))) {
        fail("TIMELINE_RUN_ARTIFACT_INVALID", `Timeline run event ${field} is invalid`, { status: 500 });
      }
    }
    if (!RUN_EVENT_STATES.has(event.state) || !Number.isInteger(event.attempt) || event.attempt < 0 || event.attempt > 100) {
      fail("TIMELINE_RUN_ARTIFACT_INVALID", "Timeline run event lifecycle is invalid", { status: 500 });
    }
    if (event.adapter_id !== null && (typeof event.adapter_id !== "string" || !/^[a-z][a-z0-9_-]{0,119}$/.test(event.adapter_id))) {
      fail("TIMELINE_RUN_ARTIFACT_INVALID", "Timeline run adapter reference is invalid", { status: 500 });
    }
    for (const field of ["started_at", "ended_at"]) {
      if (event[field] !== null && (typeof event[field] !== "string" || Number.isNaN(Date.parse(event[field])))) {
        fail("TIMELINE_RUN_ARTIFACT_INVALID", `Timeline run event ${field} is invalid`, { status: 500 });
      }
    }
    if (event.result !== null && !isPlainObject(event.result)) fail("TIMELINE_RUN_ARTIFACT_INVALID", "Timeline run event result is invalid", { status: 500 });
    if (event.error !== null && (typeof event.error !== "string" || !event.error.length)) fail("TIMELINE_RUN_ARTIFACT_INVALID", "Timeline run event error is invalid", { status: 500 });
    if (!EVENT_CLEANUP_STATES.has(event.cleanup_state)) fail("TIMELINE_RUN_ARTIFACT_INVALID", "Timeline run event cleanup state is invalid", { status: 500 });
  }
  exactKeys(artifact.cleanup, ["state", "pending_items", "confirmed_stopped", "ended_pie", "error"], ["state", "pending_items", "confirmed_stopped", "ended_pie", "error"], "timeline run cleanup", "TIMELINE_RUN_ARTIFACT_INVALID");
  if (!RUN_CLEANUP_STATES.has(artifact.cleanup.state) || !Array.isArray(artifact.cleanup.pending_items) || typeof artifact.cleanup.confirmed_stopped !== "boolean" || typeof artifact.cleanup.ended_pie !== "boolean") {
    fail("TIMELINE_RUN_ARTIFACT_INVALID", "Timeline run cleanup evidence is invalid", { status: 500 });
  }
  if (artifact.cleanup.error !== null && (typeof artifact.cleanup.error !== "string" || !artifact.cleanup.error.length)) {
    fail("TIMELINE_RUN_ARTIFACT_INVALID", "Timeline run cleanup error is invalid", { status: 500 });
  }
  return artifact;
}

function snapshot(run) {
  return deepFreeze(cloneArtifactValue(validateTimelineRunArtifact(run.artifact), "run"));
}

function isTerminal(run) {
  return TERMINAL_STATES.has(run.artifact.state);
}

function transitionRun(run, nextState) {
  const current = run.artifact.state;
  if (current === nextState) return;
  const allowed = RUN_TRANSITIONS[current];
  if (!allowed || !allowed.includes(nextState)) {
    fail("TIMELINE_FSM_TRANSITION_INVALID", `Timeline run cannot transition from '${current}' to '${nextState}'`, {
      status: 500,
      details: { current, next: nextState },
    });
  }
  run.artifact.state = nextState;
}

function validateWallClock(wallClock) {
  if (typeof wallClock !== "function") fail("TIMELINE_SCHEDULER_CONFIG_INVALID", "wallClock must be a function");
  const value = wallClock();
  if (typeof value !== "string" || Number.isNaN(Date.parse(value))) {
    fail("TIMELINE_SCHEDULER_CONFIG_INVALID", "wallClock must return an ISO date-time string");
  }
  return wallClock;
}

function createVistaTimelineScheduler(options = {}) {
  exactKeys(
    options,
    [
      "capabilityRegistry", "engineTimeSampler", "clock", "wallClock", "idFactory",
      "maxQueueSize", "maxConcurrentRuns", "maxRetainedRuns", "hookTimeoutMs",
    ],
    ["capabilityRegistry", "engineTimeSampler"],
    "scheduler options",
    "TIMELINE_SCHEDULER_CONFIG_INVALID",
  );

  let registry;
  try {
    registry = validateCapabilityRegistry(options.capabilityRegistry);
  } catch (error) {
    fail("TIMELINE_SCHEDULER_CONFIG_INVALID", "Capability registry is invalid", {
      details: { cause: error && error.code },
    });
  }
  if (typeof options.engineTimeSampler !== "function") {
    fail("TIMELINE_SCHEDULER_CONFIG_INVALID", "engineTimeSampler must be injected");
  }
  const engineTimeSampler = options.engineTimeSampler;
  const clock = validateClock(options.clock === undefined ? createDefaultMonotonicClock() : options.clock);
  const wallClock = validateWallClock(options.wallClock === undefined ? (() => new Date().toISOString()) : options.wallClock);
  const idFactory = options.idFactory === undefined ? (() => crypto.randomBytes(12).toString("hex")) : options.idFactory;
  if (typeof idFactory !== "function") fail("TIMELINE_SCHEDULER_CONFIG_INVALID", "idFactory must be a function");
  const maxQueueSize = requirePositiveInteger(options.maxQueueSize === undefined ? DEFAULT_MAX_QUEUE_SIZE : options.maxQueueSize, "maxQueueSize", 1000);
  const maxConcurrentRuns = requirePositiveInteger(options.maxConcurrentRuns === undefined ? DEFAULT_MAX_CONCURRENT_RUNS : options.maxConcurrentRuns, "maxConcurrentRuns", 256);
  const maxRetainedRuns = requirePositiveInteger(options.maxRetainedRuns === undefined ? DEFAULT_MAX_RETAINED_RUNS : options.maxRetainedRuns, "maxRetainedRuns", 10_000);
  const hookTimeoutMs = requirePositiveInteger(options.hookTimeoutMs === undefined ? DEFAULT_HOOK_TIMEOUT_MS : options.hookTimeoutMs, "hookTimeoutMs", 600_000);
  const adapters = new Map(registry.adapters.map((adapter) => [adapter.adapter_id, adapter]));
  const runs = new Map();

  function nowIso() {
    const value = wallClock();
    if (typeof value !== "string" || Number.isNaN(Date.parse(value))) {
      fail("TIMELINE_SCHEDULER_CLOCK_INVALID", "wallClock returned an invalid date-time", { status: 500 });
    }
    return value;
  }

  function nextRunId() {
    const suffix = idFactory();
    if (typeof suffix !== "string" || !/^[a-z0-9_-]{8,120}$/.test(suffix)) {
      fail("TIMELINE_SCHEDULER_ID_INVALID", "idFactory returned an invalid run id suffix", { status: 500 });
    }
    const runId = `vtr-${suffix}`;
    if (!RUN_ID_RE.test(runId) || runs.has(runId)) {
      fail("TIMELINE_SCHEDULER_ID_INVALID", "idFactory returned a duplicate or invalid run id", { status: 500 });
    }
    return runId;
  }

  function activeRunCount() {
    let count = 0;
    for (const run of runs.values()) if (!isTerminal(run)) count += 1;
    return count;
  }

  function pruneRetainedRuns() {
    if (runs.size < maxRetainedRuns) return;
    for (const [runId, run] of runs) {
      if (isTerminal(run)) {
        runs.delete(runId);
        if (runs.size < maxRetainedRuns) return;
      }
    }
    if (runs.size >= maxRetainedRuns) {
      fail("TIMELINE_RUN_LIMIT_EXCEEDED", "No retained run capacity is available", { status: 429, retryable: true });
    }
  }

  function validateTimelineForStart(timeline) {
    try {
      validateCompiledTimeline(timeline);
    } catch (error) {
      fail("TIMELINE_PREFLIGHT_INVALID", "Timeline preflight artifact is invalid", {
        status: 422,
        details: { cause: error && error.code },
      });
    }
    if (!timeline.start_allowed) {
      fail("TIMELINE_START_BLOCKED", "Timeline preflight does not allow execution", { status: 409 });
    }
    if (timeline.registry_revision !== registry.revision) {
      fail("TIMELINE_REGISTRY_MISMATCH", "Timeline and scheduler capability revisions differ", { status: 409 });
    }
    if (timeline.events.length > maxQueueSize) {
      fail("TIMELINE_QUEUE_LIMIT_EXCEEDED", "Timeline exceeds the bounded scheduler queue", {
        status: 422,
        details: { count: timeline.events.length, max: maxQueueSize },
      });
    }
    for (const event of timeline.events) {
      if (event.disposition !== "execute") continue;
      const adapter = event.adapter && adapters.get(event.adapter.adapter_id);
      if (!adapter || adapter.version !== event.adapter.version || adapter.timeout_ms !== event.adapter.timeout_ms) {
        fail("TIMELINE_ADAPTER_NOT_REGISTERED", "Compiled event adapter is not available at the verified version", {
          status: 409,
          details: { event_id: event.event_id, adapter_id: event.adapter && event.adapter.adapter_id },
        });
      }
    }
    return timeline;
  }

  function makeRunEvent(event, createdAt) {
    if (event.disposition === "skip") {
      return {
        event_id: event.event_id,
        adapter_id: null,
        planned_sec: event.at_sec,
        actual_sec: null,
        engine_time: null,
        drift_ms: null,
        state: "skipped",
        attempt: 0,
        started_at: null,
        ended_at: createdAt,
        result: { reason: "preflight_skip", issue_ids: [...event.issue_ids] },
        error: null,
        cleanup_state: "not_required",
      };
    }
    return {
      event_id: event.event_id,
      adapter_id: event.adapter.adapter_id,
      planned_sec: event.at_sec,
      actual_sec: null,
      engine_time: null,
      drift_ms: null,
      state: "pending",
      attempt: 0,
      started_at: null,
      ended_at: null,
      result: null,
      error: null,
      cleanup_state: "not_required",
    };
  }

  function buildArtifact(request, timeline, runId) {
    const createdAt = nowIso();
    return {
      schema: TIMELINE_RUN_SCHEMA,
      run_id: runId,
      timeline_id: timeline.timeline_id,
      scene_revision: timeline.scene_revision,
      correlation_id: request.correlationId,
      owner_id: request.ownerId,
      session_id: request.sessionId,
      slot_id: request.slotId,
      duration_sec: timeline.duration_sec,
      clock: "server_monotonic",
      state: "ready",
      created_at: createdAt,
      started_at: null,
      ended_at: null,
      events: timeline.events.map((event) => makeRunEvent(event, createdAt)),
      checkpoints: [],
      cleanup: {
        state: "not_required",
        pending_items: [],
        confirmed_stopped: false,
        ended_pie: false,
        error: null,
      },
    };
  }

  function accessRun(request, pointer) {
    exactKeys(request, ["runId", "ownerId", "sessionId"], ["runId", "ownerId", "sessionId"], pointer);
    const runId = requireOpaqueId(request.runId, `${pointer}.runId`);
    const ownerId = requireOpaqueId(request.ownerId, `${pointer}.ownerId`);
    const sessionId = requireOpaqueId(request.sessionId, `${pointer}.sessionId`);
    const run = runs.get(runId);
    if (!run) fail("TIMELINE_RUN_NOT_FOUND", "Timeline run was not found", { status: 404 });
    if (run.artifact.owner_id !== ownerId || run.artifact.session_id !== sessionId) {
      fail("TIMELINE_RUN_ACCESS_DENIED", "Timeline run belongs to another owner or session", { status: 403 });
    }
    return run;
  }

  function eventContext(run, compiledEvent, adapterEvent, signal, reason = null) {
    return Object.freeze({
      run_id: run.artifact.run_id,
      timeline_id: run.timeline.timeline_id,
      scene_revision: run.timeline.scene_revision,
      event_id: compiledEvent.event_id,
      action: compiledEvent.action,
      actor_id: compiledEvent.actor_id,
      target_id: compiledEvent.target_id,
      actor_binding_id: compiledEvent.actor_binding_id,
      target_binding_id: compiledEvent.target_binding_id,
      parameters: compiledEvent.parameters,
      planned_sec: compiledEvent.at_sec,
      attempt: adapterEvent.attempt,
      signal,
      reason,
    });
  }

  async function raceWithDelay(operation, timeoutMs, signal) {
    const timerController = new AbortController();
    const onAbort = () => timerController.abort(signal.reason);
    if (signal) {
      if (signal.aborted) timerController.abort(signal.reason);
      else signal.addEventListener("abort", onAbort, { once: true });
    }
    const operationPromise = Promise.resolve().then(operation).then((value) => ({ kind: "value", value }));
    const timeoutPromise = Promise.resolve()
      .then(() => clock.delay(timeoutMs, { signal: timerController.signal }))
      .then(() => ({ kind: "timeout" }));
    try {
      const outcome = await Promise.race([operationPromise, timeoutPromise]);
      if (outcome.kind === "timeout") return { timedOut: true, value: undefined };
      return { timedOut: false, value: outcome.value };
    } finally {
      timerController.abort("timer complete");
      if (signal) signal.removeEventListener("abort", onAbort);
    }
  }

  async function callBoundedHook(handler, context, label) {
    const controller = new AbortController();
    const outcome = await raceWithDelay(() => handler(Object.freeze({ ...context, signal: controller.signal })), hookTimeoutMs, null);
    if (outcome.timedOut) {
      controller.abort(`${label} timeout`);
      throw new Error(`${label} exceeded ${hookTimeoutMs}ms`);
    }
    controller.abort(`${label} complete`);
    return outcome.value;
  }

  function validatePrecondition(value, eventId) {
    if (value === true) return;
    if (isPlainObject(value) && value.ok === true) return;
    const reason = isPlainObject(value) && typeof value.reason === "string" ? value.reason : "precondition rejected";
    throw new Error(`Event '${eventId}' ${reason}`);
  }

  function validateCompletion(value, eventId) {
    if (value === true) return {};
    if (isPlainObject(value) && value.completed === true) return normalizeHookResult(value, "completion");
    throw new Error(`Event '${eventId}' did not produce a completion signal`);
  }

  async function sampleEngineTime(run, compiledEvent) {
    const samplerController = new AbortController();
    const onRunAbort = () => samplerController.abort(run.controller.signal.reason);
    if (run.controller.signal.aborted) samplerController.abort(run.controller.signal.reason);
    else run.controller.signal.addEventListener("abort", onRunAbort, { once: true });
    try {
      const outcome = await raceWithDelay(() => engineTimeSampler(Object.freeze({
        run_id: run.artifact.run_id,
        timeline_id: run.timeline.timeline_id,
        event_id: compiledEvent.event_id,
        signal: samplerController.signal,
      })), hookTimeoutMs, run.controller.signal);
      if (outcome.timedOut) {
        samplerController.abort(`engine time sampler timed out for '${compiledEvent.event_id}'`);
        throw new Error(`Engine time sampler timed out for '${compiledEvent.event_id}'`);
      }
      const value = outcome.value;
      if (typeof value !== "number" || !Number.isFinite(value)) {
        throw new Error(`Engine time sampler returned an invalid value for '${compiledEvent.event_id}'`);
      }
      return value;
    } finally {
      samplerController.abort(`engine time sample '${compiledEvent.event_id}' finalized`);
      run.controller.signal.removeEventListener("abort", onRunAbort);
    }
  }

  async function cleanupAdapter(run, compiledEvent, adapterEvent, adapter, reason) {
    if (adapterEvent.cleanup_state === "completed" || adapterEvent.cleanup_state === "failed") return;
    adapterEvent.cleanup_state = "pending";
    const base = eventContext(run, compiledEvent, adapterEvent, run.controller.signal, reason);
    try {
      await callBoundedHook(adapter.implementation.cleanup, base, `cleanup '${compiledEvent.event_id}'`);
      adapterEvent.cleanup_state = "completed";
    } catch (error) {
      adapterEvent.cleanup_state = "failed";
      adapterEvent.error = [adapterEvent.error, `Cleanup failed: ${errorMessage(error)}`].filter(Boolean).join("; ").slice(0, 1000);
      run.cleanupFailures.add(compiledEvent.event_id);
    }
  }

  async function cancelAdapter(run, compiledEvent, adapterEvent, adapter, reason) {
    const base = eventContext(run, compiledEvent, adapterEvent, run.controller.signal, reason);
    try {
      await callBoundedHook(adapter.implementation.cancel, base, `cancel '${compiledEvent.event_id}'`);
    } catch (error) {
      adapterEvent.error = [adapterEvent.error, `Cancel failed: ${errorMessage(error)}`].filter(Boolean).join("; ").slice(0, 1000);
      run.cleanupFailures.add(compiledEvent.event_id);
    }
  }

  async function timeoutAdapter(run, compiledEvent, adapterEvent, adapter, error) {
    const base = eventContext(run, compiledEvent, adapterEvent, run.controller.signal, error.message);
    try {
      await callBoundedHook(adapter.implementation.timeout, base, `timeout '${compiledEvent.event_id}'`);
    } catch (hookError) {
      adapterEvent.error = [adapterEvent.error, `Timeout hook failed: ${errorMessage(hookError)}`].filter(Boolean).join("; ").slice(0, 1000);
      run.cleanupFailures.add(compiledEvent.event_id);
    }
  }

  async function executeEvent(run, compiledEvent, adapterEvent) {
    const adapter = adapters.get(compiledEvent.adapter.adapter_id);
    adapterEvent.state = "dispatched";
    adapterEvent.attempt += 1;
    adapterEvent.started_at = nowIso();
    const actualSec = Math.max(0, (clock.now() - run.startMonotonicMs) / 1000);
    adapterEvent.actual_sec = actualSec;
    adapterEvent.drift_ms = (actualSec - compiledEvent.at_sec) * 1000;
    try {
      adapterEvent.engine_time = await sampleEngineTime(run, compiledEvent);
    } catch (error) {
      if (run.controller.signal.aborted || run.stopRequested || isAbortError(error)) throw error;
      adapterEvent.state = "failed";
      adapterEvent.error = errorMessage(error);
      adapterEvent.ended_at = nowIso();
      throw error;
    }
    adapterEvent.state = "running";
    run.active = { compiledEvent, adapterEvent, adapter };
    const eventController = new AbortController();
    const onRunAbort = () => eventController.abort(run.controller.signal.reason);
    if (run.controller.signal.aborted) eventController.abort(run.controller.signal.reason);
    else run.controller.signal.addEventListener("abort", onRunAbort, { once: true });
    const context = eventContext(run, compiledEvent, adapterEvent, eventController.signal);

    try {
      const outcome = await raceWithDelay(async () => {
        const precondition = await adapter.implementation.precondition(context);
        validatePrecondition(precondition, compiledEvent.event_id);
        const execution = await adapter.implementation.execute(context);
        const completion = await adapter.implementation.completion(context);
        return {
          execute: normalizeHookResult(execution, "execute"),
          completion: validateCompletion(completion, compiledEvent.event_id),
        };
      }, adapter.timeout_ms, run.controller.signal);

      if (outcome.timedOut) {
        eventController.abort(`event '${compiledEvent.event_id}' timed out`);
        throw new TimelineEventTimeoutError(compiledEvent.event_id, adapter.timeout_ms);
      }
      adapterEvent.result = outcome.value;
      adapterEvent.state = "completed";
      adapterEvent.ended_at = nowIso();
      await cleanupAdapter(run, compiledEvent, adapterEvent, adapter, "completed");
      if (adapterEvent.cleanup_state === "failed") throw new Error(`Cleanup failed for '${compiledEvent.event_id}'`);
    } catch (error) {
      if (run.controller.signal.aborted || run.stopRequested || isAbortError(error)) {
        adapterEvent.state = "cancelled";
        adapterEvent.error = errorMessage(run.stopReason || error);
        adapterEvent.ended_at = nowIso();
        await cancelAdapter(run, compiledEvent, adapterEvent, adapter, run.stopReason || "run aborted");
        await cleanupAdapter(run, compiledEvent, adapterEvent, adapter, "cancelled");
        throw makeAbortError(run.stopReason || errorMessage(error));
      }
      if (error instanceof TimelineEventTimeoutError) {
        adapterEvent.state = "timed_out";
        adapterEvent.error = error.message;
        adapterEvent.ended_at = nowIso();
        await timeoutAdapter(run, compiledEvent, adapterEvent, adapter, error);
        await cleanupAdapter(run, compiledEvent, adapterEvent, adapter, "timed_out");
        throw error;
      }
      adapterEvent.state = "failed";
      adapterEvent.error = errorMessage(error);
      adapterEvent.ended_at = nowIso();
      await cleanupAdapter(run, compiledEvent, adapterEvent, adapter, "failed");
      throw error;
    } finally {
      eventController.abort(`event '${compiledEvent.event_id}' finalized`);
      run.controller.signal.removeEventListener("abort", onRunAbort);
      run.active = null;
    }
  }

  function cancelPendingEvents(run, reason) {
    const endedAt = nowIso();
    for (const event of run.artifact.events) {
      if (event.state !== "pending" && event.state !== "dispatched") continue;
      event.state = "cancelled";
      event.error = String(reason || "run ended before dispatch").slice(0, 1000);
      event.ended_at = endedAt;
    }
  }

  function finalizeCleanup(run) {
    const pendingItems = [...run.cleanupFailures].sort();
    run.artifact.cleanup.pending_items = pendingItems;
    run.artifact.cleanup.ended_pie = false;
    run.artifact.cleanup.confirmed_stopped = pendingItems.length === 0 && run.artifact.events.every((event) => !new Set(["pending", "dispatched", "running"]).has(event.state));
    if (pendingItems.length) {
      run.artifact.cleanup.state = "partial";
      run.artifact.cleanup.error = `Adapter cleanup incomplete for: ${pendingItems.join(", ")}`.slice(0, 4000);
    } else {
      run.artifact.cleanup.state = "completed";
      run.artifact.cleanup.error = null;
    }
  }

  async function runTimeline(run) {
    let terminalState = null;
    try {
      if (run.controller.signal.aborted) throw makeAbortError(run.stopReason);
      transitionRun(run, "running");
      run.artifact.started_at = nowIso();
      run.startMonotonicMs = clock.now();

      for (let index = 0; index < run.timeline.events.length; index += 1) {
        const compiledEvent = run.timeline.events[index];
        const adapterEvent = run.artifact.events[index];
        if (compiledEvent.disposition === "skip") continue;
        await clock.waitUntil(run.startMonotonicMs + (compiledEvent.at_sec * 1000), { signal: run.controller.signal });
        if (run.controller.signal.aborted) throw makeAbortError(run.stopReason);
        await executeEvent(run, compiledEvent, adapterEvent);
      }

      await clock.waitUntil(run.startMonotonicMs + (run.timeline.duration_sec * 1000), { signal: run.controller.signal });
      if (run.controller.signal.aborted) throw makeAbortError(run.stopReason);
      terminalState = "completed";
    } catch (error) {
      if (run.stopRequested || run.controller.signal.aborted || isAbortError(error)) {
        cancelPendingEvents(run, run.stopReason || "run stopped");
        terminalState = run.cleanupFailures.size ? "failed" : "cancelled";
      } else {
        cancelPendingEvents(run, `run failed: ${errorMessage(error)}`);
        terminalState = "failed";
      }
    } finally {
      finalizeCleanup(run);
      if (run.cleanupFailures.size) terminalState = "failed";
      transitionRun(run, terminalState || "failed");
      run.artifact.ended_at = nowIso();
      if (run.externalSignal && run.externalAbortListener) {
        run.externalSignal.removeEventListener("abort", run.externalAbortListener);
      }
    }
    return snapshot(run);
  }

  function start(request) {
    exactKeys(
      request,
      ["timeline", "ownerId", "sessionId", "slotId", "correlationId", "signal"],
      ["timeline", "ownerId", "sessionId", "slotId", "correlationId"],
      "start request",
    );
    const ownerId = requireOpaqueId(request.ownerId, "start request.ownerId");
    const sessionId = requireOpaqueId(request.sessionId, "start request.sessionId");
    const slotId = requireOpaqueId(request.slotId, "start request.slotId");
    const correlationId = requireOpaqueId(request.correlationId, "start request.correlationId");
    const externalSignal = validateSignal(request.signal, "start request.signal");
    const timeline = validateTimelineForStart(request.timeline);
    if (activeRunCount() >= maxConcurrentRuns) {
      fail("TIMELINE_CONCURRENCY_LIMIT_EXCEEDED", "Timeline scheduler concurrency limit reached", { status: 429, retryable: true });
    }
    for (const existing of runs.values()) {
      if (!isTerminal(existing) && existing.artifact.session_id === sessionId && existing.artifact.slot_id === slotId) {
        fail("TIMELINE_SLOT_BUSY", "This session and slot already have an active timeline", { status: 409 });
      }
    }
    pruneRetainedRuns();
    const runId = nextRunId();
    const controller = new AbortController();
    const normalizedRequest = { ownerId, sessionId, slotId, correlationId };
    const run = {
      timeline,
      artifact: buildArtifact(normalizedRequest, timeline, runId),
      controller,
      externalSignal,
      externalAbortListener: null,
      startMonotonicMs: null,
      stopRequested: false,
      stopReason: null,
      stopPromise: null,
      active: null,
      cleanupFailures: new Set(),
      completion: null,
    };
    runs.set(runId, run);
    if (externalSignal) {
      run.externalAbortListener = () => requestStop(run, externalSignal.reason || "external abort");
      if (externalSignal.aborted) run.externalAbortListener();
      else externalSignal.addEventListener("abort", run.externalAbortListener, { once: true });
    }
    run.completion = Promise.resolve().then(() => runTimeline(run));
    return snapshot(run);
  }

  function requestStop(run, reason) {
    if (isTerminal(run)) return Promise.resolve(snapshot(run));
    if (run.stopPromise) return run.stopPromise;
    run.stopRequested = true;
    run.stopReason = typeof reason === "string" && reason.trim() ? reason.trim().slice(0, 500) : "stop requested";
    transitionRun(run, "stopping");
    run.controller.abort(run.stopReason);
    run.stopPromise = run.completion;
    return run.stopPromise;
  }

  function stop(request) {
    exactKeys(request, ["runId", "ownerId", "sessionId", "reason"], ["runId", "ownerId", "sessionId"], "stop request");
    const access = { runId: request.runId, ownerId: request.ownerId, sessionId: request.sessionId };
    const run = accessRun(access, "stop request");
    if (request.reason !== undefined && (typeof request.reason !== "string" || request.reason.length > 500)) {
      fail("TIMELINE_SCHEDULER_INPUT_INVALID", "stop request.reason is invalid");
    }
    return requestStop(run, request.reason);
  }

  function getRun(request) {
    return snapshot(accessRun(request, "get request"));
  }

  function waitForRun(request) {
    return accessRun(request, "wait request").completion;
  }

  function replay(request) {
    exactKeys(
      request,
      ["runId", "ownerId", "sessionId", "correlationId", "signal"],
      ["runId", "ownerId", "sessionId", "correlationId"],
      "replay request",
    );
    const original = accessRun({ runId: request.runId, ownerId: request.ownerId, sessionId: request.sessionId }, "replay request");
    if (!isTerminal(original)) fail("TIMELINE_REPLAY_NOT_READY", "Only a terminal timeline run can be replayed", { status: 409 });
    return start({
      timeline: original.timeline,
      ownerId: original.artifact.owner_id,
      sessionId: original.artifact.session_id,
      slotId: original.artifact.slot_id,
      correlationId: requireOpaqueId(request.correlationId, "replay request.correlationId"),
      ...(request.signal === undefined ? {} : { signal: request.signal }),
    });
  }

  return Object.freeze({
    start,
    stop,
    replay,
    getRun,
    waitForRun,
    limits: Object.freeze({ maxQueueSize, maxConcurrentRuns, maxRetainedRuns, hookTimeoutMs }),
  });
}

module.exports = {
  DEFAULT_HOOK_TIMEOUT_MS,
  DEFAULT_MAX_CONCURRENT_RUNS,
  DEFAULT_MAX_QUEUE_SIZE,
  DEFAULT_MAX_RETAINED_RUNS,
  RUN_TRANSITIONS,
  TimelineEventTimeoutError,
  VistaTimelineSchedulerError,
  createDefaultMonotonicClock,
  createVistaTimelineScheduler,
  validateTimelineRunArtifact,
};
