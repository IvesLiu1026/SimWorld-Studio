"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  ACTION_DEFINITIONS,
  ANIMATION_CONTENT_PROFILE_SCHEMA,
  ANIMATION_PREFLIGHT_RESPONSE_SCHEMA,
  digest,
} = require("../vista-animation-contract");
const {
  ANIMATION_EVIDENCE_SCHEMA,
  RELEASE_RESPONSE_SCHEMA,
  RESTORE_RESPONSE_SCHEMA,
  SNAPSHOT_RESPONSE_SCHEMA,
  START_RESPONSE_SCHEMA,
  STOP_RESPONSE_SCHEMA,
  WAIT_RESPONSE_SCHEMA,
  createVistaAnimationRuntime,
} = require("../vista-animation-runtime");
const { compileVistaSceneBuildPlan } = require("../vista-scene-build-plan");
const {
  ANIMATION_PREFLIGHT_SERVICE_SCHEMA,
  ANIMATION_RUN_RECORD_SCHEMA,
  ANIMATION_START_SERVICE_SCHEMA,
  ANIMATION_STATUS_SERVICE_SCHEMA,
  VistaAnimationTimelineServiceError,
  createVistaAnimationTimelineService,
} = require("../vista-animation-timeline-service");
const { createVistaImporter, validateSceneSpec } = require("../vista-importer");
const { BINDINGS_SCHEMA } = require("../vista-timeline-compiler");

const FIXTURE_ROOT = path.resolve(__dirname, "fixtures/vista/mmg_040");
const OWNER = "owner-animation-test";
const IMPORT_ID = `vim_${"a".repeat(64)}`;
const ACCESS = Object.freeze({
  ownerId: OWNER,
  sessionId: "session-animation-test",
  leaseId: "lease-animation-test",
  slotId: 3,
  mcpPort: 55565,
});
const CHECKED_AT = "2026-07-21T00:00:00.000Z";
const CONTENT_DIGEST = "b".repeat(64);

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

async function flushTurns(count = 8) {
  for (let index = 0; index < count; index += 1) await flush();
}

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

function profile() {
  const actions = ["look_at", "drag", "brace", "lift_foot", "pause", "fall", "recover"];
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
      verified_at: CHECKED_AT,
    },
    actions: actions.map((action) => ({
      action,
      adapter_id: ACTION_DEFINITIONS[action].adapter_id,
      version: "1.0.0",
      bridge_action_id: ACTION_DEFINITIONS[action].bridge_action_id,
      implementation_asset: `/Game/VISTA/Animations/ABP_${action}.ABP_${action}`,
      completion_signal: `vista_${action}_complete`,
      timeout_ms: 5000,
    })),
  };
}

function pin(layout, entityId) {
  const known = {
    wheeled_office_chair_with_visible_casters: {
      asset_id: "citydb-office-chair-b",
      ue_path: "/Game/CityDatabase/meshes/SM_chair_b.SM_chair_b",
    },
    stable_step_stool: {
      asset_id: "mmg040-step-stool",
      ue_path: "/Game/VISTA/Curated/MMG040/SM_StepStool.SM_StepStool",
    },
    camera_wearer_hands_and_forearms: {
      asset_id: "human-avatar-third-person-character",
      ue_path: "/Game/Human_Avatar/DefaultCharacter/ThirdPerson/Blueprints/BP_ThirdPersonCharacter.BP_ThirdPersonCharacter_C",
    },
  };
  const selected = known[entityId]
    || layout.placements.find((item) => item.source_entity_id === entityId && item.asset_pin).asset_pin;
  return {
    snapshot_id: layout.asset_snapshot_id,
    asset_id: selected.asset_id,
    ue_path: selected.ue_path,
    confidence: 1,
  };
}

let cachedSceneAndPlan;
async function sceneAndPlan() {
  if (cachedSceneAndPlan) return clone(cachedSceneAndPlan);
  const layout = JSON.parse(fs.readFileSync(path.join(FIXTURE_ROOT, "build-layout.v1.json"), "utf8"));
  const importer = createVistaImporter({
    registry: { round1_reviewed_latest: { root: FIXTURE_ROOT } },
  });
  const scene = clone(await importer.preview({
    datasetRevision: "round1_reviewed_latest",
    sampleId: "mmg_040",
    attempt: 7,
    scenarioType: "multimodal_grounded",
  }));
  scene.entities = scene.entities.map((entity) => {
    const binding = pin(layout, entity.id);
    return {
      ...entity,
      asset_binding: binding,
      asset_resolution: {
        schema: "vista-asset-resolution/v1",
        snapshot_id: layout.asset_snapshot_id,
        query: entity.semantic_query,
        min_confidence: 0,
        candidates: [{ rank: 1, ...binding, origin: "manual_override" }],
        selected_binding: binding,
        selected_by: "manual_override",
        manual_override: {
          confirmed: true,
          reason: "Verified animation service fixture",
          ...binding,
        },
      },
    };
  });
  scene.unresolved = scene.unresolved.filter((item) => item.kind !== "asset");
  const validated = validateSceneSpec(scene);
  const plan = compileVistaSceneBuildPlan(validated, layout);
  cachedSceneAndPlan = { scene: validated, plan, layout };
  return clone(cachedSceneAndPlan);
}

function sceneBuildResult(plan) {
  return {
    schema: "vista-scene-build-result/v1",
    build_id: `vsb-${plan.plan_id.slice(4)}`,
    plan_id: plan.plan_id,
    scene_id: plan.scene_id,
    status: "succeeded",
    actor_manifest: plan.actors.map((actor, index) => ({
      ...clone(actor),
      operation_id: `vso-${"d".repeat(16)}-${String(index).padStart(4, "0")}`,
      runtime_actor_name: actor.actor_name,
      object_guid: `aaaaaaaa-bbbb-cccc-dddd-${String(index).padStart(12, "0")}`,
      disposition: "spawned",
    })),
    player_start: {
      ...clone(plan.player_start),
      disposition: "updated",
      applied: { transform: true, pawn_class: false },
      pawn_application_status: "not_applied",
      pawn_application_reason: "animation_runtime_required",
    },
    evidence: plan.evidence_requests.map((request) => ({
      evidence_id: request.evidence_id,
      kind: request.kind,
      required: request.required,
      status: "captured",
      artifact: { artifact_ref: `scene-build/${request.evidence_id}.json` },
      error: null,
    })),
    rollback: {
      state: "not_required",
      deleted_actor_names: [],
      restored_player_start: false,
      failures: [],
    },
  };
}

function bindingsFor(scene) {
  return {
    schema: BINDINGS_SCHEMA,
    revision: "mmg040_verified_animation_bindings_v1",
    actors: [{
      source_id: "camera_wearer",
      binding_id: "actor_camera_wearer",
      kind: "player",
      capabilities: ["gaze", "hold_pose", "lower_body_ik", "root_motion", "upper_body_ik"],
    }],
    entities: scene.entities.map((entity, index) => ({
      source_id: entity.id,
      binding_id: `target_${String(index + 1).padStart(3, "0")}`,
      kind: "prop",
      capabilities: ["draggable", "foot_contact_target", "gaze_target", "hand_contact_target"],
    })),
  };
}

function fakeBroker(bindings, contentProfile) {
  const actors = new Map(bindings.actors.map((entry) => [entry.binding_id, entry]));
  const entities = new Map(bindings.entities.map((entry) => [entry.binding_id, entry]));
  const completionByHandle = new Map();
  let engineTime = 1;
  return {
    async preflightAnimation(request) {
      const runtimeBinding = (binding, target) => ({
        binding_id: binding.binding_id,
        available: true,
        class_matches: true,
        skeleton_matches: true,
        capabilities: [...binding.capabilities],
        anchor_kinds: target ? ["foot_contact", "gaze_target", "hand_contact"] : [],
      });
      return {
        schema: ANIMATION_PREFLIGHT_RESPONSE_SCHEMA,
        scene_revision: request.scene_revision,
        profile_revision: request.profile_revision,
        content_revision: request.content_revision,
        content_digest: request.content_digest,
        ready: true,
        actors: request.actor_binding_ids.map((id) => runtimeBinding(actors.get(id), false)),
        targets: request.target_binding_ids.map((id) => runtimeBinding(entities.get(id), true)),
        actions: request.requested_actions.map((action) => ({
          action,
          bridge_action_id: ACTION_DEFINITIONS[action].bridge_action_id,
          available: true,
          implementation_matches: true,
          completion_signal_available: true,
        })),
      };
    },
    async snapshotAnimationState(request) {
      return {
        schema: SNAPSHOT_RESPONSE_SCHEMA,
        status: "captured",
        snapshot_id: `snapshot:${request.event_id}`,
        action: request.action,
        actor_binding_id: request.actor_binding_id,
        target_binding_id: request.target_binding_id,
        engine_time: engineTime++,
        state_digest: "c".repeat(64),
      };
    },
    async startAnimationAction(request) {
      const action = Object.keys(ACTION_DEFINITIONS)
        .find((name) => ACTION_DEFINITIONS[name].bridge_action_id === request.bridge_action_id);
      const handle = `handle:${request.event_id}`;
      completionByHandle.set(handle, contentProfile.actions.find((entry) => entry.action === action).completion_signal);
      return {
        schema: START_RESPONSE_SCHEMA,
        status: "started",
        action_handle: handle,
        bridge_action_id: request.bridge_action_id,
        actor_binding_id: request.actor_binding_id,
        target_binding_id: request.target_binding_id,
        engine_time: engineTime++,
      };
    },
    async waitAnimationAction(request) {
      return {
        schema: WAIT_RESPONSE_SCHEMA,
        status: "completed",
        action_handle: request.action_handle,
        completion_signal: completionByHandle.get(request.action_handle),
        engine_time: engineTime++,
        evidence_ids: [],
      };
    },
    async stopAnimationAction(request) {
      return { schema: STOP_RESPONSE_SCHEMA, status: "stopped", action_handle: request.action_handle, engine_time: engineTime++ };
    },
    async releaseAnimationAction(request) {
      return { schema: RELEASE_RESPONSE_SCHEMA, status: "released", action_handle: request.action_handle };
    },
    async restoreAnimationState(request) {
      return {
        schema: RESTORE_RESPONSE_SCHEMA,
        status: "restored",
        snapshot_id: request.snapshot_id,
        state_digest: request.state_digest,
        engine_time: engineTime++,
      };
    },
  };
}

function evidenceHooks() {
  const hook = (kind) => async (context) => ({
    evidence_id: `${kind}:${context.run_id}:${context.event_id || "terminal"}:${context.phase}`,
    artifact_ref: `animation/${context.run_id}/${kind}-${context.event_id || "terminal"}-${context.phase}.json`,
    sha256: crypto.createHash("sha256").update(`${kind}:${JSON.stringify(context)}`).digest("hex"),
    assertion: new Set(["interaction_state", "scene_validation"]).has(kind) ? "pass" : null,
  });
  return {
    pose_snapshot: hook("pose_snapshot"),
    interaction_state: hook("interaction_state"),
    screenshot: hook("screenshot"),
    scene_validation: hook("scene_validation"),
  };
}

function readiness(identity, sceneRevision, contentProfile, checkedAt = CHECKED_AT, changes = {}) {
  return {
    status: "ready",
    revision: {
      plugin_name: "VistaAnimationContentApi",
      plugin_version: "1.0.0",
      plugin_build_id: "plugin-build-test",
      binary_sha256: "e".repeat(64),
      engine_version: "5.8.0",
      target_platform: "linux",
      api_schema: "vista-animation-ue-content-api/v1",
      profile_id: contentProfile.profile_id,
      profile_revision: contentProfile.revision,
      content_revision: contentProfile.content_revision,
      content_digest: contentProfile.content_digest,
      slot_binding_digest: digest({
        schema: "vista-animation-ue-slot-binding/v1",
        owner_id: identity.ownerId,
        session_id: identity.sessionId,
        slot_id: String(identity.slotId),
        scene_revision: sceneRevision,
      }),
      operation_allowlist_digest: "f".repeat(64),
      process_instance_id: "ue-process-test",
      checked_at: checkedAt,
      verification: "live_plugin_challenge",
      ...changes,
    },
    causes: [],
  };
}

function randomFactory() {
  let value = 0;
  return (size) => {
    value += 1;
    return Buffer.alloc(size, value);
  };
}

async function fixture(t, overrides = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "vista-animation-timeline-service-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const { scene, plan, layout } = await sceneAndPlan();
  const buildResult = sceneBuildResult(plan);
  const contentProfile = profile();
  const bindings = bindingsFor(scene);
  const broker = fakeBroker(bindings, contentProfile);
  const runtime = createVistaAnimationRuntime({
    broker,
    contentProfile,
    evidenceHooks: evidenceHooks(),
    clock: () => CHECKED_AT,
  });
  const providerCalls = [];
  const bindingCalls = [];
  const fakeClock = new FakeMonotonicClock();
  const artifact = {
    schema: "vista-import-artifact/v1",
    artifact_id: IMPORT_ID,
    status: "committed",
    access: { owner_id: OWNER, session_id: "creator-session" },
    scene_spec: scene,
  };
  const buildStatus = {
    schema: "vista-scene-build-plan-response/v1",
    import_artifact_id: IMPORT_ID,
    profile_id: layout.profile_id,
    state: "succeeded",
    plan,
    last_result: buildResult,
    updated_at: CHECKED_AT,
  };
  const service = createVistaAnimationTimelineService({
    importService: overrides.importService || {
      async status(id, access) {
        if (id !== IMPORT_ID) throw Object.assign(new Error("missing"), { code: "VISTA_IMPORT_NOT_FOUND", status: 404 });
        if (access.ownerId !== OWNER) throw Object.assign(new Error("denied"), { code: "VISTA_IMPORT_ACCESS_DENIED", status: 403 });
        return clone(artifact);
      },
    },
    sceneBuildService: overrides.sceneBuildService || {
      async status(id, request, access) {
        assert.equal(id, IMPORT_ID);
        assert.equal(access.ownerId, OWNER);
        if (request.profile_id !== undefined) assert.equal(request.profile_id, layout.profile_id);
        return clone(overrides.buildStatus || buildStatus);
      },
    },
    runtimeProvider: overrides.runtimeProvider || (async (identity) => {
      providerCalls.push(identity);
      return {
        runtime,
        probeReadiness: async () => readiness(
          ACCESS,
          identity.sceneRevision,
          contentProfile,
          overrides.readinessCheckedAt || CHECKED_AT,
          overrides.readinessChanges || {},
        ),
        engineTimeSampler: async () => fakeClock.now() / 1000,
      };
    }),
    bindingResolver: overrides.bindingResolver || (async (context) => {
      bindingCalls.push(context);
      return clone(bindings);
    }),
    recordRoot: path.join(root, "records"),
    clock: () => CHECKED_AT,
    randomBytes: randomFactory(),
    schedulerOptions: { clock: fakeClock, hookTimeoutMs: 1000 },
    preflightTtlMs: 60_000,
  });
  return {
    artifact,
    bindingCalls,
    bindings,
    buildResult,
    buildStatus,
    contentProfile,
    fakeClock,
    layout,
    plan,
    providerCalls,
    root,
    runtime,
    scene,
    service,
  };
}

function confirmation(preflight) {
  return {
    plan_id: preflight.plan_id,
    preflight_id: preflight.preflight_id,
    timeline_id: preflight.timeline_id,
    program_id: preflight.program_id,
    confirm: true,
  };
}

function hasCode(code) {
  return (error) => {
    assert.equal(error && error.code, code);
    return true;
  };
}

async function finishRun(serviceFixture, runId) {
  for (const time of [0, 2000, 5000, 9000, 12000]) {
    serviceFixture.fakeClock.advanceTo(time);
    await flushTurns();
  }
  let status;
  for (let attempt = 0; attempt < 20; attempt += 1) {
    status = await serviceFixture.service.status(IMPORT_ID, runId, ACCESS);
    if (["completed", "failed", "cancelled"].includes(status.status)) break;
    await flushTurns();
  }
  return status;
}

test("preflight derives exact server-owned inputs and preserves compound frame order", async (t) => {
  const current = await fixture(t);
  const result = await current.service.preflight(IMPORT_ID, { plan_id: current.plan.plan_id }, ACCESS);

  assert.equal(result.schema, ANIMATION_PREFLIGHT_SERVICE_SCHEMA);
  assert.equal(result.ready, true);
  assert.equal(result.plan_id, current.plan.plan_id);
  assert.equal(result.scene_id, current.scene.scene_id);
  assert.deepEqual(result.events.map((event) => event.action), ["look_at", "drag", "brace", "lift_foot", "pause"]);
  assert.deepEqual(result.events.map((event) => event.at_sec), [0, 2, 5, 5, 9]);
  assert.deepEqual(result.events.filter((event) => event.at_sec === 5).map((event) => event.frame_order), [0, 1]);
  assert.equal(current.providerCalls.length, 1);
  assert.deepEqual(current.providerCalls[0], {
    ownerId: ACCESS.ownerId,
    sessionId: ACCESS.sessionId,
    slotId: ACCESS.slotId,
    leaseId: ACCESS.leaseId,
    mcpPort: ACCESS.mcpPort,
    sceneRevision: current.scene.scene_id,
    planId: current.plan.plan_id,
  });
  assert.equal(current.bindingCalls.length, 1);
  assert.equal(current.bindingCalls[0].buildPlan.plan_id, current.plan.plan_id);
  assert.equal(current.bindingCalls[0].buildResult.scene_id, current.scene.scene_id);
  const publicJson = JSON.stringify(result);
  assert.equal(publicJson.includes("/Game/"), false);
  assert.equal(publicJson.includes("contentProfile"), false);
  assert.equal(fs.existsSync(path.join(current.root, "records")), false, "preflight is read-only");
});

test("start writes a private pending receipt before execution and finalizes durable evidence", async (t) => {
  const current = await fixture(t);
  const preflight = await current.service.preflight(IMPORT_ID, { plan_id: current.plan.plan_id }, ACCESS);
  const started = await current.service.start(IMPORT_ID, confirmation(preflight), ACCESS);

  assert.equal(started.schema, ANIMATION_START_SERVICE_SCHEMA);
  assert.equal(started.status, "pending");
  assert.equal(started.operation_id, started.run_id);
  assert.equal(started.replay_of, null);
  const ownerHash = crypto.createHash("sha256").update(OWNER).digest("hex").slice(0, 24);
  const recordFile = path.join(current.root, "records", `${started.run_id}-${ownerHash}.json`);
  assert.equal(fs.statSync(recordFile).mode & 0o777, 0o600);
  const pending = JSON.parse(fs.readFileSync(recordFile, "utf8"));
  assert.equal(pending.schema, ANIMATION_RUN_RECORD_SCHEMA);
  assert.equal(pending.status, "pending");
  assert.equal(pending.run, null);
  assert.equal(JSON.stringify(pending).includes(ACCESS.leaseId), false, "raw lease must not be persisted");

  const status = await finishRun(current, started.run_id);
  assert.equal(status.schema, ANIMATION_STATUS_SERVICE_SCHEMA);
  assert.equal(status.status, "completed");
  assert.equal(status.run.state, "completed");
  assert.ok(status.evidence, JSON.stringify(status));
  assert.equal(status.evidence.schema, ANIMATION_EVIDENCE_SCHEMA);
  assert.equal(status.evidence.coverage.complete, true);
  assert.deepEqual(status.run.events.map((event) => event.state), ["completed", "completed", "completed", "completed", "completed"]);
  assert.equal(JSON.stringify(status).includes(OWNER), false, "public status must omit owner/session principals");
  const committed = JSON.parse(fs.readFileSync(recordFile, "utf8"));
  assert.equal(committed.status, "completed");
  assert.equal(committed.operation.state, "terminal");
  assert.equal(committed.evidence.run_id, started.run_id);
});

test("caller commands, stale confirmations, failed builds, and mismatched readiness fail closed", async (t) => {
  const current = await fixture(t);
  await assert.rejects(
    current.service.preflight(IMPORT_ID, { plan_id: current.plan.plan_id, command: "execute_python" }, ACCESS),
    hasCode("ANIMATION_TIMELINE_REQUEST_INVALID"),
  );
  assert.equal(current.providerCalls.length, 0, "invalid caller shape must fail before runtime resolution");

  const preflight = await current.service.preflight(IMPORT_ID, { plan_id: current.plan.plan_id }, ACCESS);
  await assert.rejects(
    current.service.start(IMPORT_ID, { ...confirmation(preflight), timeline_id: `vtl-${"0".repeat(24)}` }, ACCESS),
    hasCode("ANIMATION_PREFLIGHT_STALE"),
  );
  await assert.rejects(
    current.service.start(IMPORT_ID, { ...confirmation(preflight), confirm: false }, ACCESS),
    hasCode("ANIMATION_CONFIRMATION_REQUIRED"),
  );

  const failedBuild = await fixture(t, { buildStatus: { ...current.buildStatus, state: "failed" } });
  await assert.rejects(
    failedBuild.service.preflight(IMPORT_ID, { plan_id: failedBuild.plan.plan_id }, ACCESS),
    hasCode("ANIMATION_SCENE_BUILD_REQUIRED"),
  );
  assert.equal(failedBuild.providerCalls.length, 0);

  const wrongSlot = await fixture(t, { readinessChanges: { slot_binding_digest: "0".repeat(64) } });
  await assert.rejects(
    wrongSlot.service.preflight(IMPORT_ID, { plan_id: wrongSlot.plan.plan_id }, ACCESS),
    hasCode("ANIMATION_RUNTIME_REVISION_MISMATCH"),
  );
  assert.equal(wrongSlot.bindingCalls.length, 0, "readiness must pass before binding/runtime preflight");
});

test("status is owner durable, stop requires the exact lease, and replay requires terminal exact revisions", async (t) => {
  const current = await fixture(t);
  const preflight = await current.service.preflight(IMPORT_ID, { plan_id: current.plan.plan_id }, ACCESS);
  const started = await current.service.start(IMPORT_ID, confirmation(preflight), ACCESS);
  await flushTurns();

  await assert.rejects(
    current.service.replay(IMPORT_ID, started.run_id, confirmation(preflight), ACCESS),
    hasCode("ANIMATION_REPLAY_NOT_READY"),
  );
  await assert.rejects(
    current.service.stop(IMPORT_ID, started.run_id, {}, { ...ACCESS, leaseId: "rotated-lease" }),
    hasCode("ANIMATION_RUN_IDENTITY_STALE"),
  );
  const stopping = await current.service.stop(IMPORT_ID, started.run_id, {}, ACCESS);
  assert.equal(stopping.status, "stopping");
  await flushTurns(16);
  const cancelled = await current.service.status(IMPORT_ID, started.run_id, { ...ACCESS, sessionId: "reattached-session", leaseId: "reattached-lease" });
  assert.ok(["cancelled", "failed"].includes(cancelled.status));

  await assert.rejects(
    current.service.status(IMPORT_ID, started.run_id, { ...ACCESS, ownerId: "different-owner" }),
    hasCode("ANIMATION_RUN_NOT_FOUND"),
  );
  const replayPreflight = await current.service.preflight(IMPORT_ID, { plan_id: current.plan.plan_id }, ACCESS);
  await assert.rejects(
    current.service.replay(IMPORT_ID, started.run_id, { ...confirmation(replayPreflight), program_id: `vag-${"0".repeat(24)}` }, ACCESS),
    hasCode("ANIMATION_REPLAY_REVISION_MISMATCH"),
  );
  const replay = await current.service.replay(IMPORT_ID, started.run_id, confirmation(replayPreflight), ACCESS);
  assert.equal(replay.schema, ANIMATION_START_SERVICE_SCHEMA);
  assert.equal(replay.status, "pending");
  assert.equal(replay.replay_of, started.run_id);
  assert.notEqual(replay.run_id, started.run_id);
});

test("orphaned pending receipts fail closed with an explicit recovery requirement", async (t) => {
  const current = await fixture(t);
  const preflight = await current.service.preflight(IMPORT_ID, { plan_id: current.plan.plan_id }, ACCESS);
  const started = await current.service.start(IMPORT_ID, confirmation(preflight), ACCESS);
  const ownerHash = crypto.createHash("sha256").update(OWNER).digest("hex").slice(0, 24);
  const recordFile = path.join(current.root, "records", `${started.run_id}-${ownerHash}.json`);
  const pending = JSON.parse(fs.readFileSync(recordFile, "utf8"));

  const replacement = createVistaAnimationTimelineService({
    importService: current.service.importService,
    sceneBuildService: current.service.sceneBuildService,
    runtimeProvider: current.service.runtimeProvider,
    bindingResolver: current.service.bindingResolver,
    recordRoot: path.join(current.root, "records"),
    clock: () => CHECKED_AT,
    randomBytes: randomFactory(),
  });
  assert.equal(pending.status, "pending");
  const recovered = await replacement.status(IMPORT_ID, started.run_id, ACCESS);
  assert.equal(recovered.status, "failed");
  assert.deepEqual(recovered.error, { code: "ANIMATION_RUN_RECOVERY_REQUIRED", retryable: false });
});

test("concurrent start requests are serialized per owner slot", async (t) => {
  const current = await fixture(t);
  const preflight = await current.service.preflight(IMPORT_ID, { plan_id: current.plan.plan_id }, ACCESS);
  const results = await Promise.allSettled([
    current.service.start(IMPORT_ID, confirmation(preflight), ACCESS),
    current.service.start(IMPORT_ID, confirmation(preflight), ACCESS),
  ]);
  assert.equal(results.filter((entry) => entry.status === "fulfilled").length, 1);
  const rejection = results.find((entry) => entry.status === "rejected");
  assert.equal(rejection.reason.code, "ANIMATION_SLOT_BUSY");
  const started = results.find((entry) => entry.status === "fulfilled").value;
  await current.service.stop(IMPORT_ID, started.run_id, {}, ACCESS);
  await flushTurns(12);
});

test("tampered records and symlinked record roots are rejected", async (t) => {
  const current = await fixture(t);
  const preflight = await current.service.preflight(IMPORT_ID, { plan_id: current.plan.plan_id }, ACCESS);
  const started = await current.service.start(IMPORT_ID, confirmation(preflight), ACCESS);
  await current.service.stop(IMPORT_ID, started.run_id, {}, ACCESS);
  await flushTurns(12);
  await current.service.status(IMPORT_ID, started.run_id, ACCESS);
  const ownerHash = crypto.createHash("sha256").update(OWNER).digest("hex").slice(0, 24);
  const recordFile = path.join(current.root, "records", `${started.run_id}-${ownerHash}.json`);
  fs.writeFileSync(recordFile, "{not-json\n", "utf8");
  await assert.rejects(
    current.service.status(IMPORT_ID, started.run_id, ACCESS),
    hasCode("ANIMATION_RECORD_CORRUPT"),
  );

  const linked = await fixture(t);
  const linkedPreflight = await linked.service.preflight(IMPORT_ID, { plan_id: linked.plan.plan_id }, ACCESS);
  const outside = path.join(linked.root, "outside-records");
  fs.mkdirSync(outside, { mode: 0o700 });
  fs.symlinkSync(outside, path.join(linked.root, "records"));
  await assert.rejects(
    linked.service.start(IMPORT_ID, confirmation(linkedPreflight), ACCESS),
    hasCode("ANIMATION_STORAGE_UNAVAILABLE"),
  );
  assert.deepEqual(fs.readdirSync(outside), []);
});

test("constructor and access validation reject missing injected trust boundaries", async (t) => {
  const current = await fixture(t);
  assert.throws(() => createVistaAnimationTimelineService({}), TypeError);
  await assert.rejects(
    current.service.preflight(IMPORT_ID, { plan_id: current.plan.plan_id }, {
      ownerId: ACCESS.ownerId,
      sessionId: ACCESS.sessionId,
    }),
    (error) => error instanceof VistaAnimationTimelineServiceError && error.code === "ANIMATION_ACCESS_INVALID",
  );
});
