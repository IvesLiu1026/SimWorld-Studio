"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { createVistaImporter } = require("../vista-importer");
const {
  ACTION_ADAPTER_METHODS,
  ACTION_ADAPTER_SCHEMA,
  BINDINGS_SCHEMA,
  CAPABILITY_REGISTRY_SCHEMA,
  TIMELINE_RUN_SCHEMA,
  TIMELINE_SCHEMA,
  VistaTimelineCompileError,
  compileVistaTimeline,
  validateActionAdapter,
  validateCompiledTimeline,
} = require("../vista-timeline-compiler");

const FIXTURE_ROOT = path.resolve(__dirname, "fixtures/vista/mmg_040");
const SCHEMA_ROOT = path.resolve(__dirname, "../schemas");
const REQUEST = Object.freeze({
  datasetRevision: "round1_reviewed_latest",
  sampleId: "mmg_040",
  attempt: 7,
  scenarioType: "multimodal_grounded",
});

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

async function loadMmg040() {
  const importer = createVistaImporter({
    registry: {
      round1_reviewed_latest: { root: FIXTURE_ROOT },
    },
  });
  return importer.preview(REQUEST);
}

function makeLifecycleCounters() {
  const calls = Object.fromEntries(ACTION_ADAPTER_METHODS.map((method) => [method, 0]));
  const handlers = Object.fromEntries(ACTION_ADAPTER_METHODS.map((method) => [method, () => {
    calls[method] += 1;
    throw new Error(`compiler must not invoke ${method}`);
  }]));
  return { calls, handlers };
}

function makeAdapter({
  adapterId,
  action,
  actorKinds = ["player"],
  targetPolicy = "optional",
  targetKinds = ["prop"],
  actorCapabilities = [],
  targetCapabilities = [],
  timeoutMs = 2000,
  lifecycle = null,
}) {
  const handlers = lifecycle || Object.fromEntries(ACTION_ADAPTER_METHODS.map((method) => [method, () => undefined]));
  return {
    schema: ACTION_ADAPTER_SCHEMA,
    adapter_id: adapterId,
    version: "1.0.0",
    action,
    contract: {
      actor_kinds: actorKinds,
      target_policy: targetPolicy,
      target_kinds: targetKinds,
      required_actor_capabilities: actorCapabilities,
      required_target_capabilities: targetCapabilities,
    },
    timeout_ms: timeoutMs,
    ...handlers,
  };
}

function makeBindings(scene, { omitActor = false, omitTarget = null, actorCapabilities = ["gaze", "wait"] } = {}) {
  return {
    schema: BINDINGS_SCHEMA,
    revision: "mmg040_bindings_v1",
    actors: omitActor ? [] : [{
      source_id: "camera_wearer",
      binding_id: "actor_camera_wearer",
      kind: "player",
      capabilities: actorCapabilities,
    }],
    entities: scene.entities
      .filter((entity) => entity.id !== omitTarget)
      .map((entity) => ({
        source_id: entity.id,
        binding_id: `bound_${entity.id}`,
        kind: "prop",
        capabilities: ["gaze_target"],
      })),
  };
}

function makeRegistry(adapters) {
  return {
    schema: CAPABILITY_REGISTRY_SCHEMA,
    revision: "verified_actions_v1",
    adapters,
  };
}

function baselineAdapters(lifecycle = null) {
  return [
    makeAdapter({
      adapterId: "look_at_v1",
      action: "look_at",
      actorCapabilities: ["gaze"],
      targetCapabilities: ["gaze_target"],
      lifecycle,
    }),
    makeAdapter({
      adapterId: "pause_v1",
      action: "pause",
      actorCapabilities: ["wait"],
      lifecycle,
    }),
  ];
}

function compile(scene, { policy = "strict", bindings = null, adapters = null } = {}) {
  return compileVistaTimeline(scene, {
    policy,
    bindings: bindings || makeBindings(scene),
    capabilityRegistry: makeRegistry(adapters || baselineAdapters()),
  });
}

function expectCompileError(fn, code = null) {
  assert.throws(fn, (error) => {
    assert.ok(error instanceof VistaTimelineCompileError);
    assert.equal(error.status, 400);
    assert.equal(error.retryable, false);
    if (code) assert.equal(error.code, code);
    return true;
  });
}

test("timeline and run schemas define versioned preflight and lifecycle contracts", () => {
  const timelineSchema = JSON.parse(fs.readFileSync(path.join(SCHEMA_ROOT, "vista-timeline-v1.schema.json"), "utf8"));
  const runSchema = JSON.parse(fs.readFileSync(path.join(SCHEMA_ROOT, "vista-timeline-run-v1.schema.json"), "utf8"));

  assert.equal(timelineSchema.$schema, "https://json-schema.org/draft/2020-12/schema");
  assert.equal(timelineSchema.properties.schema.const, TIMELINE_SCHEMA);
  assert.deepEqual(timelineSchema.properties.policy.enum, ["strict", "lenient"]);
  assert.deepEqual(timelineSchema.$defs.event.properties.preflight_status.enum, [
    "ready", "unbound", "unsupported", "incompatible", "ambiguous",
  ]);
  assert.equal(runSchema.properties.schema.const, TIMELINE_RUN_SCHEMA);
  assert.equal(runSchema.properties.clock.const, "server_monotonic");
  assert.deepEqual(runSchema.$defs.runEvent.properties.state.enum, [
    "pending", "dispatched", "running", "completed", "skipped", "failed", "cancelled", "timed_out",
  ]);
  assert.ok(runSchema.required.includes("cleanup"));
  assert.ok(runSchema.required.includes("session_id"));
  assert.ok(runSchema.required.includes("slot_id"));
});

test("fixed adapter validation requires the six lifecycle handlers and rejects command-shaped extras", () => {
  const valid = makeAdapter({ adapterId: "look_at_v1", action: "look_at" });
  const checked = validateActionAdapter(valid);
  assert.equal(checked.adapter_id, "look_at_v1");
  assert.equal(checked.action, "look_at");
  assert.equal(checked.timeout_ms, 2000);

  for (const method of ACTION_ADAPTER_METHODS) {
    const invalid = { ...valid };
    delete invalid[method];
    expectCompileError(() => validateActionAdapter(invalid), "TIMELINE_ADAPTER_INVALID");
  }

  expectCompileError(() => validateActionAdapter({ ...valid, command: "caller-authored-vbp" }), "TIMELINE_ADAPTER_INVALID");
  expectCompileError(() => validateActionAdapter({ ...valid, timeout_ms: 0 }), "TIMELINE_ADAPTER_INVALID");
});

test("mmg_040 strict preflight preserves the ordered 5-second compound beat and blocks unsupported actions", async () => {
  const scene = await loadMmg040();
  const { calls, handlers } = makeLifecycleCounters();
  const before = JSON.stringify(scene);
  const timeline = compile(scene, { adapters: baselineAdapters(handlers) });

  assert.equal(timeline.schema, TIMELINE_SCHEMA);
  assert.equal(timeline.policy, "strict");
  assert.equal(timeline.duration_sec, 12);
  assert.equal(timeline.start_allowed, false);
  assert.deepEqual(timeline.events.map((event) => event.at_sec), [0, 2, 5, 5, 9]);
  assert.deepEqual(timeline.events.map((event) => event.action), ["look_at", "drag", "brace", "lift_foot", "pause"]);
  assert.deepEqual(timeline.events.map((event) => event.preflight_status), ["ready", "unsupported", "unsupported", "unsupported", "ready"]);
  assert.deepEqual(timeline.events.map((event) => event.disposition), ["execute", "block", "block", "block", "execute"]);
  assert.deepEqual(
    timeline.issues.map((issue) => [issue.event_id, issue.code]),
    [
      ["beat-0002", "ACTION_UNSUPPORTED"],
      ["beat-0003-brace", "ACTION_UNSUPPORTED"],
      ["beat-0003-lift_foot", "ACTION_UNSUPPORTED"],
    ],
  );
  assert.deepEqual(timeline.summary, { total: 5, ready: 2, skipped: 0, blocked: 3, errors: 3, warnings: 0 });
  assert.deepEqual(calls, Object.fromEntries(ACTION_ADAPTER_METHODS.map((method) => [method, 0])));
  assert.equal(JSON.stringify(scene), before, "compiler must not mutate SceneSpec");
  assert.equal(JSON.stringify(timeline).includes("precondition"), false, "runtime handlers must not enter artifacts");
  assert.equal(validateCompiledTimeline(timeline), timeline);
});

test("mmg_040 compilation is deterministic across registry and binding input order", async () => {
  const scene = await loadMmg040();
  const bindings = makeBindings(scene);
  const adapters = baselineAdapters();
  const first = compileVistaTimeline(scene, {
    policy: "strict",
    bindings,
    capabilityRegistry: makeRegistry(adapters),
  });
  const second = compileVistaTimeline(scene, {
    policy: "strict",
    bindings: {
      ...bindings,
      actors: [...bindings.actors].reverse(),
      entities: [...bindings.entities].reverse(),
    },
    capabilityRegistry: makeRegistry([...adapters].reverse()),
  });

  assert.deepEqual(second, first);
  assert.equal(second.timeline_id, first.timeline_id);
  assert.equal(JSON.stringify(second), JSON.stringify(first));
  assert.ok(Object.isFrozen(first));
  assert.ok(Object.isFrozen(first.events));
});

test("lenient policy explicitly skips unsupported events without rewriting their action", async () => {
  const scene = await loadMmg040();
  const timeline = compile(scene, { policy: "lenient" });

  assert.equal(timeline.start_allowed, true);
  assert.deepEqual(timeline.events.map((event) => event.action), ["look_at", "drag", "brace", "lift_foot", "pause"]);
  assert.deepEqual(timeline.events.map((event) => event.disposition), ["execute", "skip", "skip", "skip", "execute"]);
  assert.ok(timeline.issues.every((issue) => issue.severity === "warning"));
  assert.deepEqual(timeline.summary, { total: 5, ready: 2, skipped: 3, blocked: 0, errors: 0, warnings: 3 });
});

test("an unrelated move adapter is never used as a generic fallback for compound interaction actions", async () => {
  const scene = await loadMmg040();
  const adapters = [
    ...baselineAdapters(),
    makeAdapter({ adapterId: "move_to_v1", action: "move_to" }),
  ];
  const timeline = compile(scene, { adapters });
  const drag = timeline.events.find((event) => event.action === "drag");
  const brace = timeline.events.find((event) => event.action === "brace");
  const liftFoot = timeline.events.find((event) => event.action === "lift_foot");

  assert.equal(drag.preflight_status, "unsupported");
  assert.equal(drag.adapter, null);
  assert.equal(brace.preflight_status, "unsupported");
  assert.equal(brace.adapter, null);
  assert.equal(liftFoot.preflight_status, "unsupported");
  assert.equal(liftFoot.adapter, null);
  assert.equal(timeline.events.some((event) => event.adapter && event.adapter.adapter_id === "move_to_v1"), false);
});

test("multiple compatible fixed adapters produce an explicit ambiguous event", async () => {
  const scene = await loadMmg040();
  const adapters = [
    ...baselineAdapters(),
    makeAdapter({
      adapterId: "look_at_alt_v1",
      action: "look_at",
      actorCapabilities: ["gaze"],
      targetCapabilities: ["gaze_target"],
    }),
  ];
  const timeline = compile(scene, { adapters });
  const event = timeline.events[0];
  const issue = timeline.issues.find((candidate) => candidate.event_id === event.event_id);

  assert.equal(event.preflight_status, "ambiguous");
  assert.equal(event.adapter, null);
  assert.equal(event.disposition, "block");
  assert.equal(issue.code, "ACTION_ADAPTER_AMBIGUOUS");
  assert.deepEqual(issue.candidates, ["look_at_alt_v1", "look_at_v1"]);
});

test("actor, target, and capability binding failures are explicit preflight outcomes", async () => {
  const scene = await loadMmg040();
  const noActor = compile(scene, { bindings: makeBindings(scene, { omitActor: true }) });
  assert.ok(noActor.events.every((event) => event.preflight_status === "unbound"));
  assert.ok(noActor.issues.some((issue) => issue.code === "ACTOR_BINDING_MISSING"));

  const lookTarget = scene.timeline[0].target_id;
  const noTarget = compile(scene, { bindings: makeBindings(scene, { omitTarget: lookTarget }) });
  assert.equal(noTarget.events[0].preflight_status, "unbound");
  assert.equal(noTarget.issues.find((issue) => issue.event_id === "beat-0001").code, "TARGET_BINDING_MISSING");

  const incompatible = compile(scene, { bindings: makeBindings(scene, { actorCapabilities: ["wait"] }) });
  assert.equal(incompatible.events[0].preflight_status, "incompatible");
  assert.equal(incompatible.issues.find((issue) => issue.event_id === "beat-0001").code, "ACTION_ADAPTER_INCOMPATIBLE");
});

test("all-skipped lenient preflight remains blocked with a global no-executable issue", async () => {
  const scene = await loadMmg040();
  const timeline = compile(scene, { policy: "lenient", adapters: [] });

  assert.equal(timeline.start_allowed, false);
  assert.equal(timeline.summary.ready, 0);
  assert.equal(timeline.summary.skipped, 5);
  assert.ok(timeline.issues.some((issue) => issue.code === "NO_EXECUTABLE_EVENTS" && issue.event_id === null));
});

test("absolute timestamp bounds, stable event ids, and target references fail before preflight", async () => {
  const base = await loadMmg040();

  const exactlyAtEnd = clone(base);
  exactlyAtEnd.timeline.at(-1).at_sec = 12;
  assert.equal(compile(exactlyAtEnd).events.at(-1).at_sec, 12);

  const afterEnd = clone(base);
  afterEnd.timeline.at(-1).at_sec = 12.001;
  expectCompileError(() => compile(afterEnd), "TIMELINE_SCENE_INVALID");

  const negative = clone(base);
  negative.timeline[0].at_sec = -0.001;
  expectCompileError(() => compile(negative), "TIMELINE_SCENE_INVALID");

  const duplicateId = clone(base);
  duplicateId.timeline[1].event_id = duplicateId.timeline[0].event_id;
  expectCompileError(() => compile(duplicateId), "TIMELINE_SCENE_INVALID");

  const unstableId = clone(base);
  unstableId.timeline[0].event_id = "event generated at runtime";
  expectCompileError(() => compile(unstableId), "TIMELINE_SCENE_INVALID");

  const unknownTarget = clone(base);
  unknownTarget.timeline[0].target_id = "not_a_scene_entity";
  expectCompileError(() => compile(unknownTarget), "TIMELINE_SCENE_INVALID");
});

test("events are ordered deterministically by absolute timestamp then stable id", async () => {
  const scene = clone(await loadMmg040());
  scene.timeline = [scene.timeline[3], scene.timeline[4], scene.timeline[1], scene.timeline[0], scene.timeline[2]];
  const timeline = compile(scene, { policy: "lenient" });

  assert.deepEqual(timeline.events.map((event) => event.at_sec), [0, 2, 5, 5, 9]);
  assert.deepEqual(timeline.events.map((event) => event.event_id), [
    "beat-0001",
    "beat-0002",
    "beat-0003-brace",
    "beat-0003-lift_foot",
    "beat-0004",
  ]);
});
