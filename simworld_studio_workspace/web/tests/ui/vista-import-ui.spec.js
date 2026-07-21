import { expect, test } from "@playwright/test";

const CHECKSUM = "b".repeat(64);

function previewFixture() {
  return {
    schema: "vista-simworld-scene/v1",
    scene_id: "mmg_040@bbbbbbbbbbbbbbbb",
    profile: "reconstruction",
    privilege: {
      profile: "reconstruction",
      classification: "reconstruction_only",
      evaluation_input_allowed: false,
    },
    provenance: {
      importer_name: "vista-scene-importer",
      importer_version: "1.0.0",
      source_checksum: CHECKSUM,
      bundle_id: "round1_reviewed_latest:mmg_040:multimodal_grounded_safety_040:sora2:attempt_007",
    },
    source: {
      profile: "reconstruction",
      classification: "reconstruction_source",
      dataset_revision: "round1_reviewed_latest",
      source_row_id: "mmg_040__multimodal_grounded::multimodal_grounded_safety_040::sora2::attempt_007",
      visual_id: "mmg_040",
      case_scope: "multimodal_grounded_safety_040",
      scenario_type: "multimodal_grounded",
      attempt: { provider: "sora2", index: 7, selected: true },
      source_checksum: CHECKSUM,
    },
    duration_sec: 12,
    entities: [
      {
        id: "black_wheeled_office_chair",
        semantic_query: "black wheeled office chair",
        required: true,
        asset_binding: null,
        source_pointer: "/Scene/Key_Visual_Elements/0",
      },
      {
        id: "metal_storage_cabinet",
        semantic_query: "metal storage cabinet",
        required: true,
        asset_binding: { snapshot_id: "snapshot-1", asset_id: "cabinet-1", ue_path: "/Game/Props/SM_Cabinet", confidence: 0.94 },
        asset_resolution: {
          query: "metal storage cabinet",
          snapshot_id: "snapshot-1",
          selected_by: "automatic",
          candidates: [{ rank: 1, asset_id: "cabinet-1", ue_path: "/Game/Props/SM_Cabinet", confidence: 0.94 }],
        },
        source_pointer: "/Scene/Key_Visual_Elements/1",
      },
    ],
    timeline: [
      { event_id: "beat-0001", at_sec: 0, action: "look_at", actor_id: "camera_wearer", target_id: "black_wheeled_office_chair", description: "Look toward the office chair.", source_pointer: "/Scene/Actions/0" },
      { event_id: "beat-0002", at_sec: 2, action: "drag", actor_id: "camera_wearer", target_id: "black_wheeled_office_chair", description: "Drag the chair away from the cabinet.", source_pointer: "/Scene/Actions/1" },
      { event_id: "beat-0003", at_sec: 5, action: "brace", actor_id: "camera_wearer", target_id: "metal_storage_cabinet", description: "Brace against the cabinet.", source_pointer: "/Scene/Actions/2" },
      { event_id: "beat-0004", at_sec: 9, action: "pause", actor_id: "camera_wearer", target_id: null, description: "Pause and hold position.", source_pointer: "/Scene/Actions/3" },
    ],
    unresolved: [
      { mapping_id: "unresolved-action-0002", kind: "action", source_pointer: "/Scene/Actions/1", reason_code: "unsupported_action", message: "Action 'drag' requires an explicit verified runtime adapter", blocking: true, candidates: [] },
      { mapping_id: "unresolved-action-0003", kind: "action", source_pointer: "/Scene/Actions/2", reason_code: "unsupported_action", message: "Action 'brace' requires an explicit verified runtime adapter", blocking: true, candidates: [] },
    ],
  };
}

function buildFixture() {
  return {
    schema: "vista-scene-build-plan-response/v1",
    import_artifact_id: "vim_fixture",
    profile_id: "mmg_040_static_office_v1",
    state: "planned",
    last_result: null,
    updated_at: null,
    plan: {
      schema: "vista-scene-build-plan/v1",
      plan_id: "vsp-aaaaaaaaaaaaaaaaaaaaaaaa",
      scene_id: "mmg_040@bbbbbbbbbbbbbbbb",
      layout_revision: "mmg-040-layout-r1",
      asset_snapshot_id: "assets-r1",
      content_revision: "ue-content-r1",
      actors: [
        { actor_id: "chair", actor_name: "VISTA_mmg040_chair" },
        { actor_id: "cabinet", actor_name: "VISTA_mmg040_cabinet" },
      ],
    },
  };
}

test("VISTA preview is allowlisted, reviewable, and explicitly confirmed before commit", async ({ page }) => {
  const previewBodies = [];
  const commitBodies = [];
  const statusIds = [];
  const planBodies = [];
  const preflightBodies = [];
  const executeBodies = [];

  await page.route(
    (url) => url.origin === "http://127.0.0.1:4182" && url.pathname.startsWith("/api/"),
    (route) => route.fulfill({ contentType: "application/json", json: {} }),
  );
  await page.route("**/api/session/acquire", (route) => route.fulfill({
    contentType: "application/json",
    json: { dev: true },
  }));
  await page.route("**/api/vista/imports/preview", async (route) => {
    previewBodies.push(route.request().postDataJSON());
    await route.fulfill({ contentType: "application/json", json: previewFixture() });
  });
  await page.route(
    (url) => url.pathname === "/api/vista/imports/vim_fixture",
    async (route) => {
      statusIds.push(new URL(route.request().url()).pathname.split("/").pop());
      await route.fulfill({
        contentType: "application/json",
        json: {
          schema: "vista-import-artifact/v1",
          artifact_id: "vim_fixture",
          run_id: "vim_fixture",
          scene_id: "vis_fixture",
          status: "committed",
          created_at: "2026-07-14T00:00:00.000Z",
        },
      });
    },
  );
  await page.route("**/api/vista/imports/vim_fixture/build/plan", async (route) => {
    planBodies.push(route.request().postDataJSON());
    await route.fulfill({ contentType: "application/json", json: buildFixture() });
  });
  await page.route("**/api/vista/imports/vim_fixture/build/preflight", async (route) => {
    preflightBodies.push(route.request().postDataJSON());
    await route.fulfill({
      contentType: "application/json",
      json: {
        schema: "vista-scene-build-service-preflight/v1",
        plan_id: buildFixture().plan.plan_id,
        ready: true,
        preflight: { ready: true, assets: [{}, {}], actors: [{}, {}] },
      },
    });
  });
  await page.route("**/api/vista/imports/vim_fixture/build/execute", async (route) => {
    executeBodies.push(route.request().postDataJSON());
    await route.fulfill({
      contentType: "application/json",
      json: {
        schema: "vista-scene-build-service-execution/v1",
        plan_id: buildFixture().plan.plan_id,
        status: "succeeded",
        result: {
          schema: "vista-scene-build-result/v1",
          plan_id: buildFixture().plan.plan_id,
          scene_id: buildFixture().plan.scene_id,
          status: "succeeded",
          rollback: { state: "not_required", deleted_actor_names: [] },
        },
      },
    });
  });
  await page.route(
    (url) => url.pathname === "/api/vista/imports",
    async (route) => {
      commitBodies.push(route.request().postDataJSON());
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        json: {
          schema: "vista-import-artifact/v1",
          artifact_id: "vim_fixture",
          run_id: "vim_fixture",
          scene_id: "vis_fixture",
          status: "committed",
          created: true,
          scene_spec: previewFixture(),
        },
      });
    },
  );

  await page.goto("/");
  await page.locator(".sw-drawer-header").click();
  await page.getByRole("button", { name: "VISTA Import" }).click();

  await expect(page.getByTestId("vista-import-panel")).toBeVisible();
  await expect(page.getByLabel("VISTA dataset revision")).toHaveValue("round1_reviewed_latest");
  await expect(page.getByLabel("VISTA sample")).toHaveValue("mmg_040");
  await expect(page.getByLabel("VISTA selected attempt")).toHaveValue("7");
  await expect(page.getByText("Filesystem paths are not accepted.", { exact: false })).toBeVisible();

  await page.getByTestId("vista-import-preview").click();
  await expect(page.getByTestId("vista-import-preview-result")).toBeVisible();
  expect(previewBodies).toEqual([{
    datasetRevision: "round1_reviewed_latest",
    sampleId: "mmg_040",
    attempt: 7,
  }]);
  expect(Object.keys(previewBodies[0]).some((key) => /path|file|dir/i.test(key))).toBe(false);

  for (const second of [0, 2, 5, 9]) {
    await expect(page.getByTestId(`vista-import-beat-${second}`)).toBeVisible();
  }
  await expect(page.getByText("black wheeled office chair", { exact: true })).toBeVisible();
  await expect(page.getByText("No verified asset selected", { exact: true })).toBeVisible();
  await expect(page.getByText("/Game/Props/SM_Cabinet", { exact: true })).toBeVisible();
  await expect(page.getByText("Unsupported", { exact: true })).toHaveCount(2);
  await expect(page.getByText("vista-simworld-scene/v1", { exact: true })).toBeVisible();
  await expect(page.getByText("reconstruction_only", { exact: true })).toBeVisible();

  expect(commitBodies).toHaveLength(0);
  await page.getByTestId("vista-import-prepare-commit").click();
  await expect(page.getByText("Commit VISTA scene artifact", { exact: true })).toBeVisible();
  await expect(page.getByText("This does not start Unreal or mutate the active scene.", { exact: false })).toBeVisible();
  await expect(page.getByText("no basic geometry fallback will be inserted.", { exact: false })).toBeVisible();
  expect(commitBodies).toHaveLength(0);

  await page.getByTestId("vista-import-confirm-commit").click();
  await expect(page.getByTestId("vista-import-artifact-status")).toContainText("Scene artifact committed");
  await expect(page.getByTestId("vista-import-artifact-status")).toContainText("vim_fixture");
  expect(commitBodies).toEqual(previewBodies);
  expect(statusIds).toEqual(["vim_fixture"]);

  await expect(page.getByTestId("vista-scene-build-status")).toContainText("No BuildPlan prepared");
  await page.getByTestId("vista-scene-build-prepare").click();
  await expect(page.getByTestId("vista-scene-build-status")).toContainText(buildFixture().plan.plan_id);
  expect(planBodies).toEqual([{}]);

  await page.getByTestId("vista-scene-build-preflight").click();
  await expect(page.getByTestId("vista-scene-build-preflight-ready")).toBeVisible();
  expect(preflightBodies).toEqual([{
    plan_id: buildFixture().plan.plan_id,
    profile_id: buildFixture().profile_id,
  }]);

  expect(executeBodies).toHaveLength(0);
  await page.getByTestId("vista-scene-build-execute").click();
  await expect(page.getByText("Build verified scene in Unreal", { exact: true })).toBeVisible();
  expect(executeBodies).toHaveLength(0);
  await page.getByTestId("vista-scene-build-confirm").click();
  await expect(page.getByTestId("vista-scene-build-status")).toContainText("succeeded");
  expect(executeBodies).toEqual([{
    plan_id: buildFixture().plan.plan_id,
    confirm: true,
    profile_id: buildFixture().profile_id,
  }]);
});
