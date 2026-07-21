"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const {
  ACTION_DEFINITIONS,
  ANIMATION_CONTENT_PROFILE_SCHEMA,
  ANIMATION_PREFLIGHT_RESPONSE_SCHEMA,
  VistaAnimationContractError,
  compileVistaAnimationProgram,
  validateContentProfile,
  validateVistaAnimationProgram,
} = require("../vista-animation-contract");
const {
  ANIMATION_EVIDENCE_SCHEMA,
  RELEASE_RESPONSE_SCHEMA,
  RESTORE_RESPONSE_SCHEMA,
  SNAPSHOT_RESPONSE_SCHEMA,
  START_RESPONSE_SCHEMA,
  STOP_RESPONSE_SCHEMA,
  WAIT_RESPONSE_SCHEMA,
  VistaAnimationRuntimeError,
  createVistaAnimationRuntime,
  validateAnimationEvidenceManifest,
} = require("../vista-animation-runtime");
const {
  BINDINGS_SCHEMA,
  compileVistaTimeline,
} = require("../vista-timeline-compiler");
const { createVistaTimelineScheduler } = require("../vista-timeline-scheduler");

const ALL_ACTIONS = Object.freeze(["look_at", "drag", "brace", "lift_foot", "pause", "fall", "recover"]);
const CHECKSUM = "a".repeat(64);
const CONTENT_DIGEST = "b".repeat(64);
const STATE_DIGEST = "c".repeat(64);
const SCENE_REVISION = "mmg_040@aaaaaaaaaaaaaaaa";
const SCHEMA_ROOT = path.resolve(__dirname, "../schemas");
const COUNTERFACTUAL_PICK_UP_FALL_SCENE = path.resolve(
  __dirname,
  "fixtures/vista/counterfactual/pick-up-fall-12s.scene.json",
);

class FakeMonotonicClock {
  constructor() {
    this.currentMs = 0;
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
    const error = new Error("fake clock aborted");
    error.name = "AbortError";
    error.code = "ABORT_ERR";
    return error;
  }

  advanceTo(nextMs) {
    assert.ok(nextMs >= this.currentMs);
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
}

function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

async function flushTurns(count = 5) {
  for (let index = 0; index < count; index += 1) await flush();
}

function makeIsoClock() {
  let offset = 0;
  return () => new Date(Date.UTC(2026, 6, 21) + offset++).toISOString();
}

function makeProfile(actions = ALL_ACTIONS) {
  return {
    schema: ANIMATION_CONTENT_PROFILE_SCHEMA,
    profile_id: "vista_hands_ik_v1",
    revision: "vista_hands_ik_2026_r1",
    content_revision: "simworld-content-2026.07.21-r1",
    content_digest: CONTENT_DIGEST,
    pawn_class_path: "/Game/VISTA/Characters/BP_VistaFirstPerson.BP_VistaFirstPerson_C",
    skeleton_path: "/Game/VISTA/Characters/SK_VistaHuman.SK_VistaHuman",
    verification: {
      status: "verified",
      receipt_id: "ue-content-receipt:2026-07-21:001",
      verified_at: "2026-07-21T00:00:00.000Z",
    },
    actions: actions.map((action) => ({
      action,
      adapter_id: ACTION_DEFINITIONS[action].adapter_id,
      version: "1.0.0",
      bridge_action_id: ACTION_DEFINITIONS[action].bridge_action_id,
      implementation_asset: `/Game/VISTA/Animations/ABP_${action}.ABP_${action}`,
      completion_signal: ACTION_DEFINITIONS[action].completion_signal || `vista_${action}_complete`,
      timeout_ms: 5000,
    })),
  };
}

function makeBindings({ actorCapabilities = null } = {}) {
  return {
    schema: BINDINGS_SCHEMA,
    revision: "mmg040_animation_bindings_v1",
    actors: [{
      source_id: "camera_wearer",
      binding_id: "actor_camera_wearer",
      kind: "player",
      capabilities: actorCapabilities || [
        "fall_montage",
        "gaze",
        "hold_pose",
        "lower_body_ik",
        "recover_montage",
        "root_motion",
        "upper_body_ik",
      ],
    }],
    entities: [
      {
        source_id: "high_box",
        binding_id: "target_high_box",
        kind: "prop",
        capabilities: ["gaze_target"],
      },
      {
        source_id: "rolling_chair",
        binding_id: "target_rolling_chair",
        kind: "prop",
        capabilities: ["draggable", "foot_contact_target", "gaze_target", "hand_contact_target"],
      },
    ],
  };
}

function makeScene(actions = ALL_ACTIONS) {
  const definitions = {
    look_at: { at: 0, target: "high_box" },
    drag: { at: 2, target: "rolling_chair" },
    brace: { at: 5, target: "rolling_chair" },
    lift_foot: { at: 5, target: "rolling_chair" },
    pause: { at: 9, target: "rolling_chair" },
    fall: { at: 10, target: null },
    recover: { at: 11, target: null },
  };
  return {
    schema: "vista-simworld-scene/v1",
    scene_id: SCENE_REVISION,
    source: { source_checksum: CHECKSUM },
    duration_sec: 12,
    entities: [{ id: "high_box" }, { id: "rolling_chair" }],
    timeline: actions.map((action, index) => ({
      event_id: `beat-${String(index + 1).padStart(4, "0")}`,
      at_sec: definitions[action].at,
      action,
      actor_id: "camera_wearer",
      target_id: definitions[action].target,
      parameters: {},
      source_pointer: `/Scene/Actions/${index}`,
    })),
  };
}

function bindingRuntimeResponse(binding, { anchors = [] } = {}) {
  return {
    binding_id: binding.binding_id,
    available: true,
    class_matches: true,
    skeleton_matches: true,
    capabilities: [...binding.capabilities],
    anchor_kinds: anchors,
  };
}

function makeFakeBroker({
  bindings,
  missingActions = [],
  missingChairAnchors = false,
  invalidStart = false,
  waitForever = false,
} = {}) {
  const log = [];
  const handles = new Map();
  const actors = new Map(bindings.actors.map((entry) => [entry.binding_id, entry]));
  const targets = new Map(bindings.entities.map((entry) => [entry.binding_id, entry]));
  return {
    log,
    async preflightAnimation(request) {
      log.push(["preflight", request]);
      const actionEntries = request.requested_actions.map((action) => ({
        action,
        bridge_action_id: ACTION_DEFINITIONS[action].bridge_action_id,
        available: !missingActions.includes(action),
        implementation_matches: !missingActions.includes(action),
        completion_signal_available: !missingActions.includes(action),
      }));
      const runtimeActors = request.actor_binding_ids.map((bindingId) => bindingRuntimeResponse(actors.get(bindingId)));
      const runtimeTargets = request.target_binding_ids.map((bindingId) => {
        const target = targets.get(bindingId);
        const isChair = bindingId === "target_rolling_chair";
        const anchors = [
          ...(target.capabilities.includes("foot_contact_target") ? ["foot_contact"] : []),
          ...(target.capabilities.includes("gaze_target") ? ["gaze_target"] : []),
          ...(target.capabilities.includes("hand_contact_target") ? ["hand_contact"] : []),
        ];
        return bindingRuntimeResponse(target, {
          anchors: isChair && missingChairAnchors ? ["gaze_target"] : anchors,
        });
      });
      const ready = runtimeActors.every((entry) => entry.available && entry.class_matches && entry.skeleton_matches)
        && runtimeTargets.every((entry) => entry.available && entry.class_matches)
        && actionEntries.every((entry) => entry.available && entry.implementation_matches && entry.completion_signal_available);
      return {
        schema: ANIMATION_PREFLIGHT_RESPONSE_SCHEMA,
        scene_revision: request.scene_revision,
        profile_revision: request.profile_revision,
        content_revision: request.content_revision,
        content_digest: request.content_digest,
        ready,
        actors: runtimeActors,
        targets: runtimeTargets,
        actions: actionEntries,
      };
    },
    async snapshotAnimationState(request) {
      log.push(["snapshot", request]);
      return {
        schema: SNAPSHOT_RESPONSE_SCHEMA,
        status: "captured",
        snapshot_id: `snapshot:${request.event_id}`,
        action: request.action,
        actor_binding_id: request.actor_binding_id,
        target_binding_id: request.target_binding_id,
        engine_time: 1,
        state_digest: STATE_DIGEST,
      };
    },
    async startAnimationAction(request) {
      log.push(["start", request]);
      const handle = `handle:${request.event_id}`;
      const action = Object.entries(ACTION_DEFINITIONS)
        .find(([, definition]) => definition.bridge_action_id === request.bridge_action_id)[0];
      handles.set(handle, action);
      return {
        schema: START_RESPONSE_SCHEMA,
        status: "started",
        action_handle: handle,
        bridge_action_id: invalidStart ? "vista_invalid_start_v1" : request.bridge_action_id,
        actor_binding_id: request.actor_binding_id,
        target_binding_id: request.target_binding_id,
        engine_time: 1.1,
      };
    },
    async waitAnimationAction(request) {
      log.push(["wait", request]);
      if (waitForever) return new Promise(() => {});
      const action = handles.get(request.action_handle);
      return {
        schema: WAIT_RESPONSE_SCHEMA,
        status: "completed",
        action_handle: request.action_handle,
        completion_signal: ACTION_DEFINITIONS[action].completion_signal || `vista_${action}_complete`,
        engine_time: 1.5,
        evidence_ids: [`broker:${request.event_id}`],
      };
    },
    async stopAnimationAction(request) {
      log.push(["stop", request]);
      return {
        schema: STOP_RESPONSE_SCHEMA,
        status: "stopped",
        action_handle: request.action_handle,
        engine_time: 1.6,
      };
    },
    async releaseAnimationAction(request) {
      log.push(["release", request]);
      return {
        schema: RELEASE_RESPONSE_SCHEMA,
        status: "released",
        action_handle: request.action_handle,
      };
    },
    async restoreAnimationState(request) {
      log.push(["restore", request]);
      return {
        schema: RESTORE_RESPONSE_SCHEMA,
        status: "restored",
        snapshot_id: request.snapshot_id,
        state_digest: request.state_digest,
        engine_time: 1.7,
      };
    },
  };
}

function makeEvidenceHooks({ interactionAssertion = "pass" } = {}) {
  let sequence = 0;
  const make = (kind, assertion) => async (context) => {
    sequence += 1;
    return {
      evidence_id: `evidence:${kind}:${String(sequence).padStart(4, "0")}`,
      artifact_ref: `animation/${context.run_id}/${kind}-${String(sequence).padStart(4, "0")}.json`,
      sha256: String(sequence % 10).repeat(64),
      assertion,
    };
  };
  return {
    pose_snapshot: make("pose_snapshot", null),
    interaction_state: make("interaction_state", interactionAssertion),
    screenshot: make("screenshot", null),
    scene_validation: make("scene_validation", "pass"),
  };
}

async function prepare({
  actions = ALL_ACTIONS,
  profileActions = actions,
  bindings = makeBindings(),
  brokerOptions = {},
  evidenceOptions = {},
  sceneRevision = SCENE_REVISION,
} = {}) {
  const broker = makeFakeBroker({ bindings, ...brokerOptions });
  const runtime = createVistaAnimationRuntime({
    broker,
    contentProfile: makeProfile(profileActions),
    evidenceHooks: makeEvidenceHooks(evidenceOptions),
    clock: makeIsoClock(),
    fps: 30,
  });
  const prepared = await runtime.preflight({
    sceneRevision,
    bindings,
    requestedActions: actions,
  });
  return { broker, runtime, prepared, bindings };
}

function compile(scene, bindings, prepared) {
  return compileVistaTimeline(scene, {
    policy: "strict",
    bindings,
    capabilityRegistry: prepared.capabilityRegistry,
  });
}

function createScheduler(registry, clock) {
  return createVistaTimelineScheduler({
    capabilityRegistry: registry,
    clock,
    wallClock: () => new Date(Date.UTC(2026, 6, 21) + clock.now()).toISOString(),
    idFactory: () => "animation_run_001",
    engineTimeSampler: async () => clock.now() / 1000,
    hookTimeoutMs: 1000,
  });
}

function startRequest(timeline) {
  return {
    timeline,
    ownerId: "owner:animation-tests",
    sessionId: "session:animation-tests",
    slotId: "slot:0",
    correlationId: "corr:animation:001",
  };
}

test("animation schemas and fixed action allowlist are versioned and closed", () => {
  const expected = [
    ["vista-animation-content-profile-v1.schema.json", "vista-animation-content-profile/v1"],
    ["vista-animation-preflight-v1.schema.json", "vista-animation-preflight/v1"],
    ["vista-animation-program-v1.schema.json", "vista-animation-program/v1"],
    ["vista-animation-evidence-v1.schema.json", "vista-animation-evidence/v1"],
  ];
  for (const [filename, schema] of expected) {
    const value = JSON.parse(fs.readFileSync(path.join(SCHEMA_ROOT, filename), "utf8"));
    assert.equal(value.type, "object");
    assert.equal(value.additionalProperties, false);
    assert.equal(value.properties.schema.const, schema);
  }
  assert.deepEqual(Object.keys(ACTION_DEFINITIONS).sort(), ["brace", "drag", "fall", "lift_foot", "look_at", "pause", "pick_up", "recover"]);
  assert.equal(ACTION_DEFINITIONS.brace.anchor_kinds.includes("hand_contact"), true);
  assert.equal(ACTION_DEFINITIONS.lift_foot.anchor_kinds.includes("foot_contact"), true);
  assert.deepEqual(ACTION_DEFINITIONS.pick_up, {
    adapter_id: "vista_pick_up_ik_v1",
    bridge_action_id: "vista_pick_up_ik_v1",
    actor_kinds: ["player"],
    target_policy: "required",
    target_kinds: ["prop"],
    actor_capabilities: ["upper_body_ik", "object_attachment"],
    target_capabilities: ["pickupable", "hand_contact_target"],
    anchor_kinds: ["hand_contact"],
    completion_signal: "vista_pick_up_attached",
    defaults: { hand: "right", duration_sec: 2 },
  });
  const contentSchema = JSON.parse(fs.readFileSync(path.join(SCHEMA_ROOT, "vista-animation-content-profile-v1.schema.json"), "utf8"));
  const programSchema = JSON.parse(fs.readFileSync(path.join(SCHEMA_ROOT, "vista-animation-program-v1.schema.json"), "utf8"));
  const preflightSchema = JSON.parse(fs.readFileSync(path.join(SCHEMA_ROOT, "vista-animation-preflight-v1.schema.json"), "utf8"));
  const evidenceSchema = JSON.parse(fs.readFileSync(path.join(SCHEMA_ROOT, "vista-animation-evidence-v1.schema.json"), "utf8"));
  assert.equal(contentSchema.properties.actions.maxItems, 8);
  assert.equal(contentSchema.$defs.action.properties.action.enum.includes("pick_up"), true);
  assert.equal(programSchema.$defs.event.properties.action.enum.includes("pick_up"), true);
  assert.equal(preflightSchema.$defs.actionName.enum.includes("pick_up"), true);
  assert.equal(preflightSchema.$defs.actionList.maxItems, 8);
  assert.equal(evidenceSchema.$defs.checkpoint.properties.action.oneOf[1].enum.includes("pick_up"), true);
});

test("content profile rejects unverified receipts, arbitrary bridge commands, and noncanonical assets", () => {
  assert.throws(() => validateContentProfile({
    ...makeProfile(["brace"]),
    verification: { ...makeProfile(["brace"]).verification, status: "pending" },
  }), (error) => error instanceof VistaAnimationContractError && error.code === "ANIMATION_CONTENT_PROFILE_INVALID");

  const arbitrary = makeProfile(["brace"]);
  arbitrary.actions[0].bridge_action_id = "vbp_arbitrary_command";
  assert.throws(() => validateContentProfile(arbitrary), (error) => error.code === "ANIMATION_CONTENT_PROFILE_INVALID");

  const traversal = makeProfile(["brace"]);
  traversal.actions[0].implementation_asset = "/Game/VISTA/../Private.Secret";
  assert.throws(() => validateContentProfile(traversal), (error) => error.code === "ANIMATION_CONTENT_PROFILE_INVALID");

  const wrongPickUpCompletion = makeProfile(["pick_up"]);
  wrongPickUpCompletion.actions[0].completion_signal = "vista_pick_up_complete";
  assert.throws(
    () => validateContentProfile(wrongPickUpCompletion),
    (error) => error.code === "ANIMATION_CONTENT_PROFILE_INVALID",
  );
});

test("counterfactual 0/2/5/9 pick-up and fall fixture compiles through the 12-second terminal checkpoint", async () => {
  const actions = ["look_at", "pick_up", "pause", "fall"];
  const bindings = makeBindings({
    actorCapabilities: [
      "fall_montage",
      "gaze",
      "hold_pose",
      "object_attachment",
      "root_motion",
      "upper_body_ik",
    ],
  });
  bindings.entities[0].capabilities.push("hand_contact_target", "pickupable");
  const scene = JSON.parse(fs.readFileSync(COUNTERFACTUAL_PICK_UP_FALL_SCENE, "utf8"));
  const { broker, runtime, prepared } = await prepare({
    actions,
    profileActions: actions,
    bindings,
    sceneRevision: scene.scene_id,
  });

  const timeline = compile(scene, bindings, prepared);
  const program = runtime.compileProgram(timeline, prepared.artifact);
  assert.equal(timeline.start_allowed, true);
  assert.deepEqual(program.events.map((event) => event.at_sec), [0, 2, 5, 9]);
  assert.deepEqual(program.events.map((event) => event.action), actions);
  const pickUp = program.events[1];
  assert.equal(pickUp.target_binding_id, "target_high_box");
  assert.equal(pickUp.adapter_id, "vista_pick_up_ik_v1");
  assert.equal(pickUp.bridge_action_id, "vista_pick_up_ik_v1");
  assert.deepEqual(pickUp.parameters, { duration_sec: 2, hand: "right" });
  assert.equal(prepared.artifact.actions.find((entry) => entry.action === "pick_up").completion_signal, "vista_pick_up_attached");
  assert.deepEqual(program.checkpoints.at(-1), {
    checkpoint_id: program.checkpoints.at(-1).checkpoint_id,
    kind: "terminal",
    event_id: null,
    at_sec: 12,
    at_frame: 360,
    frame_order: 0,
  });
  assert.deepEqual(broker.log.map(([method]) => method), ["preflight"], "compile-only fixture must not claim a live mutation");

  const missingTarget = JSON.parse(JSON.stringify(scene));
  missingTarget.timeline[1].target_id = null;
  const blockedTarget = compile(missingTarget, bindings, prepared);
  assert.equal(blockedTarget.start_allowed, false);
  assert.equal(blockedTarget.events[1].preflight_status, "incompatible");
  assert.throws(
    () => runtime.compileProgram(blockedTarget, prepared.artifact),
    (error) => error.code === "ANIMATION_PROGRAM_START_BLOCKED",
  );

  const unresolvedAction = JSON.parse(JSON.stringify(scene));
  unresolvedAction.timeline[1].action = "unresolved_action";
  const blockedUnknown = compile(unresolvedAction, bindings, prepared);
  assert.equal(blockedUnknown.start_allowed, false);
  assert.equal(blockedUnknown.events[1].preflight_status, "unsupported");
  assert.throws(
    () => runtime.compileProgram(blockedUnknown, prepared.artifact),
    (error) => error.code === "ANIMATION_PROGRAM_START_BLOCKED",
  );
});

test("live preflight registers only verified fixed adapters and fails closed on missing fall/recover content", async () => {
  const bindings = makeBindings();
  const { prepared } = await prepare({
    actions: ["look_at", "fall", "recover"],
    profileActions: ["look_at"],
    bindings,
  });

  assert.equal(prepared.artifact.start_allowed, false);
  assert.deepEqual(prepared.artifact.supported_actions, ["look_at"]);
  assert.deepEqual(prepared.capabilityRegistry.adapters.map((entry) => entry.action), ["look_at"]);
  assert.deepEqual(
    prepared.artifact.issues.filter((entry) => entry.code === "ACTION_CONTENT_UNVERIFIED").map((entry) => entry.subject),
    ["fall", "recover"],
  );

  const timeline = compile(makeScene(["look_at", "fall", "recover"]), bindings, prepared);
  assert.equal(timeline.start_allowed, false);
  assert.deepEqual(timeline.events.map((event) => event.preflight_status), ["ready", "unsupported", "unsupported"]);
  assert.throws(
    () => compileVistaAnimationProgram(timeline, prepared.artifact),
    (error) => error instanceof VistaAnimationContractError && error.code === "ANIMATION_PROGRAM_START_BLOCKED",
  );
});

test("brace/drag/lift-foot preflight blocks missing IK anchors before any action mutation", async () => {
  const bindings = makeBindings();
  const { broker, runtime, prepared } = await prepare({
    actions: ["brace", "drag", "lift_foot"],
    profileActions: ["brace", "drag", "lift_foot"],
    bindings,
    brokerOptions: { missingChairAnchors: true },
  });

  assert.equal(prepared.artifact.start_allowed, false);
  assert.ok(prepared.artifact.issues.some((entry) => entry.code === "TARGET_CAPABILITY_UNVERIFIED" && entry.subject === "target_rolling_chair"));
  const timeline = compile(makeScene(["drag", "brace", "lift_foot"]), bindings, prepared);
  assert.equal(timeline.start_allowed, true, "semantic compiler stays separate from live content proof");
  assert.throws(() => runtime.compileProgram(timeline, prepared.artifact), (error) => error.code === "ANIMATION_PROGRAM_CAPABILITY_BLOCKED");
  const clock = new FakeMonotonicClock();
  const scheduler = createScheduler(prepared.capabilityRegistry, clock);
  const initial = scheduler.start(startRequest(timeline));
  await flushTurns();
  clock.advanceTo(2000);
  await flushTurns(8);
  const run = await scheduler.waitForRun({
    runId: initial.run_id,
    ownerId: "owner:animation-tests",
    sessionId: "session:animation-tests",
  });
  assert.equal(run.state, "failed");
  assert.match(run.events[0].error, /preflight does not allow runtime mutation/);
  assert.deepEqual(broker.log.map(([method]) => method), ["preflight"]);
});

test("12-second program has deterministic frame/time ordering including same-frame IK beats and terminal frame", async () => {
  const { runtime, prepared, bindings } = await prepare();
  const timeline = compile(makeScene(), bindings, prepared);
  const first = runtime.compileProgram(timeline, prepared.artifact);
  const second = runtime.compileProgram(timeline, prepared.artifact);

  assert.equal(first.program_id, second.program_id);
  assert.equal(JSON.stringify(first), JSON.stringify(second));
  assert.deepEqual(first.events.map((event) => event.at_sec), [0, 2, 5, 5, 9, 10, 11]);
  assert.deepEqual(first.events.map((event) => event.at_frame), [0, 60, 150, 150, 270, 300, 330]);
  assert.deepEqual(first.events.map((event) => event.frame_order), [0, 0, 0, 1, 0, 0, 0]);
  assert.deepEqual(first.events.map((event) => event.action), ALL_ACTIONS);
  assert.deepEqual(first.checkpoints.at(-1), {
    checkpoint_id: first.checkpoints.at(-1).checkpoint_id,
    kind: "terminal",
    event_id: null,
    at_sec: 12,
    at_frame: 360,
    frame_order: 0,
  });
  assert.equal(first.events.find((event) => event.action === "brace").parameters.hand, "both");
  assert.equal(first.events.find((event) => event.action === "lift_foot").parameters.height_cm, 35);
  assert.equal(validateVistaAnimationProgram(first), first);
  assert.throws(() => runtime.compileProgram(timeline, prepared.artifact, { fps: 60 }), (error) => error instanceof VistaAnimationRuntimeError && error.code === "ANIMATION_PROGRAM_FPS_MISMATCH");
  const otherScene = await runtime.preflight({
    sceneRevision: "mmg_041@bbbbbbbbbbbbbbbb",
    bindings,
    requestedActions: ALL_ACTIONS,
  });
  assert.notEqual(otherScene.artifact.registry_revision, prepared.artifact.registry_revision, "capability registries are scene/content bound");
});

test("fake broker executes fixed adapters, proves IK/contact evidence, and emits a complete frame-ordered manifest", async () => {
  const { broker, runtime, prepared, bindings } = await prepare();
  const timeline = compile(makeScene(), bindings, prepared);
  const program = runtime.compileProgram(timeline, prepared.artifact);
  const clock = new FakeMonotonicClock();
  const scheduler = createScheduler(prepared.capabilityRegistry, clock);
  const initial = scheduler.start(startRequest(timeline));

  await flushTurns();
  for (const atMs of [2000, 5000, 9000, 10_000, 11_000, 12_000]) {
    clock.advanceTo(atMs);
    await flushTurns(8);
  }
  const run = await scheduler.waitForRun({
    runId: initial.run_id,
    ownerId: "owner:animation-tests",
    sessionId: "session:animation-tests",
  });
  assert.equal(run.state, "completed", JSON.stringify(run.events.map((event) => ({ event_id: event.event_id, state: event.state, error: event.error }))));
  assert.deepEqual(run.events.map((event) => event.state), ALL_ACTIONS.map(() => "completed"));

  const manifest = await runtime.finalizeEvidence({ program, run });
  assert.equal(manifest.schema, ANIMATION_EVIDENCE_SCHEMA);
  assert.equal(manifest.run_state, "completed");
  assert.equal(manifest.coverage.complete, true);
  assert.equal(manifest.coverage.required_event_count, 7);
  assert.equal(manifest.coverage.completed_event_count, 7);
  assert.equal(manifest.checkpoints.length, 15);
  assert.deepEqual(
    manifest.checkpoints.filter((entry) => entry.at_frame === 150).map((entry) => [entry.event_id, entry.frame_order, entry.phase]),
    [
      ["beat-0003", 0, "before"],
      ["beat-0003", 0, "after"],
      ["beat-0004", 1, "before"],
      ["beat-0004", 1, "after"],
    ],
  );
  for (const action of ["drag", "brace", "lift_foot"]) {
    const event = program.events.find((entry) => entry.action === action);
    const after = manifest.checkpoints.find((entry) => entry.event_id === event.event_id && entry.phase === "after");
    assert.equal(after.evidence.find((entry) => entry.kind === "interaction_state").assertion, "pass");
  }
  assert.equal(manifest.checkpoints.at(-1).phase, "terminal");
  assert.equal(manifest.checkpoints.at(-1).at_sec, 12);
  assert.equal(manifest.checkpoints.at(-1).evidence.find((entry) => entry.kind === "scene_validation").assertion, "pass");
  assert.equal(validateAnimationEvidenceManifest(manifest), manifest);
  assert.equal(await runtime.finalizeEvidence({ program, run }), manifest, "evidence finalization is idempotent");

  const startRequests = broker.log.filter(([method]) => method === "start").map(([, request]) => request);
  assert.deepEqual(startRequests.map((request) => request.bridge_action_id), ALL_ACTIONS.map((action) => ACTION_DEFINITIONS[action].bridge_action_id));
  const serialized = JSON.stringify(startRequests);
  assert.equal(serialized.includes("vbp"), false);
  assert.equal(serialized.includes("implementation_asset"), false);
  assert.equal(serialized.includes("/Game/"), false);
});

test("Stop is idempotent and performs stop, transient release, exact snapshot restore, and rollback evidence", async () => {
  const actions = ["look_at"];
  const bindings = makeBindings();
  const { broker, runtime, prepared } = await prepare({
    actions,
    profileActions: actions,
    bindings,
    brokerOptions: { waitForever: true },
  });
  const timeline = compile(makeScene(actions), bindings, prepared);
  const program = runtime.compileProgram(timeline, prepared.artifact);
  const clock = new FakeMonotonicClock();
  const scheduler = createScheduler(prepared.capabilityRegistry, clock);
  const initial = scheduler.start(startRequest(timeline));
  await flushTurns(8);

  const access = {
    runId: initial.run_id,
    ownerId: "owner:animation-tests",
    sessionId: "session:animation-tests",
    reason: "operator stop",
  };
  const firstStop = scheduler.stop(access);
  const secondStop = scheduler.stop(access);
  assert.equal(firstStop, secondStop);
  const run = await firstStop;

  assert.equal(run.state, "cancelled");
  assert.equal(run.events[0].state, "cancelled");
  assert.equal(run.events[0].cleanup_state, "completed");
  assert.equal(run.cleanup.confirmed_stopped, true);
  const lifecycle = broker.log.map(([method]) => method).filter((method) => method !== "preflight");
  assert.deepEqual(lifecycle, ["snapshot", "start", "wait", "stop", "release", "restore"]);
  const restore = broker.log.find(([method]) => method === "restore")[1];
  assert.equal(restore.snapshot_id, "snapshot:beat-0001");
  assert.equal(restore.state_digest, STATE_DIGEST);

  const manifest = await runtime.finalizeEvidence({ program, run });
  assert.equal(manifest.run_state, "cancelled");
  assert.equal(manifest.coverage.complete, false);
  assert.deepEqual(manifest.checkpoints.map((entry) => entry.phase), ["before", "rollback", "terminal"]);
  assert.equal(manifest.checkpoints[1].evidence.some((entry) => entry.kind === "screenshot"), true);
});

test("failed IK assertion aborts completion and compensates from the pre-action snapshot", async () => {
  const actions = ["brace"];
  const bindings = makeBindings();
  const { broker, prepared } = await prepare({
    actions,
    profileActions: actions,
    bindings,
    evidenceOptions: { interactionAssertion: "fail" },
  });
  const timeline = compile(makeScene(actions), bindings, prepared);
  const clock = new FakeMonotonicClock();
  const scheduler = createScheduler(prepared.capabilityRegistry, clock);
  const initial = scheduler.start(startRequest(timeline));
  await flushTurns();
  clock.advanceTo(5000);
  await flushTurns(10);
  const run = await scheduler.waitForRun({
    runId: initial.run_id,
    ownerId: "owner:animation-tests",
    sessionId: "session:animation-tests",
  });

  assert.equal(run.state, "failed");
  assert.equal(run.events[0].state, "failed");
  assert.match(run.events[0].error, /did not prove its IK\/object interaction/);
  assert.equal(run.events[0].cleanup_state, "completed");
  assert.deepEqual(
    broker.log.map(([method]) => method).filter((method) => method !== "preflight"),
    ["snapshot", "start", "wait", "stop", "release", "restore"],
  );
});

test("an unverified start response restores the snapshot but never claims cleanup was confirmed", async () => {
  const actions = ["look_at"];
  const bindings = makeBindings();
  const { broker, prepared } = await prepare({
    actions,
    profileActions: actions,
    bindings,
    brokerOptions: { invalidStart: true },
  });
  const timeline = compile(makeScene(actions), bindings, prepared);
  const clock = new FakeMonotonicClock();
  const scheduler = createScheduler(prepared.capabilityRegistry, clock);
  const initial = scheduler.start(startRequest(timeline));
  await flushTurns(10);
  const run = await scheduler.waitForRun({
    runId: initial.run_id,
    ownerId: "owner:animation-tests",
    sessionId: "session:animation-tests",
  });

  assert.equal(run.state, "failed");
  assert.equal(run.events[0].cleanup_state, "failed");
  assert.match(run.events[0].error, /Cleanup failed/);
  assert.equal(run.cleanup.confirmed_stopped, false);
  assert.equal(run.cleanup.state, "partial");
  assert.deepEqual(
    broker.log.map(([method]) => method).filter((method) => method !== "preflight"),
    ["snapshot", "start", "restore"],
  );
});

test("tampered frame order, evidence path, and completed coverage fail validation", async () => {
  const { runtime, prepared, bindings } = await prepare({ actions: ["brace", "lift_foot"], profileActions: ["brace", "lift_foot"] });
  const timeline = compile(makeScene(["brace", "lift_foot"]), bindings, prepared);
  const program = runtime.compileProgram(timeline, prepared.artifact);
  const tampered = JSON.parse(JSON.stringify(program));
  tampered.events[1].frame_order = 0;
  assert.throws(() => validateVistaAnimationProgram(tampered), (error) => error.code === "ANIMATION_PROGRAM_INVALID");

  const invalidEvidence = {
    schema: ANIMATION_EVIDENCE_SCHEMA,
    manifest_id: `vae-${"d".repeat(24)}`,
    program_id: program.program_id,
    run_id: "vtr-animation_run_001",
    timeline_id: program.timeline_id,
    scene_revision: program.scene_revision,
    content_revision: program.content_revision,
    fps: 30,
    duration_sec: 12,
    run_state: "completed",
    created_at: "2026-07-21T00:00:00.000Z",
    finalized_at: "2026-07-21T00:00:01.000Z",
    run_digest: "e".repeat(64),
    checkpoints: [{
      checkpoint_id: `vek-${"f".repeat(20)}`,
      phase: "terminal",
      event_id: null,
      action: null,
      attempt: 0,
      at_sec: 12,
      at_frame: 360,
      frame_order: 0,
      captured_at: "2026-07-21T00:00:01.000Z",
      evidence: [{
        kind: "screenshot",
        evidence_id: "evidence:screenshot:bad",
        artifact_ref: "../secret.png",
        sha256: "f".repeat(64),
        assertion: null,
      }],
    }],
    coverage: {
      required_event_count: 2,
      completed_event_count: 2,
      checkpoint_count: 1,
      missing: ["beat-0001:after"],
      complete: false,
    },
  };
  assert.throws(() => validateAnimationEvidenceManifest(invalidEvidence), (error) => error.code === "ANIMATION_EVIDENCE_INVALID" || error.code === "ANIMATION_EVIDENCE_MANIFEST_INVALID");
});
