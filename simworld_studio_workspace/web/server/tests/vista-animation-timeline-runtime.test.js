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
} = require("../vista-animation-contract");
const {
  ANIMATION_UE_CAPABILITY_OPERATION_FINGERPRINT,
  ANIMATION_UE_CAPABILITY_OPERATION_ID,
  ANIMATION_UE_CAPABILITY_SCHEMA,
  ANIMATION_UE_PLUGIN_API_SCHEMA,
  ANIMATION_UE_PLUGIN_ARTIFACT_SCHEMA,
  ANIMATION_UE_PLUGIN_NAME,
  OPERATION_SET,
  SECURITY_POLICY,
} = require("../vista-animation-ue-readiness");
const {
  ENGINE_TIME_RESPONSE_SCHEMA,
  VistaAnimationTimelineRuntimeError,
  createVerifiedSceneBindingResolver,
  createVistaAnimationTimelineRuntime,
  resolveVistaAnimationTimelineConfig,
} = require("../vista-animation-timeline-runtime");
const { createVistaImporter, validateSceneSpec } = require("../vista-importer");
const { compileVistaSceneBuildPlan } = require("../vista-scene-build-plan");

const SERVER_ROOT = path.resolve(__dirname, "..");
const VISTA_FIXTURE_ROOT = path.join(__dirname, "fixtures", "vista", "mmg_040");
const CHECKED_AT = "2026-07-21T12:00:00.000Z";

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function sha(file) {
  return crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
}

function contentProfile(overrides = {}) {
  const actions = ["look_at", "drag", "brace", "lift_foot", "pause", "fall", "recover"];
  return {
    schema: ANIMATION_CONTENT_PROFILE_SCHEMA,
    profile_id: "vista_hands_ik_v1",
    revision: "vista_hands_ik_2026_r1",
    content_revision: "simworld-content-2026.07.21-r1",
    content_digest: "b".repeat(64),
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
      completion_signal: `vista_${action}_complete`,
      timeout_ms: 5000,
    })),
    ...overrides,
  };
}

function pluginArtifact(overrides = {}) {
  return {
    schema: ANIMATION_UE_PLUGIN_ARTIFACT_SCHEMA,
    plugin_name: ANIMATION_UE_PLUGIN_NAME,
    plugin_version: "1.0.0",
    plugin_build_id: "vista-animation-linux-ue5.8-build001",
    binary_sha256: "c".repeat(64),
    engine_version: "5.8.0",
    target_platform: "linux-x86_64",
    api_schema: ANIMATION_UE_PLUGIN_API_SCHEMA,
    ...overrides,
  };
}

function writeJson(file, value) {
  fs.writeFileSync(file, `${JSON.stringify(value, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
}

function runtimeEnv(root, profile = contentProfile(), artifact = pluginArtifact(), extra = {}) {
  const profileFile = path.join(root, "content-profile.json");
  const artifactFile = path.join(root, "plugin-artifact.json");
  writeJson(profileFile, profile);
  writeJson(artifactFile, artifact);
  return {
    NODE_ENV: "production",
    VISTA_ANIMATION_TIMELINE_ENABLED: "1",
    VISTA_ANIMATION_CONTENT_PROFILE_FILE: profileFile,
    VISTA_ANIMATION_CONTENT_PROFILE_SHA256: sha(profileFile),
    VISTA_ANIMATION_UE_PLUGIN_ARTIFACT_FILE: artifactFile,
    VISTA_ANIMATION_UE_PLUGIN_ARTIFACT_SHA256: sha(artifactFile),
    VISTA_ANIMATION_RECORD_ROOT: path.join(root, "records"),
    ...extra,
  };
}

function temporaryRoot(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "vat-runtime-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return root;
}

function hasCode(code, status) {
  return (error) => {
    assert.equal(error && error.code, code);
    if (status !== undefined) assert.equal(error.status, status);
    return true;
  };
}

test("disabled and wholly missing configuration construct an unavailable typed service", async () => {
  const disabled = createVistaAnimationTimelineRuntime({ env: { NODE_ENV: "production" } });
  assert.equal(disabled.config.enabled, false);
  assert.equal(disabled.config.configured, false);
  await assert.rejects(
    disabled.service.preflight("ignored", {}, {}),
    hasCode("ANIMATION_RUNTIME_DISABLED", 503),
  );

  const missing = createVistaAnimationTimelineRuntime({
    env: { NODE_ENV: "development", VISTA_ANIMATION_TIMELINE_ENABLED: "1" },
  });
  assert.equal(missing.config.enabled, true);
  assert.equal(missing.config.configured, false);
  await assert.rejects(
    missing.service.preflight("ignored", {}, {}),
    hasCode("ANIMATION_RUNTIME_NOT_CONFIGURED", 503),
  );
});

test("partial production configuration fails closed at startup", () => {
  assert.throws(
    () => resolveVistaAnimationTimelineConfig({
      NODE_ENV: "production",
      VISTA_ANIMATION_TIMELINE_ENABLED: "1",
      VISTA_ANIMATION_CONTENT_PROFILE_FILE: "/srv/animation/content.json",
    }),
    hasCode("ANIMATION_RUNTIME_CONFIG_INVALID", 500),
  );
  assert.throws(
    () => resolveVistaAnimationTimelineConfig({
      NODE_ENV: "production",
      VISTA_ANIMATION_CONTENT_PROFILE_SHA256: "a".repeat(64),
    }),
    hasCode("ANIMATION_RUNTIME_CONFIG_INVALID", 500),
  );
});

test("pinned receipts reject checksum changes, symlinks, and production fixture markers", (t) => {
  const root = temporaryRoot(t);
  const env = runtimeEnv(root);
  const valid = resolveVistaAnimationTimelineConfig(env, { baseDir: SERVER_ROOT });
  assert.equal(valid.configured, true);
  assert.equal(valid.contentProfile.profile_id, "vista_hands_ik_v1");
  assert.equal(valid.pluginArtifact.plugin_build_id, "vista-animation-linux-ue5.8-build001");

  assert.throws(
    () => resolveVistaAnimationTimelineConfig({
      ...env,
      VISTA_ANIMATION_CONTENT_PROFILE_SHA256: "0".repeat(64),
    }),
    /checksum/,
  );

  const symlink = path.join(root, "linked-content.json");
  fs.symlinkSync(env.VISTA_ANIMATION_CONTENT_PROFILE_FILE, symlink);
  assert.throws(
    () => resolveVistaAnimationTimelineConfig({
      ...env,
      VISTA_ANIMATION_CONTENT_PROFILE_FILE: symlink,
    }),
    /pinned regular file/,
  );

  const fixtureProfileFile = path.join(root, "content-fixture.json");
  writeJson(fixtureProfileFile, contentProfile({ content_revision: "simworld-content-demo-r1" }));
  assert.throws(
    () => resolveVistaAnimationTimelineConfig({
      ...env,
      VISTA_ANIMATION_CONTENT_PROFILE_FILE: fixtureProfileFile,
      VISTA_ANIMATION_CONTENT_PROFILE_SHA256: sha(fixtureProfileFile),
    }),
    /not allowed in production/,
  );
});

function capabilityResponse(request, artifact) {
  return JSON.stringify({
    schema: ANIMATION_UE_CAPABILITY_SCHEMA,
    status: "ready",
    operation_id: ANIMATION_UE_CAPABILITY_OPERATION_ID,
    operation_fingerprint: ANIMATION_UE_CAPABILITY_OPERATION_FINGERPRINT,
    nonce_marker: clone(request.nonce_marker),
    challenge_digest: request.challenge_digest,
    slot_binding: clone(request.slot_binding),
    plugin_artifact: clone(artifact),
    process_instance_id: "ue-process:animation001",
    content_proof: clone(request.content_proof),
    security: clone(SECURITY_POLICY),
    operation_allowlist_digest: OPERATION_SET.operation_allowlist_digest,
    operations: clone(OPERATION_SET.operations),
  });
}

function dedicatedTransport(artifact, calls = []) {
  return {
    async captureAnimationEvidence() {
      throw new Error("not used by this test");
    },
    async invokeAnimationContentApi() {
      throw new Error("not used by this test");
    },
    async probeAnimationContentApi(requestJson, options) {
      const request = JSON.parse(requestJson);
      calls.push({ kind: "probe", request, options });
      return capabilityResponse(request, artifact);
    },
    async sampleAnimationEngineTime(requestJson, options) {
      const request = JSON.parse(requestJson);
      calls.push({ kind: "engine_time", request, options });
      return JSON.stringify({
        schema: ENGINE_TIME_RESPONSE_SCHEMA,
        run_id: request.run_id,
        timeline_id: request.timeline_id,
        event_id: request.event_id,
        slot_binding: clone(request.slot_binding),
        request_digest: request.request_digest,
        engine_time_sec: 4.25,
      });
    },
  };
}

function identity(overrides = {}) {
  return {
    ownerId: "owner:ives",
    sessionId: "session:animation001",
    slotId: 3,
    leaseId: "lease:animation001",
    mcpPort: 55565,
    sceneRevision: `mmg_040@${"a".repeat(64)}`,
    planId: `vsp-${"d".repeat(24)}`,
    ...overrides,
  };
}

function inertServices() {
  return {
    importService: { async status() { throw new Error("not used"); } },
    sceneBuildService: { async status() { throw new Error("not used"); } },
  };
}

test("runtime provider binds the live challenge and engine sampler to the exact server slot", async (t) => {
  const root = temporaryRoot(t);
  const env = runtimeEnv(root);
  const artifact = pluginArtifact();
  const calls = [];
  const runtime = createVistaAnimationTimelineRuntime({
    env,
    ...inertServices(),
    transportResolver: async (resolvedIdentity) => {
      assert.deepEqual(resolvedIdentity, identity());
      return dedicatedTransport(artifact, calls);
    },
    clock: () => new Date(CHECKED_AT),
  });
  const bundle = await runtime.runtimeProvider(identity());
  const ready = await bundle.probeReadiness();
  assert.equal(ready.status, "ready");
  assert.deepEqual(ready.causes, []);
  assert.equal(ready.revision.verification, "live_plugin_challenge");
  const probe = calls.find((entry) => entry.kind === "probe");
  assert.deepEqual(probe.request.slot_binding, {
    schema: "vista-animation-ue-slot-binding/v1",
    owner_id: "owner:ives",
    session_id: "session:animation001",
    slot_id: "3",
    scene_revision: identity().sceneRevision,
    binding_digest: ready.revision.slot_binding_digest,
  });

  const signal = new AbortController().signal;
  const engineTime = await bundle.engineTimeSampler({
    run_id: "vtr-aaaaaaaaaaaaaaaaaaaaaaaa",
    timeline_id: `vtl-${"b".repeat(24)}`,
    event_id: "beat-0001",
    signal,
    owner_id: identity().ownerId,
    session_id: identity().sessionId,
    slot_id: String(identity().slotId),
    lease_id: identity().leaseId,
    mcp_port: identity().mcpPort,
    scene_revision: identity().sceneRevision,
  });
  assert.equal(engineTime, 4.25);
  const sample = calls.find((entry) => entry.kind === "engine_time");
  assert.equal(Object.prototype.hasOwnProperty.call(sample.request, "lease_id"), false);
  assert.equal(sample.request.slot_binding.scene_revision, identity().sceneRevision);
});

test("transport resolution rejects incomplete and generic command-capable interfaces", async (t) => {
  const root = temporaryRoot(t);
  const env = runtimeEnv(root);
  const services = inertServices();
  const incomplete = createVistaAnimationTimelineRuntime({
    env,
    ...services,
    transportResolver: async () => ({
      probeAnimationContentApi() {},
      invokeAnimationContentApi() {},
    }),
  });
  await assert.rejects(
    incomplete.runtimeProvider(identity()),
    hasCode("ANIMATION_UE_TRANSPORT_INVALID", 503),
  );

  const generic = createVistaAnimationTimelineRuntime({
    env,
    ...services,
    transportResolver: async () => ({
      ...dedicatedTransport(pluginArtifact()),
      executePythonScript() {},
    }),
  });
  await assert.rejects(
    generic.runtimeProvider(identity()),
    hasCode("ANIMATION_UE_TRANSPORT_INVALID", 503),
  );
});

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

async function verifiedSceneAndPlan() {
  const layout = JSON.parse(fs.readFileSync(path.join(VISTA_FIXTURE_ROOT, "build-layout.v1.json"), "utf8"));
  const importer = createVistaImporter({
    registry: { round1_reviewed_latest: { root: VISTA_FIXTURE_ROOT } },
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
        manual_override: { confirmed: true, reason: "Verified binding fixture", ...binding },
      },
    };
  });
  scene.unresolved = scene.unresolved.filter((item) => item.kind !== "asset");
  const validatedScene = validateSceneSpec(scene);
  return { scene: validatedScene, plan: compileVistaSceneBuildPlan(validatedScene, layout) };
}

function buildResult(plan) {
  return {
    schema: "vista-scene-build-result/v1",
    build_id: `vsb-${plan.plan_id.slice(4)}`,
    plan_id: plan.plan_id,
    scene_id: plan.scene_id,
    status: "succeeded",
    actor_manifest: plan.actors.map((actor, index) => ({
      ...clone(actor),
      operation_id: `vso-${crypto.createHash("sha256")
        .update(`${plan.plan_id}\0${actor.actor_name}\0${actor.fingerprint}`, "utf8")
        .digest("hex")
        .slice(0, 24)}`,
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
    evidence: [],
    rollback: { state: "not_required", deleted_actor_names: [], restored_player_start: false, failures: [] },
  };
}

function bindingIdentity() {
  const value = identity();
  return {
    ownerId: value.ownerId,
    sessionId: value.sessionId,
    slotId: value.slotId,
    leaseId: value.leaseId,
    mcpPort: value.mcpPort,
  };
}

test("binding resolver reconstructs exact pawn and target anchors only from verified build output", async () => {
  const { scene, plan } = await verifiedSceneAndPlan();
  const result = buildResult(plan);
  const resolver = createVerifiedSceneBindingResolver();
  const bindings = await resolver({
    sceneSpec: scene,
    buildPlan: plan,
    buildResult: result,
    contentProfile: contentProfile(),
    identity: bindingIdentity(),
  });
  assert.deepEqual(bindings.actors, [{
    source_id: "camera_wearer",
    binding_id: "first_person_hands",
    kind: "player",
    capabilities: ["gaze", "hold_pose", "lower_body_ik", "root_motion", "upper_body_ik"],
  }]);
  const targets = Object.fromEntries(bindings.entities.map((entry) => [entry.source_id, entry]));
  assert.equal(targets.wheeled_office_chair_with_visible_casters.binding_id, "office_chair");
  assert.equal(targets.cardboard_box_on_a_high_cabinet.binding_id, "high_cardboard_box");
  assert.deepEqual(targets.wheeled_office_chair_with_visible_casters.capabilities, [
    "draggable", "foot_contact_target", "hand_contact_target",
  ]);
  assert.deepEqual(targets.cardboard_box_on_a_high_cabinet.capabilities, ["gaze_target"]);
  assert.match(bindings.revision, /^bindings_[a-f0-9]{24}$/);
});

test("binding resolver rejects manifest drift, ambiguous anchors, and caller binding fields", async () => {
  const { scene, plan } = await verifiedSceneAndPlan();
  const result = buildResult(plan);
  const resolver = createVerifiedSceneBindingResolver();
  const context = {
    sceneSpec: scene,
    buildPlan: plan,
    buildResult: result,
    contentProfile: contentProfile(),
    identity: bindingIdentity(),
  };
  const drifted = clone(result);
  drifted.actor_manifest[0].runtime_actor_name = "Browser_Selected_Actor";
  await assert.rejects(
    resolver({ ...context, buildResult: drifted }),
    hasCode("ANIMATION_BINDINGS_MISMATCH", 409),
  );

  const ambiguous = clone(scene);
  ambiguous.timeline = ambiguous.timeline.map((event) => (
    event.target_id === "cardboard_box_on_a_high_cabinet"
      ? { ...event, action: "brace" }
      : event
  ));
  await assert.rejects(
    resolver({ ...context, sceneSpec: ambiguous }),
    hasCode("ANIMATION_BINDINGS_MISMATCH", 409),
  );

  await assert.rejects(
    resolver({ ...context, browserBindings: { actor: "anything" } }),
    hasCode("ANIMATION_BINDINGS_MISMATCH", 500),
  );
});

test("runtime config errors remain typed and do not expose filesystem contents", () => {
  const error = new VistaAnimationTimelineRuntimeError("ANIMATION_RUNTIME_CONFIG_INVALID", "invalid");
  assert.equal(error.status, 500);
  assert.equal(error.retryable, false);
});
