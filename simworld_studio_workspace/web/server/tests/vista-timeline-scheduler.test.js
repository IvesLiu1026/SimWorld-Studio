"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const { createVistaImporter } = require("../vista-importer");
const {
  ACTION_ADAPTER_METHODS,
  ACTION_ADAPTER_SCHEMA,
  BINDINGS_SCHEMA,
  CAPABILITY_REGISTRY_SCHEMA,
  compileVistaTimeline,
} = require("../vista-timeline-compiler");
const {
  RUN_TRANSITIONS,
  VistaTimelineSchedulerError,
  createVistaTimelineScheduler,
} = require("../vista-timeline-scheduler");

const FIXTURE_ROOT = path.resolve(__dirname, "fixtures/vista/mmg_040");
const OWNER = "owner:timeline-tests";
const SESSION = "session:timeline-tests";
const SLOT = "slot:0";

class FakeMonotonicClock {
  constructor(initialMs = 0) {
    this.currentMs = initialMs;
    this.waiters = new Set();
  }

  now() {
    return this.currentMs;
  }

  waitUntil(deadlineMs, { signal } = {}) {
    return this.#wait(deadlineMs, signal);
  }

  delay(ms, { signal } = {}) {
    return this.#wait(this.currentMs + ms, signal);
  }

  #wait(deadlineMs, signal) {
    if (signal && signal.aborted) return Promise.reject(this.#abortError());
    if (this.currentMs >= deadlineMs) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const waiter = { deadlineMs, resolve, reject, signal, onAbort: null };
      waiter.onAbort = () => {
        this.waiters.delete(waiter);
        reject(this.#abortError());
      };
      if (signal) signal.addEventListener("abort", waiter.onAbort, { once: true });
      this.waiters.add(waiter);
    });
  }

  #abortError() {
    const error = new Error("fake clock wait aborted");
    error.name = "AbortError";
    error.code = "ABORT_ERR";
    return error;
  }

  advanceTo(nextMs) {
    if (!Number.isFinite(nextMs) || nextMs < this.currentMs) throw new Error("fake monotonic clock cannot move backwards");
    this.currentMs = nextMs;
    const due = [...this.waiters]
      .filter((waiter) => waiter.deadlineMs <= nextMs)
      .sort((left, right) => left.deadlineMs - right.deadlineMs);
    for (const waiter of due) {
      this.waiters.delete(waiter);
      if (waiter.signal) waiter.signal.removeEventListener("abort", waiter.onAbort);
      waiter.resolve();
    }
  }

  advanceBy(ms) {
    this.advanceTo(this.currentMs + ms);
  }
}

function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

async function flushTurns(count = 3) {
  for (let index = 0; index < count; index += 1) await flush();
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

async function loadMmg040() {
  const importer = createVistaImporter({
    registry: { round1_reviewed_latest: { root: FIXTURE_ROOT } },
  });
  return importer.preview({
    datasetRevision: "round1_reviewed_latest",
    sampleId: "mmg_040",
    attempt: 7,
    scenarioType: "multimodal_grounded",
  });
}

function makeBindings(scene) {
  return {
    schema: BINDINGS_SCHEMA,
    revision: "scheduler_bindings_v1",
    actors: [{
      source_id: "camera_wearer",
      binding_id: "actor_camera_wearer",
      kind: "player",
      capabilities: ["brace", "drag", "gaze", "lift_foot", "wait"],
    }],
    entities: scene.entities.map((entity) => ({
      source_id: entity.id,
      binding_id: `bound_${entity.id}`,
      kind: "prop",
      capabilities: ["interaction_target"],
    })),
  };
}

function defaultLifecycle(log, action) {
  return {
    precondition(context) {
      log.push([action, "precondition", context.event_id]);
      return { ok: true };
    },
    execute(context) {
      log.push([action, "execute", context.event_id]);
      return { actor_id: context.actor_id, target_id: context.target_id };
    },
    completion(context) {
      log.push([action, "completion", context.event_id]);
      return { completed: true, signal: "fake_completion" };
    },
    timeout(context) {
      log.push([action, "timeout", context.event_id]);
      return { handled: true };
    },
    cancel(context) {
      log.push([action, "cancel", context.event_id]);
      return { cancelled: true };
    },
    cleanup(context) {
      log.push([action, "cleanup", context.event_id]);
      return { cleaned: true };
    },
  };
}

function makeAdapter(action, log, { timeoutMs = 1000, lifecycle = {} } = {}) {
  const defaults = defaultLifecycle(log, action);
  return {
    schema: ACTION_ADAPTER_SCHEMA,
    adapter_id: `${action}_v1`,
    version: "1.0.0",
    action,
    contract: {
      actor_kinds: ["player"],
      target_policy: "optional",
      target_kinds: ["prop"],
      required_actor_capabilities: [action === "look_at" ? "gaze" : action === "pause" ? "wait" : action],
      required_target_capabilities: [],
    },
    timeout_ms: timeoutMs,
    ...defaults,
    ...lifecycle,
  };
}

function makeRegistry(log, overrides = {}) {
  return {
    schema: CAPABILITY_REGISTRY_SCHEMA,
    revision: "scheduler_actions_v1",
    adapters: ["look_at", "drag", "brace", "lift_foot", "pause"].map((action) => makeAdapter(action, log, overrides[action] || {})),
  };
}

function makeTimeline(scene, registry, policy = "strict") {
  return compileVistaTimeline(scene, {
    policy,
    bindings: makeBindings(scene),
    capabilityRegistry: registry,
  });
}

function createIdFactory() {
  let value = 0;
  return () => `run${String(++value).padStart(5, "0")}`;
}

function createHarness(scene, {
  registry = null,
  clock = new FakeMonotonicClock(),
  maxQueueSize = 256,
  hookTimeoutMs = 5000,
  engineTimeSampler = null,
} = {}) {
  const log = [];
  const capabilityRegistry = registry || makeRegistry(log);
  const timeline = makeTimeline(scene, capabilityRegistry);
  const engineSamples = [];
  const scheduler = createVistaTimelineScheduler({
    capabilityRegistry,
    clock,
    wallClock: () => new Date(Date.UTC(2026, 6, 14) + clock.now()).toISOString(),
    idFactory: createIdFactory(),
    maxQueueSize,
    hookTimeoutMs,
    engineTimeSampler: engineTimeSampler || (async (context) => {
      engineSamples.push([context.event_id, clock.now()]);
      return 100 + (clock.now() / 1000);
    }),
  });
  return { scheduler, timeline, registry: capabilityRegistry, clock, log, engineSamples };
}

function startRequest(timeline, overrides = {}) {
  return {
    timeline,
    ownerId: OWNER,
    sessionId: SESSION,
    slotId: SLOT,
    correlationId: "corr:timeline:001",
    ...overrides,
  };
}

function access(runId, overrides = {}) {
  return { runId, ownerId: OWNER, sessionId: SESSION, ...overrides };
}

function expectSchedulerError(fn, code) {
  assert.throws(fn, (error) => {
    assert.ok(error instanceof VistaTimelineSchedulerError);
    assert.equal(error.code, code);
    assert.equal(error.retryable, false);
    return true;
  });
}

async function driveMmg040(clock, startMs = 0, secondBeatMs = startMs + 2000) {
  await flushTurns();
  clock.advanceTo(secondBeatMs);
  await flushTurns();
  clock.advanceTo(startMs + 5000);
  await flushTurns();
  clock.advanceTo(startMs + 9000);
  await flushTurns();
  clock.advanceTo(startMs + 12000);
  await flushTurns();
}

test("server monotonic scheduler executes the ordered 5-second compound beat and waits through second 12", async () => {
  const scene = await loadMmg040();
  const harness = createHarness(scene);
  const initial = harness.scheduler.start(startRequest(harness.timeline));

  assert.equal(initial.schema, "vista-timeline-run/v1");
  assert.equal(initial.state, "ready");
  assert.equal(initial.clock, "server_monotonic");
  assert.deepEqual(initial.events.map((event) => event.planned_sec), [0, 2, 5, 5, 9]);

  await driveMmg040(harness.clock, 0, 2500);
  const final = await harness.scheduler.waitForRun(access(initial.run_id));

  assert.equal(final.state, "completed");
  assert.equal(final.ended_at, "2026-07-14T00:00:12.000Z");
  assert.deepEqual(final.events.map((event) => event.state), ["completed", "completed", "completed", "completed", "completed"]);
  assert.deepEqual(final.events.map((event) => event.actual_sec), [0, 2.5, 5, 5, 9]);
  assert.deepEqual(final.events.map((event) => event.drift_ms), [0, 500, 0, 0, 0]);
  assert.deepEqual(final.events.map((event) => event.engine_time), [100, 102.5, 105, 105, 109]);
  assert.ok(final.events.every((event) => event.attempt === 1 && event.cleanup_state === "completed"));
  assert.deepEqual(final.cleanup, {
    state: "completed",
    pending_items: [],
    confirmed_stopped: true,
    ended_pie: false,
    error: null,
  });
  assert.deepEqual(harness.engineSamples, [
    ["beat-0001", 0],
    ["beat-0002", 2500],
    ["beat-0003-brace", 5000],
    ["beat-0003-lift_foot", 5000],
    ["beat-0004", 9000],
  ]);
  assert.equal(harness.log.filter((entry) => entry[1] === "precondition").length, 5);
  assert.equal(harness.log.filter((entry) => entry[1] === "execute").length, 5);
  assert.equal(harness.log.filter((entry) => entry[1] === "completion").length, 5);
  assert.equal(harness.log.filter((entry) => entry[1] === "cleanup").length, 5);
  assert.equal(harness.log.some((entry) => entry[1] === "timeout" || entry[1] === "cancel"), false);
});

test("event timeout aborts the lifecycle, calls timeout and cleanup, and fails the run", async () => {
  const scene = clone(await loadMmg040());
  scene.timeline = [scene.timeline[0]];
  scene.duration_sec = 2;
  const log = [];
  const never = new Promise(() => {});
  const registry = {
    schema: CAPABILITY_REGISTRY_SCHEMA,
    revision: "scheduler_actions_v1",
    adapters: [makeAdapter("look_at", log, {
      timeoutMs: 1000,
      lifecycle: { completion: () => never },
    })],
  };
  const harness = createHarness(scene, { registry });
  const initial = harness.scheduler.start(startRequest(harness.timeline));

  await flushTurns();
  assert.equal(harness.scheduler.getRun(access(initial.run_id)).events[0].state, "running");
  harness.clock.advanceBy(1000);
  await flushTurns(5);
  const final = await harness.scheduler.waitForRun(access(initial.run_id));

  assert.equal(final.state, "failed");
  assert.equal(final.events[0].state, "timed_out");
  assert.match(final.events[0].error, /exceeded its 1000ms adapter timeout/);
  assert.equal(final.events[0].cleanup_state, "completed");
  assert.equal(log.filter((entry) => entry[1] === "timeout").length, 1);
  assert.equal(log.filter((entry) => entry[1] === "cleanup").length, 1);
  assert.equal(log.filter((entry) => entry[1] === "cancel").length, 0);
});

test("Stop is idempotent, aborts the active adapter, and cancels every pending event", async () => {
  const scene = await loadMmg040();
  const log = [];
  const never = new Promise(() => {});
  const registry = makeRegistry(log, {
    look_at: { lifecycle: { completion: () => never }, timeoutMs: 60_000 },
  });
  const harness = createHarness(scene, { registry, hookTimeoutMs: 1000 });
  const initial = harness.scheduler.start(startRequest(harness.timeline));
  await flushTurns();
  assert.equal(harness.scheduler.getRun(access(initial.run_id)).events[0].state, "running");

  const firstStop = harness.scheduler.stop({ ...access(initial.run_id), reason: "operator stop" });
  const secondStop = harness.scheduler.stop({ ...access(initial.run_id), reason: "duplicate stop" });
  assert.equal(firstStop, secondStop);
  const [first, second] = await Promise.all([firstStop, secondStop]);

  assert.deepEqual(second, first);
  assert.equal(first.state, "cancelled");
  assert.deepEqual(first.events.map((event) => event.state), ["cancelled", "cancelled", "cancelled", "cancelled", "cancelled"]);
  assert.equal(first.events[0].cleanup_state, "completed");
  assert.ok(first.events.slice(1).every((event) => event.cleanup_state === "not_required"));
  assert.equal(log.filter((entry) => entry[1] === "cancel").length, 1);
  assert.equal(log.filter((entry) => entry[1] === "cleanup").length, 1);
  assert.equal(first.cleanup.confirmed_stopped, true);
  assert.deepEqual(await harness.scheduler.stop({ ...access(initial.run_id), reason: "after terminal" }), first);
});

test("an injected AbortSignal follows the same cancellation and cleanup path", async () => {
  const scene = await loadMmg040();
  const log = [];
  const never = new Promise(() => {});
  const registry = makeRegistry(log, {
    look_at: { lifecycle: { completion: () => never }, timeoutMs: 60_000 },
  });
  const harness = createHarness(scene, { registry });
  const controller = new AbortController();
  const initial = harness.scheduler.start(startRequest(harness.timeline, { signal: controller.signal }));
  await flushTurns();

  controller.abort("upstream request cancelled");
  await flushTurns(4);
  const final = await harness.scheduler.waitForRun(access(initial.run_id));

  assert.equal(final.state, "cancelled");
  assert.match(final.events[0].error, /upstream request cancelled/);
  assert.equal(log.filter((entry) => entry[1] === "cancel").length, 1);
  assert.equal(log.filter((entry) => entry[1] === "cleanup").length, 1);
});

test("cleanup failures are visible and prevent Stop from claiming a clean cancellation", async () => {
  const scene = await loadMmg040();
  const log = [];
  const never = new Promise(() => {});
  const registry = makeRegistry(log, {
    look_at: {
      timeoutMs: 60_000,
      lifecycle: {
        completion: () => never,
        cleanup: (context) => {
          log.push(["look_at", "cleanup", context.event_id]);
          throw new Error("fake cleanup failure");
        },
      },
    },
  });
  const harness = createHarness(scene, { registry });
  const initial = harness.scheduler.start(startRequest(harness.timeline));
  await flushTurns();
  const final = await harness.scheduler.stop({ ...access(initial.run_id), reason: "operator stop" });

  assert.equal(final.state, "failed");
  assert.equal(final.events[0].cleanup_state, "failed");
  assert.match(final.events[0].error, /Cleanup failed/);
  assert.equal(final.cleanup.state, "partial");
  assert.equal(final.cleanup.confirmed_stopped, false);
  assert.deepEqual(final.cleanup.pending_items, ["beat-0001"]);
});

test("Replay creates a fresh run only after the original reaches a terminal state", async () => {
  const scene = await loadMmg040();
  const harness = createHarness(scene);
  const firstInitial = harness.scheduler.start(startRequest(harness.timeline));
  expectSchedulerError(() => harness.scheduler.replay({
    ...access(firstInitial.run_id),
    correlationId: "corr:timeline:replay:early",
  }), "TIMELINE_REPLAY_NOT_READY");

  await driveMmg040(harness.clock, 0);
  const firstFinal = await harness.scheduler.waitForRun(access(firstInitial.run_id));
  const replayInitial = harness.scheduler.replay({
    ...access(firstInitial.run_id),
    correlationId: "corr:timeline:replay:001",
  });

  assert.notEqual(replayInitial.run_id, firstInitial.run_id);
  assert.equal(replayInitial.timeline_id, firstInitial.timeline_id);
  assert.equal(replayInitial.state, "ready");
  await driveMmg040(harness.clock, 12000);
  const replayFinal = await harness.scheduler.waitForRun(access(replayInitial.run_id));

  assert.equal(replayFinal.state, "completed");
  assert.equal(replayFinal.started_at, "2026-07-14T00:00:12.000Z");
  assert.deepEqual(replayFinal.events.map((event) => event.actual_sec), [0, 2, 5, 5, 9]);
  assert.deepEqual(harness.scheduler.getRun(access(firstInitial.run_id)), firstFinal);
});

test("bounded queue and server-owned clock request shape fail before any adapter invocation", async () => {
  const scene = await loadMmg040();
  const log = [];
  const registry = makeRegistry(log);
  const timeline = makeTimeline(scene, registry);
  const harness = createHarness(scene, { registry, maxQueueSize: 3 });

  expectSchedulerError(() => harness.scheduler.start(startRequest(timeline)), "TIMELINE_QUEUE_LIMIT_EXCEEDED");
  expectSchedulerError(() => harness.scheduler.start({
    ...startRequest(timeline),
    browserElapsedSec: 9,
  }), "TIMELINE_SCHEDULER_INPUT_INVALID");
  assert.deepEqual(log, []);
});

test("blocked preflight, registry mismatch, and cross-session access fail closed", async () => {
  const scene = await loadMmg040();
  const log = [];
  const fullRegistry = makeRegistry(log);
  const harness = createHarness(scene, { registry: fullRegistry });
  const incompleteRegistry = {
    schema: CAPABILITY_REGISTRY_SCHEMA,
    revision: "scheduler_actions_v1",
    adapters: [makeAdapter("look_at", [], {}), makeAdapter("pause", [], {})],
  };
  const blocked = makeTimeline(scene, incompleteRegistry);
  expectSchedulerError(() => harness.scheduler.start(startRequest(blocked)), "TIMELINE_START_BLOCKED");

  const mismatched = clone(harness.timeline);
  mismatched.registry_revision = "other_registry_v1";
  expectSchedulerError(() => harness.scheduler.start(startRequest(mismatched)), "TIMELINE_REGISTRY_MISMATCH");

  const initial = harness.scheduler.start(startRequest(harness.timeline));
  expectSchedulerError(() => harness.scheduler.getRun(access(initial.run_id, { sessionId: "session:foreign" })), "TIMELINE_RUN_ACCESS_DENIED");
  await harness.scheduler.stop({ ...access(initial.run_id), reason: "test cleanup" });
});

test("scheduler requires an injected engine-time sampler", () => {
  const registry = makeRegistry([]);
  expectSchedulerError(() => createVistaTimelineScheduler({ capabilityRegistry: registry }), "TIMELINE_SCHEDULER_CONFIG_INVALID");
  assert.deepEqual(ACTION_ADAPTER_METHODS, ["precondition", "execute", "completion", "timeout", "cancel", "cleanup"]);
  assert.deepEqual(RUN_TRANSITIONS.ready, ["running", "stopping"]);
  assert.deepEqual(RUN_TRANSITIONS.stopping, ["cancelled", "failed"]);
});

test("invalid engine-time evidence fails the dispatched event before adapter mutation", async () => {
  const scene = await loadMmg040();
  const log = [];
  const registry = makeRegistry(log);
  const harness = createHarness(scene, {
    registry,
    engineTimeSampler: async () => null,
  });
  const initial = harness.scheduler.start(startRequest(harness.timeline));
  await flushTurns(4);
  const final = await harness.scheduler.waitForRun(access(initial.run_id));

  assert.equal(final.state, "failed");
  assert.equal(final.events[0].state, "failed");
  assert.match(final.events[0].error, /invalid value/);
  assert.equal(final.events[0].cleanup_state, "not_required");
  assert.deepEqual(final.events.slice(1).map((event) => event.state), ["cancelled", "cancelled", "cancelled", "cancelled"]);
  assert.deepEqual(log, [], "adapter handlers must not run without engine-time evidence");
});
