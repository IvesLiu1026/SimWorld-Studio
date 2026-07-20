"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { createVistaImporter, validateSceneSpec } = require("../vista-importer");
const {
  BUILD_PLAN_SCHEMA,
  ENGINE_RUNTIME_GROUND,
  VistaSceneBuildError,
  compileVistaSceneBuildPlan,
  deterministicActorName,
  validateVistaSceneBuildPlan,
} = require("../vista-scene-build-plan");

const FIXTURE_ROOT = path.resolve(__dirname, "fixtures/vista/mmg_040");
const SCHEMA_PATH = path.resolve(__dirname, "../schemas/vista-scene-build-plan-v1.schema.json");
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

function selectedPins(profile) {
  const curatedByEntity = new Map();
  for (const placement of profile.placements) {
    if (placement.asset_pin && !curatedByEntity.has(placement.source_entity_id)) {
      curatedByEntity.set(placement.source_entity_id, placement.asset_pin);
    }
  }
  return {
    wheeled_office_chair_with_visible_casters: {
      snapshot_id: profile.asset_snapshot_id,
      asset_id: "citydb-office-chair-b",
      ue_path: "/Game/CityDatabase/meshes/SM_chair_b.SM_chair_b",
      confidence: 1,
    },
    stable_step_stool: {
      snapshot_id: profile.asset_snapshot_id,
      asset_id: "mmg040-step-stool",
      ue_path: "/Game/VISTA/Curated/MMG040/SM_StepStool.SM_StepStool",
      confidence: 1,
    },
    cardboard_box_on_a_high_cabinet: {
      ...curatedByEntity.get("cardboard_box_on_a_high_cabinet"),
    },
    camera_wearer_hands_and_forearms: {
      snapshot_id: profile.asset_snapshot_id,
      asset_id: "human-avatar-third-person-character",
      ue_path: "/Game/Human_Avatar/DefaultCharacter/ThirdPerson/Blueprints/BP_ThirdPersonCharacter.BP_ThirdPersonCharacter_C",
      confidence: 1,
    },
  };
}

function resolveFixtureScene(scene, profile) {
  const output = clone(scene);
  const pins = selectedPins(profile);
  output.entities = output.entities.map((entity) => {
    const selected = pins[entity.id];
    const binding = {
      snapshot_id: selected.snapshot_id,
      asset_id: selected.asset_id,
      ue_path: selected.ue_path,
      confidence: selected.confidence,
    };
    return {
      ...entity,
      asset_binding: binding,
      asset_resolution: {
        schema: "vista-asset-resolution/v1",
        snapshot_id: selected.snapshot_id,
        query: entity.semantic_query,
        min_confidence: 0,
        candidates: [{ rank: 1, ...binding, origin: "manual_override" }],
        selected_binding: binding,
        selected_by: "manual_override",
        manual_override: {
          confirmed: true,
          reason: "Pinned by the deterministic mmg_040 scene-build contract test",
          ...binding,
        },
      },
    };
  });
  output.unresolved = output.unresolved.filter((item) => item.kind !== "asset");
  return validateSceneSpec(output);
}

async function loadResolvedMmg040() {
  const importer = createVistaImporter({
    registry: { round1_reviewed_latest: { root: FIXTURE_ROOT } },
  });
  const scene = await importer.preview(REQUEST);
  return resolveFixtureScene(scene, loadProfile());
}

function expectBuildError(fn, code) {
  assert.throws(fn, (error) => {
    assert.ok(error instanceof VistaSceneBuildError);
    assert.equal(error.code, code);
    assert.equal(error.retryable, false);
    return true;
  });
}

test("BuildPlan JSON schema pins numeric transforms, asset identity, player start, and evidence", () => {
  const schema = JSON.parse(fs.readFileSync(SCHEMA_PATH, "utf8"));
  assert.equal(schema.$schema, "https://json-schema.org/draft/2020-12/schema");
  assert.equal(schema.properties.schema.const, BUILD_PLAN_SCHEMA);
  assert.ok(schema.required.includes("actors"));
  assert.ok(schema.required.includes("player_start"));
  assert.ok(schema.required.includes("camera"));
  assert.deepEqual(schema.$defs.transform.required, ["location_cm", "rotation_deg", "scale"]);
  assert.ok(schema.$defs.asset.required.includes("snapshot_id"));
  assert.ok(schema.$defs.asset.required.includes("class_path"));
  assert.ok(schema.$defs.asset.required.includes("content_revision"));
  assert.ok(schema.$defs.actor.required.includes("mobility"));
  assert.ok(schema.$defs.actor.required.includes("collision"));
});

test("mmg_040 compiles deterministically into a numeric, fully pinned static BuildPlan", async () => {
  const scene = await loadResolvedMmg040();
  const profile = loadProfile();
  const beforeScene = JSON.stringify(scene);
  const beforeProfile = JSON.stringify(profile);
  const first = compileVistaSceneBuildPlan(scene, profile);
  const reordered = clone(profile);
  reordered.placements.reverse();
  reordered.infrastructure.reverse();
  reordered.evidence_requests.reverse();
  const second = compileVistaSceneBuildPlan(scene, reordered);

  assert.deepEqual(second, first);
  assert.equal(JSON.stringify(second), JSON.stringify(first));
  assert.equal(first.schema, BUILD_PLAN_SCHEMA);
  assert.match(first.plan_id, /^vsp-[a-f0-9]{24}$/);
  assert.equal(first.scene_id, scene.scene_id);
  assert.equal(first.scene_revision, scene.source.source_checksum);
  assert.equal(first.coordinate_system, "unreal_centimeters_z_up");
  assert.equal(first.actors.length, 6);
  assert.deepEqual(first.actors.map((actor) => actor.actor_id), [
    "first_person_hands",
    "high_cardboard_box",
    "office_chair",
    "runtime_ground",
    "step_stool",
    "tall_cabinet",
  ]);
  assert.ok(first.actors.every((actor) => actor.actor_name === deterministicActorName(scene.scene_id, actor.actor_id)));
  assert.ok(first.actors.every((actor) => /^vsa-[a-f0-9]{24}$/.test(actor.fingerprint)));
  assert.ok(first.actors.every((actor) => actor.asset.verified === true));
  assert.ok(first.actors.every((actor) => actor.asset.content_revision === profile.content_revision));
  assert.ok(first.actors.every((actor) => actor.asset.verification_revision === profile.verification_revision));
  assert.deepEqual(first.player_start.transform.location_cm, [0, -350, 90]);
  assert.equal(first.player_start.class_path, "/Script/Engine.PlayerStart");
  assert.equal(first.camera.perspective, "first_person");
  assert.equal(first.camera.target_actor_id, "high_cardboard_box");
  assert.deepEqual(first.camera.source_constraints, [...scene.camera.constraints].sort());
  assert.equal(first.evidence_requests.length, 4);
  assert.equal(first.mutation_policy.fail_before_mutation, true);
  assert.equal(validateVistaSceneBuildPlan(first), first);
  assert.ok(Object.isFrozen(first));
  assert.ok(Object.isFrozen(first.actors));
  assert.equal(JSON.stringify(scene), beforeScene, "compiler must not mutate SceneSpec");
  assert.equal(JSON.stringify(profile), beforeProfile, "compiler must not mutate layout profile");
});

test("semantic basic geometry fallback is forbidden while the explicit fixed runtime ground is allowed", async () => {
  const scene = await loadResolvedMmg040();
  const profile = loadProfile();
  const plan = compileVistaSceneBuildPlan(scene, profile);
  const ground = plan.actors.find((actor) => actor.infrastructure_kind === "runtime_ground");
  assert.equal(ground.asset.ue_path, ENGINE_RUNTIME_GROUND);
  assert.equal(ground.role, "infrastructure");
  assert.equal(ground.collision.profile_name, "BlockAll");

  const semanticCube = clone(profile);
  semanticCube.placements[0].asset_source = "curated_component";
  semanticCube.placements[0].component_id = "cube_fallback";
  semanticCube.placements[0].asset_pin = {
    snapshot_id: profile.asset_snapshot_id,
    asset_id: "semantic-cube-fallback",
    ue_path: ENGINE_RUNTIME_GROUND,
    confidence: 1,
    verified: true,
  };
  expectBuildError(
    () => compileVistaSceneBuildPlan(scene, semanticCube),
    "SCENE_BUILD_BASIC_GEOMETRY_FORBIDDEN",
  );

  const disguisedGround = clone(profile);
  disguisedGround.infrastructure[0].infrastructure_kind = "environment_shell";
  expectBuildError(
    () => compileVistaSceneBuildPlan(scene, disguisedGround),
    "SCENE_BUILD_BASIC_GEOMETRY_FORBIDDEN",
  );
});

test("compiler blocks unresolved, incomplete, mismatched, and tampered plans", async () => {
  const importer = createVistaImporter({
    registry: { round1_reviewed_latest: { root: FIXTURE_ROOT } },
  });
  const unresolved = await importer.preview(REQUEST);
  const profile = loadProfile();
  expectBuildError(
    () => compileVistaSceneBuildPlan(unresolved, profile),
    "SCENE_BUILD_ASSET_UNRESOLVED",
  );

  const scene = resolveFixtureScene(unresolved, profile);
  const incomplete = clone(profile);
  incomplete.placements = incomplete.placements.filter((placement) => placement.source_entity_id !== "stable_step_stool");
  expectBuildError(
    () => compileVistaSceneBuildPlan(scene, incomplete),
    "SCENE_BUILD_LAYOUT_INCOMPLETE",
  );

  const snapshotMismatch = clone(profile);
  snapshotMismatch.asset_snapshot_id = "different-snapshot-r2";
  expectBuildError(
    () => compileVistaSceneBuildPlan(scene, snapshotMismatch),
    "SCENE_BUILD_ASSET_SNAPSHOT_MISMATCH",
  );

  const tampered = clone(compileVistaSceneBuildPlan(scene, profile));
  tampered.actors[0].transform.location_cm[0] += 1;
  expectBuildError(
    () => validateVistaSceneBuildPlan(tampered),
    "SCENE_BUILD_PLAN_INVALID",
  );
});
