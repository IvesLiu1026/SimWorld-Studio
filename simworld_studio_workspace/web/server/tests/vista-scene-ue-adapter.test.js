"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  VistaSceneUeAdapterError,
  createVistaSceneUeAdapter,
  extractMarker,
  preflightScript,
  spawnScript,
} = require("../vista-scene-ue-adapter");

const NONCE = "0123456789abcdef0123456789abcdef";
const CONTENT_REVISION = "simworld-content-r1";
const VERIFICATION_REVISION = "asset-verify-r1";
const CONTENT_RECEIPT_SHA256 = "c".repeat(64);
const OBJECT_GUID = "01234567-89ab-cdef-0123-456789abcdef";

function markerFromScript(script) {
  const match = script.match(/print\("(VISTA_SCENE_UE_V1_[A-Z_]+:[a-f0-9]{32}:)" \+ json\.dumps/);
  assert.ok(match, "fixed script must contain one nonce-bound marker");
  return match[1].slice(0, -1);
}

class FakeUeBroker {
  constructor(root) {
    this.root = root;
    this.calls = [];
    this.responses = new Map();
  }

  set(operation, payload) {
    this.responses.set(operation, payload);
  }

  async send(type, params, options) {
    this.calls.push({ type, params, options });
    if (type === "take_screenshot") {
      const png = Buffer.alloc(33);
      Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]).copy(png, 0);
      png.writeUInt32BE(13, 8);
      png.write("IHDR", 12, "ascii");
      png.writeUInt32BE(1, 16);
      png.writeUInt32BE(1, 20);
      fs.writeFileSync(params.filepath, png);
      return { status: "success" };
    }
    assert.equal(type, "execute_python_script");
    const marker = markerFromScript(params.script);
    const operation = marker.split(":")[0].slice("VISTA_SCENE_UE_V1_".length);
    const payload = this.responses.get(operation) || { ok: true, status: "success", actor_name: "Actor" };
    return { status: "success", result: { python_logs: [`${marker}:${JSON.stringify(payload)}`] } };
  }
}

function adapterFixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "vista-scene-ue-adapter-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const ueBroker = new FakeUeBroker(root);
  const adapter = createVistaSceneUeAdapter({
    ueBroker,
    contentRevision: CONTENT_REVISION,
    verificationRevision: VERIFICATION_REVISION,
    contentReceiptSha256: CONTENT_RECEIPT_SHA256,
    screenshotDir: root,
    nonceFactory: () => NONCE,
    timeoutMs: 1_000,
  });
  return { adapter, root, ueBroker };
}

function request() {
  const asset = {
    asset_key: "snapshot|chair|/Game/Office/SM_Chair.SM_Chair|/Script/Engine.StaticMeshActor|simworld-content-r1|asset-verify-r1",
    snapshot_id: "snapshot",
    asset_id: "chair",
    ue_path: "/Game/Office/SM_Chair.SM_Chair",
    class_path: "/Script/Engine.StaticMeshActor",
    content_revision: CONTENT_REVISION,
    verification_revision: VERIFICATION_REVISION,
  };
  return {
    schema: "vista-scene-build-preflight-request/v1",
    plan_id: "vsp-" + "a".repeat(24),
    scene_id: "mmg_040@0123456789abcdef",
    assets: [asset],
    actors: [{
      actor_name: "VISTA_mmg_040_actor",
      fingerprint: "vsa-" + "b".repeat(24),
      operation_id: "vso-" + "d".repeat(24),
      asset_kind: "static_mesh",
      class_path: "/Script/Engine.StaticMeshActor",
      ue_path: "/Game/Office/SM_Chair.SM_Chair",
      transform: { location_cm: [1, 2, 3], rotation_deg: [4, 5, 6], scale: [1, 1, 1] },
      mobility: "movable",
      collision: { mode: "query_and_physics", profile_name: "BlockAll", generate_overlap_events: false },
    }],
    player_start: {
      actor_name: "PlayerStart",
      class_path: "/Script/Engine.PlayerStart",
      pawn_class_path: "/Game/Human/BP_Character.BP_Character_C",
      transform: { location_cm: [0, 0, 90], rotation_deg: [0, 90, 0], scale: [1, 1, 1] },
    },
  };
}

function actor() {
  return {
    actor_id: "chair",
    actor_name: "VISTA_mmg_040_actor",
    role: "scene_entity",
    source_entity_id: "chair",
    component_id: null,
    infrastructure_kind: null,
    spawn_tool: "spawn_actor",
    asset: {
      binding_source: "scene_binding",
      kind: "static_mesh",
      snapshot_id: "snapshot",
      asset_id: "chair",
      ue_path: "/Game/Office/SM_Chair.SM_Chair",
      class_path: "/Script/Engine.StaticMeshActor",
      confidence: 1,
      content_revision: CONTENT_REVISION,
      verification_revision: VERIFICATION_REVISION,
      verified: true,
    },
    transform: { location_cm: [1, 2, 3], rotation_deg: [4, 5, 6], scale: [1, 1, 1] },
    mobility: "movable",
    collision: { mode: "query_and_physics", profile_name: "BlockAll", generate_overlap_events: false },
    fingerprint: "vsa-" + "b".repeat(24),
    operation_id: "vso-" + "d".repeat(24),
    object_guid: OBJECT_GUID,
  };
}

function validationBundle(overrides = {}) {
  return {
    ok: true,
    actor_snapshot: [],
    world_snapshot: [],
    validation_scope: {
      mode: "generated_vs_generated_and_world_static",
      world_actor_count: 0,
      excluded_class_terms: ["WorldSettings", "PlayerStart", "Volume", "Light", "Camera", "Sky"],
    },
    content_receipt: {
      schema: "simworld-ue-content-receipt/v1",
      content_revision: CONTENT_REVISION,
      verification_revision: VERIFICATION_REVISION,
      receipt_sha256: CONTENT_RECEIPT_SHA256,
    },
    scene_digest: "e".repeat(64),
    collisions: [],
    collision_count: 0,
    floating: [],
    floating_count: 0,
    ...overrides,
  };
}

test("preflight uses a fixed JSON payload and returns only the nonce-bound marker", async (t) => {
  const { adapter, ueBroker } = adapterFixture(t);
  const input = request();
  const response = {
    schema: "vista-scene-build-preflight-response/v1",
    plan_id: input.plan_id,
    ok: true,
    assets: [{ asset_key: input.assets[0].asset_key, available: true, class_matches: true, revision_matches: true }],
    actors: [{
      actor_name: input.actors[0].actor_name,
      state: "absent",
      actual_fingerprint: null,
      actual_operation_id: null,
      object_guid: null,
      spec_matches: false,
    }],
    player_start: {
      actor_name: "PlayerStart",
      class_path: "/Script/Engine.PlayerStart",
      state: "needs_update",
      current_transform: { location_cm: [0, 0, 0], rotation_deg: [0, 0, 0], scale: [1, 1, 1] },
      object_guid: "fedcba98-7654-3210-fedc-ba9876543210",
    },
  };
  ueBroker.set("PREFLIGHT", response);

  assert.deepEqual(await adapter.broker.preflight(input), response);
  const script = ueBroker.calls[0].params.script;
  assert.equal(script.includes("unreal.load_asset(item['ue_path'])"), true);
  assert.equal(script.includes(input.assets[0].ue_path), true);
  assert.equal(script.includes("content-revision.json"), true);
  assert.equal(script.includes(CONTENT_RECEIPT_SHA256), true);
  assert.equal(ueBroker.calls[0].options.timeoutMs, 1_000);
  assert.equal(ueBroker.calls[0].options.maxAttempts, 1);
});

test("spawn/delete/PlayerStart operations are fixed scripts with cleanup semantics", async (t) => {
  const { adapter, ueBroker } = adapterFixture(t);
  const item = actor();
  ueBroker.set("SPAWN", {
    ok: true,
    status: "success",
    actor_name: item.actor_name,
    operation_id: item.operation_id,
    object_guid: item.object_guid,
  });
  ueBroker.set("DELETE", { ok: true, status: "success", actor_name: item.actor_name });
  ueBroker.set("PLAYER_SET", { ok: true, status: "success", actor_name: "PlayerStart" });
  ueBroker.set("PLAYER_RESTORE", { ok: true, status: "success", actor_name: "PlayerStart" });

  await adapter.broker.spawnActor(item);
  await adapter.broker.deleteActor(item);
  await adapter.broker.setPlayerStart({ player_start: request().player_start, camera: {} });
  await adapter.broker.restorePlayerStart({ actor_name: "PlayerStart", transform: request().player_start.transform });

  assert.deepEqual(ueBroker.calls.map((call) => markerFromScript(call.params.script).split(":")[0]), [
    "VISTA_SCENE_UE_V1_SPAWN",
    "VISTA_SCENE_UE_V1_DELETE",
    "VISTA_SCENE_UE_V1_PLAYER_SET",
    "VISTA_SCENE_UE_V1_PLAYER_RESTORE",
  ]);
  assert.equal(ueBroker.calls[0].params.script.includes("subsystem.destroy_actor(created)"), true);
  assert.equal(ueBroker.calls[0].params.script.includes("VISTA_FINGERPRINT="), true);
  assert.equal(ueBroker.calls[0].params.script.includes("set_generate_overlap_events"), false);
  assert.equal(
    ueBroker.calls[0].params.script.includes(
      "component.set_editor_property('generate_overlap_events', bool(item['collision']['generate_overlap_events']))",
    ),
    true,
  );
});

test("required evidence rejects collisions/floating actors and screenshot exposes no server path", async (t) => {
  const { adapter, root, ueBroker } = adapterFixture(t);
  const item = actor();
  const context = {
    plan: { plan_id: "vsp-" + "c".repeat(24) },
    actor_manifest: [item],
    signal: undefined,
  };
  ueBroker.set("VALIDATION_BEFORE", validationBundle({
    actor_snapshot: [{
      actor_name: item.actor_name,
      fingerprint: item.fingerprint,
      operation_id: item.operation_id,
      object_guid: item.object_guid,
      class_path: item.asset.class_path,
      asset_path: item.asset.ue_path,
      location_cm: [1, 2, 3],
      rotation_deg: [4, 5, 6],
      scale: [1, 1, 1],
      component_policies: [{
        mobility: item.mobility,
        collision_mode: item.collision.mode,
        profile_name: item.collision.profile_name,
        generate_overlap_events: item.collision.generate_overlap_events,
      }],
      materials: [{
        component: "StaticMeshComponent0",
        slot_index: 0,
        material_path: "/Game/VISTA/Materials/M_ChairPBR.M_ChairPBR",
        material_class: "/Script/Engine.MaterialInstanceConstant",
        pbr_eligible: true,
      }],
    }],
  }));
  const snapshot = await adapter.evidenceHooks.actor_snapshot(context);
  assert.equal(snapshot.actors[0].fingerprint, item.fingerprint);

  ueBroker.set("VALIDATION_BEFORE", validationBundle({
    actor_snapshot: [{
      ...snapshot.actors[0],
      materials: [{
        component: "StaticMeshComponent0",
        slot_index: 0,
        material_path: "/Engine/EngineMaterials/DefaultMaterial.DefaultMaterial",
        material_class: "/Script/Engine.Material",
        pbr_eligible: false,
      }],
    }],
  }));
  await assert.rejects(
    adapter.evidenceHooks.actor_snapshot(context),
    (error) => error instanceof VistaSceneUeAdapterError && error.code === "SCENE_BUILD_ACTOR_EVIDENCE_INVALID",
  );

  ueBroker.set("VALIDATION_BEFORE", validationBundle({
    collisions: [{ actor_a: "a", actor_b: "b", scope: "generated_world_static" }],
    collision_count: 1,
  }));
  await assert.rejects(
    adapter.evidenceHooks.collision_report(context),
    (error) => error instanceof VistaSceneUeAdapterError && error.code === "SCENE_BUILD_COLLISIONS_DETECTED",
  );
  ueBroker.set("VALIDATION_BEFORE", validationBundle({
    floating: [{ actor_name: "a", gap_cm: 20 }],
    floating_count: 1,
  }));
  await assert.rejects(
    adapter.evidenceHooks.floating_report(context),
    (error) => error instanceof VistaSceneUeAdapterError && error.code === "SCENE_BUILD_FLOATING_ACTORS_DETECTED",
  );

  ueBroker.set("VALIDATION_BEFORE", validationBundle());
  ueBroker.set("VALIDATION_AFTER", validationBundle());
  const screenshot = await adapter.evidenceHooks.screenshot(context);
  assert.equal(screenshot.url.startsWith("/screenshots/"), true);
  assert.equal(JSON.stringify(screenshot).includes(root), false);
  assert.match(screenshot.sha256, /^[a-f0-9]{64}$/);
});

test("marker parser fails closed on missing and duplicate receipts", () => {
  assert.throws(
    () => extractMarker({ result: { python_logs: [] } }, "marker"),
    (error) => error.code === "SCENE_BUILD_UE_MARKER_MISSING",
  );
  assert.throws(
    () => extractMarker({ result: { python_logs: ["marker:{}", "marker:{}"] } }, "marker"),
    (error) => error.code === "SCENE_BUILD_UE_MARKER_DUPLICATE",
  );
});

test("script builders never interpolate actor values into executable source", () => {
  const hostile = actor();
  hostile.actor_name = "value'); raise RuntimeError('owned";
  const built = spawnScript(NONCE, hostile);
  const executableLines = built.script.split("\n").filter((line) => !line.startsWith("REQUEST = json.loads("));
  assert.equal(executableLines.join("\n").includes(hostile.actor_name), false);
  assert.equal(preflightScript(NONCE, request(), true).script.includes("REQUEST = json.loads("), true);
});
