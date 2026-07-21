"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");

const { createVistaImporter, validateSceneSpec } = require("../vista-importer");
const {
  EXECUTION_RESPONSE_SCHEMA,
  PLAN_RESPONSE_SCHEMA,
  PREFLIGHT_RESPONSE_SCHEMA,
  VistaSceneBuildServiceError,
  createVistaSceneBuildService,
} = require("../vista-scene-build-service");
const { createRuntimeMutationArbiter } = require("../runtime-mutation-arbiter");

const FIXTURE_ROOT = path.resolve(__dirname, "fixtures/vista/mmg_040");
const ACCESS = Object.freeze({
  ownerId: "owner-test",
  sessionId: "session-test",
  leaseId: "lease-test",
  slotId: 2,
  mcpPort: 55563,
});

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function profile({ production = true } = {}) {
  const value = JSON.parse(fs.readFileSync(path.join(FIXTURE_ROOT, "build-layout.v1.json"), "utf8"));
  if (production) {
    value.infrastructure[0].asset_pin = {
      snapshot_id: value.asset_snapshot_id,
      asset_id: "mmg040-verified-office-surface",
      ue_path: "/Game/VISTA/Curated/MMG040/SM_OfficeSurface.SM_OfficeSurface",
      confidence: 1,
      verified: true,
    };
  }
  return value;
}

function successfulResult(plan) {
  const actorManifest = plan.actors.map((actor, index) => ({
    ...clone(actor),
    runtime_actor_name: actor.actor_name,
    operation_id: `vso-${crypto.createHash("sha256").update(`${plan.plan_id}:${actor.actor_id}`).digest("hex").slice(0, 24)}`,
    object_guid: `${String(index + 1).padStart(8, "0")}-89ab-cdef-0123-456789abcdef`,
    disposition: "spawned",
  }));
  const sceneDigest = "9".repeat(64);
  const actorSnapshot = actorManifest.map((actor) => ({
    actor_name: actor.actor_name,
    fingerprint: actor.fingerprint,
    operation_id: actor.operation_id,
    object_guid: actor.object_guid,
    class_path: actor.asset.class_path,
    asset_path: actor.asset.ue_path,
    materials: [{
      component: "VerifiedMesh",
      slot_index: 0,
      material_path: "/Game/VISTA/Materials/M_VerifiedPBR.M_VerifiedPBR",
      material_class: "/Script/Engine.MaterialInstanceConstant",
      pbr_eligible: true,
    }],
  }));
  return {
    schema: "vista-scene-build-result/v1",
    plan_id: plan.plan_id,
    scene_id: plan.scene_id,
    status: "succeeded",
    mutation_count: plan.actors.length,
    actor_manifest: actorManifest,
    evidence: [
      {
        kind: "actor_snapshot", required: true, status: "captured", error: null,
        artifact: {
          schema: "vista-scene-actor-snapshot/v1",
          scene_digest: sceneDigest,
          content_receipt: {
            schema: "simworld-ue-content-receipt/v1",
            content_revision: plan.content_revision,
            verification_revision: plan.verification_revision,
            receipt_sha256: "7".repeat(64),
          },
          actors: actorSnapshot,
        },
      },
      {
        kind: "collision_report", required: true, status: "captured", error: null,
        artifact: { schema: "vista-scene-collision-report/v1", scene_digest: sceneDigest, collision_count: 0, collisions: [] },
      },
      {
        kind: "floating_report", required: true, status: "captured", error: null,
        artifact: { schema: "vista-scene-floating-report/v1", scene_digest: sceneDigest, floating_count: 0, floating: [] },
      },
      {
        kind: "screenshot", required: true, status: "captured", error: null,
        artifact: { schema: "vista-scene-screenshot/v1", scene_digest: sceneDigest, sha256: "8".repeat(64), bytes: 33 },
      },
    ],
  };
}

function pin(profileValue, entityId) {
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
    || profileValue.placements.find((item) => item.source_entity_id === entityId && item.asset_pin).asset_pin;
  return {
    snapshot_id: profileValue.asset_snapshot_id,
    asset_id: selected.asset_id,
    ue_path: selected.ue_path,
    confidence: 1,
  };
}

async function resolvedScene(profileValue) {
  const importer = createVistaImporter({
    registry: { round1_reviewed_latest: { root: FIXTURE_ROOT } },
  });
  const source = clone(await importer.preview({
    datasetRevision: "round1_reviewed_latest",
    sampleId: "mmg_040",
    attempt: 7,
    scenarioType: "multimodal_grounded",
  }));
  source.entities = source.entities.map((entity) => {
    const binding = pin(profileValue, entity.id);
    return {
      ...entity,
      asset_binding: binding,
      asset_resolution: {
        schema: "vista-asset-resolution/v1",
        snapshot_id: profileValue.asset_snapshot_id,
        query: entity.semantic_query,
        min_confidence: 0,
        candidates: [{ rank: 1, ...binding, origin: "manual_override" }],
        selected_binding: binding,
        selected_by: "manual_override",
        manual_override: {
          confirmed: true,
          reason: "Verified test fixture binding",
          ...binding,
        },
      },
    };
  });
  source.unresolved = source.unresolved.filter((item) => item.kind !== "asset");
  return validateSceneSpec(source);
}

async function fixture(t, overrides = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "vista-scene-build-service-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const layout = profile();
  const scene = await resolvedScene(layout);
  const artifact = {
    schema: "vista-import-artifact/v1",
    artifact_id: "vim_" + "a".repeat(64),
    status: "committed",
    scene_spec: scene,
  };
  const executorCalls = [];
  const executor = overrides.executor === null ? null : (overrides.executor || {
    async preflight(plan, options) {
      executorCalls.push({ operation: "preflight", plan, options });
      return {
        schema: "vista-scene-build-preflight-result/v1",
        plan_id: plan.plan_id,
        ready: true,
        assets: [],
        actors: [],
        player_start: {},
      };
    },
    async execute(plan, options) {
      executorCalls.push({ operation: "execute", plan, options });
      return successfulResult(plan);
    },
  });
  const importCalls = [];
  const service = createVistaSceneBuildService({
    importService: {
      async status(id, access) {
        importCalls.push({ id, access });
        if (id !== artifact.artifact_id) throw Object.assign(new Error("not found"), { code: "VISTA_IMPORT_NOT_FOUND", status: 404 });
        if (access.ownerId !== ACCESS.ownerId) {
          throw Object.assign(new Error("denied"), { code: "VISTA_IMPORT_ACCESS_DENIED", status: 403 });
        }
        return clone(artifact);
      },
    },
    layoutProfiles: { [layout.profile_id]: layout },
    recordRoot: path.join(root, "records"),
    executor,
    clock: () => new Date("2026-07-21T00:00:00.000Z"),
    randomBytes: () => Buffer.alloc(12, 1),
    ...(overrides.artifactRecorder ? { artifactRecorder: overrides.artifactRecorder } : {}),
    ...(overrides.service || {}),
  });
  return { artifact, executorCalls, importCalls, layout, root, service };
}

function hasCode(code) {
  return (error) => {
    assert.ok(error instanceof VistaSceneBuildServiceError);
    assert.equal(error.code, code);
    return true;
  };
}

test("committed import compiles into one deterministic server-pinned BuildPlan", async (t) => {
  const { artifact, importCalls, layout, root, service } = await fixture(t);
  const first = await service.plan(artifact.artifact_id, {}, ACCESS);
  const second = await service.plan(artifact.artifact_id, { profile_id: layout.profile_id }, ACCESS);

  assert.equal(first.schema, PLAN_RESPONSE_SCHEMA);
  assert.equal(first.state, "planned");
  assert.equal(first.profile_id, layout.profile_id);
  assert.equal(first.plan.plan_id, second.plan.plan_id);
  assert.equal(first.plan.scene_id.startsWith("mmg_040@"), true);
  assert.equal(first.plan.actors.every((actor) => actor.asset.verified === true), true);
  assert.equal(fs.existsSync(path.join(root, "records")), false, "planning is read-only");
  assert.deepEqual(importCalls[0].access, {
    ownerId: ACCESS.ownerId,
    sessionId: ACCESS.sessionId,
  });
});

test("preflight and execute require the exact current plan id and explicit confirmation", async (t) => {
  const { artifact, executorCalls, service } = await fixture(t);
  const planned = await service.plan(artifact.artifact_id, {}, ACCESS);

  await assert.rejects(
    service.preflight(artifact.artifact_id, { plan_id: "vsp-" + "0".repeat(24) }, ACCESS),
    hasCode("SCENE_BUILD_PLAN_STALE"),
  );
  await assert.rejects(
    service.execute(artifact.artifact_id, { plan_id: planned.plan.plan_id, confirm: false }, ACCESS),
    hasCode("SCENE_BUILD_CONFIRMATION_REQUIRED"),
  );
  assert.equal(executorCalls.length, 0);

  const preflight = await service.preflight(artifact.artifact_id, { plan_id: planned.plan.plan_id }, ACCESS);
  assert.equal(preflight.schema, PREFLIGHT_RESPONSE_SCHEMA);
  assert.equal(preflight.ready, true);
  const execution = await service.execute(artifact.artifact_id, {
    plan_id: planned.plan.plan_id,
    confirm: true,
  }, ACCESS);
  assert.equal(execution.schema, EXECUTION_RESPONSE_SCHEMA);
  assert.equal(execution.status, "succeeded");
  assert.deepEqual(executorCalls.map((call) => call.operation), ["preflight", "execute"]);
});

test("execution record is persisted with mode 0600 and remains owner-bound across active sessions", async (t) => {
  const { artifact, root, service } = await fixture(t);
  const planned = await service.plan(artifact.artifact_id, {}, ACCESS);
  await service.execute(artifact.artifact_id, { plan_id: planned.plan.plan_id, confirm: true }, ACCESS);
  const status = await service.status(artifact.artifact_id, {}, ACCESS);

  assert.equal(status.state, "succeeded");
  assert.equal(status.last_result.plan_id, planned.plan.plan_id);
  const ownerDigest = require("node:crypto").createHash("sha256")
    .update(ACCESS.ownerId, "utf8").digest("hex").slice(0, 24);
  const file = path.join(root, "records", `${planned.plan.plan_id}-${ownerDigest}.json`);
  assert.equal(fs.statSync(file).mode & 0o777, 0o600);
  const raw = fs.readFileSync(file, "utf8");
  assert.equal(raw.includes(ACCESS.ownerId), true);
  assert.equal(JSON.stringify(status).includes(ACCESS.ownerId), false, "public response omits access principals");
  fs.chmodSync(file, 0o640);
  await assert.rejects(
    service.status(artifact.artifact_id, {}, ACCESS),
    (error) => error.code === "SCENE_BUILD_STORAGE_UNAVAILABLE",
  );
  fs.chmodSync(file, 0o600);
  const hardlink = `${file}.hardlink`;
  fs.linkSync(file, hardlink);
  await assert.rejects(
    service.status(artifact.artifact_id, {}, ACCESS),
    (error) => error.code === "SCENE_BUILD_STORAGE_UNAVAILABLE",
  );
  fs.unlinkSync(hardlink);
  await assert.rejects(
    service.status(artifact.artifact_id, {}, { ...ACCESS, ownerId: "other-owner" }),
    (error) => error.code === "VISTA_IMPORT_ACCESS_DENIED",
  );
});

test("a same-plan rebuild gets a fresh created_at while pending and terminal keep one operation timestamp", async (t) => {
  let randomValue = 1;
  let clockTick = 0;
  const instants = [
    "2026-07-21T01:00:00.000Z",
    "2026-07-21T01:00:01.000Z",
    "2026-07-21T02:00:00.000Z",
    "2026-07-21T02:00:01.000Z",
  ];
  const { artifact, root, service } = await fixture(t, {
    service: {
      randomBytes: () => Buffer.alloc(12, randomValue++),
      clock: () => new Date(instants[Math.min(clockTick++, instants.length - 1)]),
    },
  });
  const planned = await service.plan(artifact.artifact_id, {}, ACCESS);
  await service.execute(artifact.artifact_id, { plan_id: planned.plan.plan_id, confirm: true }, ACCESS);
  const file = path.join(root, "records", fs.readdirSync(path.join(root, "records"))[0]);
  const first = JSON.parse(fs.readFileSync(file, "utf8"));
  assert.equal(first.created_at, instants[0]);
  assert.equal(first.updated_at, instants[1]);

  await service.execute(artifact.artifact_id, { plan_id: planned.plan.plan_id, confirm: true }, ACCESS);
  const second = JSON.parse(fs.readFileSync(file, "utf8"));
  assert.notEqual(second.operation.operation_id, first.operation.operation_id);
  assert.equal(second.created_at, instants[2]);
  assert.equal(second.updated_at, instants[3]);
});

test("directory fsync failure blocks UE execution and journal publication", async (t) => {
  let journalCalls = 0;
  const current = await fixture(t, {
    artifactRecorder: {
      sceneBuildLineage() { throw new Error("must not run"); },
      async ensureSceneBuildTerminal() { journalCalls += 1; },
    },
    service: {
      async syncDirectory() {
        throw Object.assign(new Error("simulated directory fsync failure"), { code: "EIO" });
      },
    },
  });
  const planned = await current.service.plan(current.artifact.artifact_id, {}, ACCESS);
  await assert.rejects(
    current.service.execute(current.artifact.artifact_id, {
      plan_id: planned.plan.plan_id,
      confirm: true,
    }, ACCESS),
    hasCode("SCENE_BUILD_STORAGE_UNAVAILABLE"),
  );
  assert.equal(current.executorCalls.length, 0);
  assert.equal(journalCalls, 0);
});

test("journal failure never rewrites a successful UE build and status can replay durability", async (t) => {
  let failJournal = true;
  const recorded = [];
  const artifactRecorder = {
    sceneBuildLineage({ record }) {
      return {
        kind: "vista-scene-build",
        artifact_id: record.plan_id,
        revision: record.operation.operation_id,
        content_digest: "e".repeat(64),
      };
    },
    async ensureSceneBuildTerminal(input) {
      recorded.push(input);
      if (failJournal) throw Object.assign(new Error("journal unavailable"), {
        name: "ArtifactJournalRuntimeError",
        code: "ARTIFACT_JOURNAL_WRITE_FAILED",
      });
    },
  };
  const { artifact, service } = await fixture(t, { artifactRecorder });
  const planned = await service.plan(artifact.artifact_id, {}, ACCESS);
  await assert.rejects(
    service.execute(artifact.artifact_id, { plan_id: planned.plan.plan_id, confirm: true }, ACCESS),
    (error) => error.code === "ARTIFACT_JOURNAL_WRITE_FAILED",
  );
  assert.equal(service.resolveActiveRuntimeProof(ACCESS), null);
  assert.equal(recorded[0].record.status, "succeeded");
  assert.equal(recorded[0].record.access.owner_id, ACCESS.ownerId);
  assert.equal(recorded[0].record.access.last_session_id, ACCESS.sessionId);

  failJournal = false;
  const status = await service.status(artifact.artifact_id, {}, { ...ACCESS, sessionId: "new-session" });
  assert.equal(status.state, "succeeded");
  assert.equal(status.artifact_lineage.revision, status.operation_id);
  assert.equal(recorded.at(-1).record.access.last_session_id, ACCESS.sessionId);
});

test("a new build quarantines old runtime lineage and re-syncs a visible terminal before journal replay", async (t) => {
  let randomValue = 1;
  let armed = false;
  let armedSyncCalls = 0;
  const journalRecords = [];
  const artifactRecorder = {
    async ensureSceneBuildTerminal({ record }) { journalRecords.push(clone(record)); },
    sceneBuildLineage({ record }) {
      return {
        kind: "vista-scene-build",
        artifact_id: record.plan_id,
        revision: record.operation.operation_id,
        content_digest: crypto.createHash("sha256")
          .update(record.operation.operation_id, "utf8").digest("hex"),
      };
    },
  };
  const { artifact, service } = await fixture(t, {
    artifactRecorder,
    service: {
      randomBytes() { return Buffer.alloc(12, randomValue++); },
      async syncDirectory() {
        if (!armed) return;
        armedSyncCalls += 1;
        if (armedSyncCalls === 2) {
          armed = false;
          throw Object.assign(new Error("simulated terminal directory fsync failure"), { code: "EIO" });
        }
      },
    },
  });
  const planned = await service.plan(artifact.artifact_id, {}, ACCESS);
  const first = await service.execute(
    artifact.artifact_id,
    { plan_id: planned.plan.plan_id, confirm: true },
    ACCESS,
  );
  assert.equal(service.resolveActiveRuntimeProof(ACCESS).start_allowed, true);
  assert.equal(service.resolveActiveRuntimeLineage(ACCESS).revision, first.operation_id);

  armed = true;
  await assert.rejects(
    service.execute(
      artifact.artifact_id,
      { plan_id: planned.plan.plan_id, confirm: true },
      ACCESS,
    ),
    hasCode("SCENE_BUILD_STORAGE_UNAVAILABLE"),
  );
  assert.equal(armedSyncCalls, 2, "the failure occurs after the new terminal rename");
  assert.equal(service.resolveActiveRuntimeProof(ACCESS), null);
  assert.equal(service.resolveActiveRuntimeLineage(ACCESS), null);
  assert.equal(journalRecords.length, 1, "the not-yet-durable terminal must not be journaled");

  const replayed = await service.status(artifact.artifact_id, {}, ACCESS);
  assert.equal(replayed.state, "succeeded");
  assert.notEqual(replayed.operation_id, first.operation_id);
  assert.equal(journalRecords.length, 2, "status re-syncs the target before publishing its journal record");
  assert.equal(journalRecords[1].operation.operation_id, replayed.operation_id);
  assert.equal(service.resolveActiveRuntimeProof(ACCESS), null, "journal replay cannot recreate process-local UE proof");
  assert.equal(service.resolveActiveRuntimeLineage(ACCESS), null, "journal replay cannot recreate live lineage");
});

test("missing executor and unresolved production bindings fail closed", async (t) => {
  const noRuntime = await fixture(t, { executor: null });
  const planned = await noRuntime.service.plan(noRuntime.artifact.artifact_id, {}, ACCESS);
  await assert.rejects(
    noRuntime.service.preflight(noRuntime.artifact.artifact_id, { plan_id: planned.plan.plan_id }, ACCESS),
    hasCode("SCENE_BUILD_RUNTIME_UNAVAILABLE"),
  );

  const layout = profile();
  const importer = createVistaImporter({ registry: { round1_reviewed_latest: { root: FIXTURE_ROOT } } });
  const unresolved = await importer.preview({ datasetRevision: "round1_reviewed_latest", sampleId: "mmg_040", attempt: 7 });
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "vista-scene-unresolved-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const service = createVistaSceneBuildService({
    importService: { async status() { return { artifact_id: "vim_" + "b".repeat(64), status: "committed", scene_spec: unresolved }; } },
    layoutProfiles: { [layout.profile_id]: layout },
    recordRoot: root,
    executor: null,
  });
  await assert.rejects(
    service.plan("vim_" + "b".repeat(64), {}, ACCESS),
    (error) => error.code === "SCENE_BUILD_ASSET_UNRESOLVED",
  );
});

test("fallback BasicShapes infrastructure is rejected before any UE preflight or mutation", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "vista-scene-fallback-gate-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const fallback = profile({ production: false });
  const scene = await resolvedScene(fallback);
  const calls = [];
  const service = createVistaSceneBuildService({
    importService: {
      async status() {
        return { artifact_id: "vim_" + "f".repeat(64), status: "committed", scene_spec: scene };
      },
    },
    layoutProfiles: { [fallback.profile_id]: fallback },
    recordRoot: root,
    executor: {
      async preflight() { calls.push("preflight"); return {}; },
      async execute() { calls.push("execute"); return {}; },
    },
  });
  const planned = await service.plan("vim_" + "f".repeat(64), {}, ACCESS);
  await assert.rejects(
    service.preflight("vim_" + "f".repeat(64), { plan_id: planned.plan.plan_id }, ACCESS),
    hasCode("SCENE_BUILD_RUNTIME_PROOF_INVALID"),
  );
  await assert.rejects(
    service.execute("vim_" + "f".repeat(64), {
      plan_id: planned.plan.plan_id,
      confirm: true,
    }, ACCESS),
    hasCode("SCENE_BUILD_RUNTIME_PROOF_INVALID"),
  );
  assert.deepEqual(calls, []);
});

test("runtime proof is exact-lease process-local and requires verified semantic/PBR evidence", async (t) => {
  const { artifact, service } = await fixture(t);
  const planned = await service.plan(artifact.artifact_id, {}, ACCESS);
  await service.execute(artifact.artifact_id, { plan_id: planned.plan.plan_id, confirm: true }, ACCESS);
  const proof = service.resolveActiveRuntimeProof(ACCESS);
  assert.equal(proof.schema, "vista-runtime-scene-proof/v1");
  assert.equal(proof.plan_id, planned.plan.plan_id);
  assert.equal(proof.actor_count, planned.plan.actors.length);
  assert.equal(proof.start_allowed, true);
  assert.equal(service.resolveActiveRuntimeProof({ ...ACCESS, leaseId: "another-lease" }), null);

  const restarted = await fixture(t);
  assert.equal(restarted.service.resolveActiveRuntimeProof(ACCESS), null, "restart must quarantine stale disk evidence");
});

test("successful journaled execution exposes exact-lease scene-build lineage for Review", async (t) => {
  const artifactRecorder = {
    async ensureSceneBuildTerminal() { return { created: true }; },
    sceneBuildLineage({ record }) {
      return {
        kind: "vista-scene-build",
        artifact_id: record.plan_id,
        revision: record.operation.operation_id,
        content_digest: "e".repeat(64),
      };
    },
  };
  const { artifact, service } = await fixture(t, { artifactRecorder });
  const planned = await service.plan(artifact.artifact_id, {}, ACCESS);
  const executed = await service.execute(
    artifact.artifact_id,
    { plan_id: planned.plan.plan_id, confirm: true },
    ACCESS,
  );
  assert.deepEqual(service.resolveActiveRuntimeLineage(ACCESS), {
    kind: "vista-scene-build",
    artifact_id: planned.plan.plan_id,
    revision: executed.operation_id,
    content_digest: "e".repeat(64),
    scene_id: planned.plan.scene_id,
  });
  assert.equal(service.resolveActiveRuntimeLineage({ ...ACCESS, leaseId: "another-lease" }), null);
});

test("a Review mutation epoch invalidates a previously verified scene proof and lineage", async (t) => {
  let tokenId = 0;
  const mutationArbiter = createRuntimeMutationArbiter({
    randomUUID: () => `mutation-token-${++tokenId}`,
  });
  const artifactRecorder = {
    async ensureSceneBuildTerminal() {},
    sceneBuildLineage({ record }) {
      return {
        kind: "vista-scene-build",
        artifact_id: record.plan_id,
        revision: record.operation.operation_id,
        content_digest: "e".repeat(64),
      };
    },
  };
  const { artifact, service } = await fixture(t, {
    artifactRecorder,
    service: { mutationArbiter },
  });
  const planned = await service.plan(artifact.artifact_id, {}, ACCESS);
  await service.execute(artifact.artifact_id, { plan_id: planned.plan.plan_id, confirm: true }, ACCESS);
  assert.equal(service.resolveActiveRuntimeProof(ACCESS).start_allowed, true);
  assert.equal(service.resolveActiveRuntimeLineage(ACCESS).artifact_id, planned.plan.plan_id);

  const review = mutationArbiter.acquire(ACCESS, { kind: "review", operationId: "review-epoch" });
  assert.equal(service.resolveActiveRuntimeLineage(ACCESS).artifact_id, planned.plan.plan_id,
    "the locked pre-mutation snapshot may still bind the verified build lineage");
  review.invalidateScene();
  assert.equal(service.resolveActiveRuntimeProof(ACCESS), null);
  assert.equal(service.resolveActiveRuntimeLineage(ACCESS), null);
  review.release();
});

test("scene execution rejects another active mutation before writing or calling UE", async (t) => {
  let tokenId = 0;
  const mutationArbiter = createRuntimeMutationArbiter({
    randomUUID: () => `mutation-token-${++tokenId}`,
  });
  const { artifact, executorCalls, root, service } = await fixture(t, {
    service: { mutationArbiter },
  });
  const planned = await service.plan(artifact.artifact_id, {}, ACCESS);
  const review = mutationArbiter.acquire(ACCESS, { kind: "review", operationId: "active-review" });
  await assert.rejects(
    service.execute(artifact.artifact_id, { plan_id: planned.plan.plan_id, confirm: true }, ACCESS),
    hasCode("SCENE_BUILD_SLOT_BUSY"),
  );
  assert.deepEqual(executorCalls, []);
  assert.equal(fs.existsSync(path.join(root, "records")), false);
  review.release();
});
