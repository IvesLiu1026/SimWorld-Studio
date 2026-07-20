"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { createVistaImporter, validateSceneSpec } = require("../vista-importer");
const { compileVistaSceneBuildPlan } = require("../vista-scene-build-plan");
const {
  BUILD_RESULT_SCHEMA,
  PREFLIGHT_RESPONSE_SCHEMA,
  VistaSceneExecutionError,
  createVistaSceneExecutor,
  makeVistaScenePreflightRequest,
} = require("../vista-scene-executor");

const FIXTURE_ROOT = path.resolve(__dirname, "fixtures/vista/mmg_040");
const REQUEST = Object.freeze({
  datasetRevision: "round1_reviewed_latest",
  sampleId: "mmg_040",
  attempt: 7,
  scenarioType: "multimodal_grounded",
});

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function loadProfile() {
  return JSON.parse(fs.readFileSync(path.join(FIXTURE_ROOT, "build-layout.v1.json"), "utf8"));
}

function pinForEntity(profile, entityId) {
  if (entityId === "wheeled_office_chair_with_visible_casters") {
    return {
      snapshot_id: profile.asset_snapshot_id,
      asset_id: "citydb-office-chair-b",
      ue_path: "/Game/CityDatabase/meshes/SM_chair_b.SM_chair_b",
      confidence: 1,
    };
  }
  if (entityId === "stable_step_stool") {
    return {
      snapshot_id: profile.asset_snapshot_id,
      asset_id: "mmg040-step-stool",
      ue_path: "/Game/VISTA/Curated/MMG040/SM_StepStool.SM_StepStool",
      confidence: 1,
    };
  }
  if (entityId === "camera_wearer_hands_and_forearms") {
    return {
      snapshot_id: profile.asset_snapshot_id,
      asset_id: "human-avatar-third-person-character",
      ue_path: "/Game/Human_Avatar/DefaultCharacter/ThirdPerson/Blueprints/BP_ThirdPersonCharacter.BP_ThirdPersonCharacter_C",
      confidence: 1,
    };
  }
  const placement = profile.placements.find((candidate) => candidate.source_entity_id === entityId && candidate.asset_pin);
  return clone(placement.asset_pin);
}

function resolveScene(scene, profile) {
  const output = clone(scene);
  output.entities = output.entities.map((entity) => {
    const pin = pinForEntity(profile, entity.id);
    const binding = {
      snapshot_id: pin.snapshot_id,
      asset_id: pin.asset_id,
      ue_path: pin.ue_path,
      confidence: pin.confidence,
    };
    return {
      ...entity,
      asset_binding: binding,
      asset_resolution: {
        schema: "vista-asset-resolution/v1",
        snapshot_id: pin.snapshot_id,
        query: entity.semantic_query,
        min_confidence: 0,
        candidates: [{ rank: 1, ...binding, origin: "manual_override" }],
        selected_binding: binding,
        selected_by: "manual_override",
        manual_override: {
          confirmed: true,
          reason: "Pinned by the deterministic mmg_040 executor contract test",
          ...binding,
        },
      },
    };
  });
  output.unresolved = output.unresolved.filter((item) => item.kind !== "asset");
  return validateSceneSpec(output);
}

async function loadPlan() {
  const profile = loadProfile();
  const importer = createVistaImporter({
    registry: { round1_reviewed_latest: { root: FIXTURE_ROOT } },
  });
  const scene = resolveScene(await importer.preview(REQUEST), profile);
  return compileVistaSceneBuildPlan(scene, profile);
}

const PREVIOUS_PLAYER_TRANSFORM = Object.freeze({
  location_cm: [-200, -500, 100],
  rotation_deg: [0, 45, 0],
  scale: [1, 1, 1],
});

class FakeSceneBroker {
  constructor(options = {}) {
    this.options = options;
    this.calls = [];
    this.mutations = [];
    this.spawnCount = 0;
  }

  async preflight(request) {
    this.calls.push({ operation: "preflight", request: clone(request) });
    if (this.options.preflightError) throw this.options.preflightError;
    const defaultActorState = this.options.defaultActorState || "absent";
    const actors = request.actors.map((actor) => {
      const state = (this.options.actorStates && this.options.actorStates[actor.actor_name]) || defaultActorState;
      return {
        actor_name: actor.actor_name,
        state,
        actual_fingerprint: state === "exact_match" ? actor.fingerprint : (state === "conflict" ? "vsa-conflicting00000000000000" : null),
      };
    }).reverse();
    const assets = request.assets.map((asset, index) => ({
      asset_key: asset.asset_key,
      available: index === this.options.unavailableAssetIndex ? false : true,
      class_matches: index === this.options.classMismatchIndex ? false : true,
      revision_matches: index === this.options.revisionMismatchIndex ? false : true,
    })).reverse();
    const playerState = this.options.playerState || "needs_update";
    const ok = this.options.ok === undefined
      ? assets.every((asset) => asset.available && asset.class_matches && asset.revision_matches)
        && actors.every((actor) => actor.state !== "conflict")
        && playerState !== "unavailable"
      : this.options.ok;
    return {
      schema: PREFLIGHT_RESPONSE_SCHEMA,
      plan_id: request.plan_id,
      ok,
      assets,
      actors,
      player_start: {
        actor_name: request.player_start.actor_name,
        class_path: request.player_start.class_path,
        state: playerState,
        current_transform: playerState === "unavailable" ? null : clone(PREVIOUS_PLAYER_TRANSFORM),
      },
    };
  }

  async spawnActor(actor) {
    this.spawnCount += 1;
    this.calls.push({ operation: "spawn_actor", actor: clone(actor) });
    this.mutations.push({ operation: "spawn_actor", actor_name: actor.actor_name });
    if (this.spawnCount === this.options.failSpawnAt) throw new Error("injected spawn failure");
    return { status: "success", actor_name: actor.actor_name };
  }

  async deleteActor(actorName) {
    this.calls.push({ operation: "delete_actor", actor_name: actorName });
    this.mutations.push({ operation: "delete_actor", actor_name: actorName });
    if (this.options.failDeleteName === actorName) throw new Error("injected delete failure");
    return { status: "success" };
  }

  async setPlayerStart(input) {
    this.calls.push({ operation: "set_player_start", input: clone(input) });
    this.mutations.push({ operation: "set_player_start", actor_name: input.player_start.actor_name });
    if (this.options.failPlayerStart) throw new Error("injected player start failure");
    return { status: "success" };
  }

  async restorePlayerStart(input) {
    this.calls.push({ operation: "restore_player_start", input: clone(input) });
    this.mutations.push({ operation: "restore_player_start", actor_name: input.actor_name });
    if (this.options.failPlayerRestore) throw new Error("injected player restore failure");
    return { status: "success" };
  }
}

function evidenceHooks(overrides = {}) {
  const calls = [];
  const hook = (kind) => async ({ request, actor_manifest: manifest }) => {
    calls.push(request.evidence_id);
    return { kind, actor_count: manifest.length, artifact_ref: `evidence://${request.evidence_id}` };
  };
  return {
    calls,
    hooks: {
      actor_snapshot: hook("actor_snapshot"),
      screenshot: hook("screenshot"),
      collision_report: hook("collision_report"),
      floating_report: hook("floating_report"),
      ...overrides,
    },
  };
}

function fixedClock() {
  let seconds = 0;
  return () => `2026-07-21T00:00:${String(seconds++).padStart(2, "0")}.000Z`;
}

function makeExecutor(broker, hooks) {
  return createVistaSceneExecutor({ broker, evidenceHooks: hooks, clock: fixedClock() });
}

function expectExecutionError(error, code) {
  assert.ok(error instanceof VistaSceneExecutionError);
  assert.equal(error.code, code);
  return true;
}

test("preflight request exposes only exact pinned assets, fingerprints, and PlayerStart state", async () => {
  const plan = await loadPlan();
  const request = makeVistaScenePreflightRequest(plan);
  assert.equal(request.plan_id, plan.plan_id);
  assert.equal(request.actors.length, plan.actors.length);
  assert.ok(request.actors.every((actor) => /^vsa-[a-f0-9]{24}$/.test(actor.fingerprint)));
  assert.ok(request.assets.every((asset) => asset.ue_path.startsWith("/Game/") || asset.ue_path.startsWith("/Engine/")));
  assert.ok(request.assets.every((asset) => asset.class_path && asset.content_revision && asset.verification_revision));
  assert.equal(request.player_start.actor_name, "PlayerStart");
  assert.ok(Object.isFrozen(request));
});

test("asset, revision, actor, and PlayerStart preflight failures happen before the first mutation", async (t) => {
  const plan = await loadPlan();
  const cases = [
    [{ unavailableAssetIndex: 0 }, "SCENE_BUILD_PREFLIGHT_ASSET_UNAVAILABLE"],
    [{ classMismatchIndex: 0 }, "SCENE_BUILD_PREFLIGHT_CLASS_MISMATCH"],
    [{ revisionMismatchIndex: 0 }, "SCENE_BUILD_PREFLIGHT_REVISION_MISMATCH"],
    [{ defaultActorState: "conflict" }, "SCENE_BUILD_PREFLIGHT_ACTOR_CONFLICT"],
    [{ playerState: "unavailable" }, "SCENE_BUILD_PREFLIGHT_PLAYER_START_UNAVAILABLE"],
  ];
  for (const [brokerOptions, code] of cases) {
    await t.test(code, async () => {
      const broker = new FakeSceneBroker(brokerOptions);
      const evidence = evidenceHooks();
      const executor = makeExecutor(broker, evidence.hooks);
      await assert.rejects(executor.execute(plan), (error) => expectExecutionError(error, code));
      assert.deepEqual(broker.mutations, [], "strict preflight must fail before mutation");
      assert.equal(evidence.calls.length, 0);
    });
  }
});

test("required evidence hook availability is part of fail-before-mutation preflight", async () => {
  const plan = await loadPlan();
  const broker = new FakeSceneBroker();
  const evidence = evidenceHooks();
  delete evidence.hooks.screenshot;
  const executor = makeExecutor(broker, evidence.hooks);

  await assert.rejects(
    executor.execute(plan),
    (error) => expectExecutionError(error, "SCENE_BUILD_EVIDENCE_HOOK_MISSING"),
  );
  assert.deepEqual(broker.mutations, []);
});

test("successful execution preserves full actor policy and emits a complete manifest and evidence", async () => {
  const plan = await loadPlan();
  const broker = new FakeSceneBroker();
  const evidence = evidenceHooks();
  const result = await makeExecutor(broker, evidence.hooks).execute(plan);

  assert.equal(result.schema, BUILD_RESULT_SCHEMA);
  assert.equal(result.status, "succeeded");
  assert.equal(result.plan_id, plan.plan_id);
  assert.equal(result.mutation_count, plan.actors.length + 1);
  assert.equal(result.actor_manifest.length, plan.actors.length);
  assert.ok(result.actor_manifest.every((entry) => entry.disposition === "spawned"));
  assert.ok(result.actor_manifest.every((entry) => entry.asset.ue_path && entry.asset.class_path));
  assert.ok(result.actor_manifest.every((entry) => entry.mobility && entry.collision.mode));
  assert.equal(result.player_start.disposition, "updated");
  assert.equal(result.camera.perspective, "first_person");
  assert.equal(result.evidence.length, 4);
  assert.ok(result.evidence.every((entry) => entry.status === "captured"));
  assert.deepEqual(evidence.calls, plan.evidence_requests.map((request) => request.evidence_id));
  assert.deepEqual(result.rollback, {
    state: "not_required",
    deleted_actor_names: [],
    restored_player_start: false,
    failures: [],
  });
  const spawnCalls = broker.calls.filter((call) => call.operation === "spawn_actor");
  assert.deepEqual(spawnCalls.map((call) => call.actor.actor_name), plan.actors.map((actor) => actor.actor_name));
  assert.deepEqual(spawnCalls[0].actor.collision, plan.actors[0].collision);
  assert.equal(spawnCalls[0].actor.fingerprint, plan.actors[0].fingerprint);
  assert.ok(Object.isFrozen(result));
});

test("exact actor and PlayerStart fingerprints produce a zero-mutation idempotent execution", async () => {
  const plan = await loadPlan();
  const broker = new FakeSceneBroker({ defaultActorState: "exact_match", playerState: "exact_match" });
  const evidence = evidenceHooks();
  const result = await makeExecutor(broker, evidence.hooks).execute(plan);

  assert.equal(result.status, "already_applied");
  assert.equal(result.mutation_count, 0);
  assert.ok(result.actor_manifest.every((entry) => entry.disposition === "reused"));
  assert.equal(result.player_start.disposition, "reused");
  assert.deepEqual(broker.mutations, []);
  assert.equal(result.evidence.length, 4, "idempotent reuse must still refresh evidence");
});

test("spawn failure deletes only newly created actors in reverse order", async () => {
  const plan = await loadPlan();
  const broker = new FakeSceneBroker({ failSpawnAt: 3 });
  const evidence = evidenceHooks();
  let thrown;
  try {
    await makeExecutor(broker, evidence.hooks).execute(plan);
  } catch (error) {
    thrown = error;
  }

  expectExecutionError(thrown, "SCENE_BUILD_ACTOR_SPAWN_FAILED");
  assert.equal(thrown.result.status, "failed");
  assert.equal(thrown.result.rollback.state, "completed");
  const successfulNames = plan.actors.slice(0, 2).map((actor) => actor.actor_name);
  assert.deepEqual(thrown.result.rollback.deleted_actor_names, [...successfulNames].reverse());
  assert.equal(thrown.result.rollback.restored_player_start, false);
  assert.equal(broker.calls.some((call) => call.operation === "set_player_start"), false);
  assert.equal(evidence.calls.length, 0);
});

test("required evidence failure rolls back actors and restores the previous PlayerStart transform", async () => {
  const plan = await loadPlan();
  const broker = new FakeSceneBroker();
  const evidence = evidenceHooks({
    screenshot: async () => { throw new Error("screenshot unavailable"); },
  });
  let thrown;
  try {
    await makeExecutor(broker, evidence.hooks).execute(plan);
  } catch (error) {
    thrown = error;
  }

  expectExecutionError(thrown, "SCENE_BUILD_REQUIRED_EVIDENCE_FAILED");
  assert.equal(thrown.result.rollback.state, "completed");
  assert.deepEqual(
    thrown.result.rollback.deleted_actor_names,
    [...plan.actors].reverse().map((actor) => actor.actor_name),
  );
  assert.equal(thrown.result.rollback.restored_player_start, true);
  const restore = broker.calls.find((call) => call.operation === "restore_player_start");
  assert.deepEqual(restore.input.transform, PREVIOUS_PLAYER_TRANSFORM);
});

test("rollback failures remain explicit evidence instead of hiding the original build error", async () => {
  const plan = await loadPlan();
  const firstActorName = plan.actors[0].actor_name;
  const broker = new FakeSceneBroker({ failSpawnAt: 3, failDeleteName: firstActorName });
  const evidence = evidenceHooks();
  let thrown;
  try {
    await makeExecutor(broker, evidence.hooks).execute(plan);
  } catch (error) {
    thrown = error;
  }

  expectExecutionError(thrown, "SCENE_BUILD_ACTOR_SPAWN_FAILED");
  assert.equal(thrown.result.rollback.state, "partial");
  assert.equal(thrown.result.rollback.failures.length, 1);
  assert.equal(thrown.result.rollback.failures[0].operation, "delete_actor");
  assert.equal(thrown.result.rollback.failures[0].actor_name, firstActorName);
});
