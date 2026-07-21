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
      { event_id: "beat-0003-brace", at_sec: 5, action: "brace", actor_id: "camera_wearer", target_id: "metal_storage_cabinet", description: "Brace against the cabinet and lift one foot.", source_pointer: "/Scene/Actions/2/brace" },
      { event_id: "beat-0003-lift_foot", at_sec: 5, action: "lift_foot", actor_id: "camera_wearer", target_id: "metal_storage_cabinet", description: "Brace against the cabinet and lift one foot.", source_pointer: "/Scene/Actions/2/lift_foot" },
      { event_id: "beat-0004", at_sec: 9, action: "pause", actor_id: "camera_wearer", target_id: null, description: "Pause and hold position.", source_pointer: "/Scene/Actions/3" },
    ],
    unresolved: [
      { mapping_id: "unresolved-action-0002", kind: "action", source_pointer: "/Scene/Actions/1", reason_code: "unsupported_action", message: "Action 'drag' requires an explicit verified runtime adapter", blocking: true, candidates: [] },
      { mapping_id: "unresolved-action-0003-brace", kind: "action", source_pointer: "/Scene/Actions/2/brace", reason_code: "unsupported_action", message: "Action 'brace' requires an explicit verified runtime adapter", blocking: true, candidates: [] },
      { mapping_id: "unresolved-action-0003-lift_foot", kind: "action", source_pointer: "/Scene/Actions/2/lift_foot", reason_code: "unsupported_action", message: "Action 'lift_foot' requires an explicit verified runtime adapter", blocking: true, candidates: [] },
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

const ANIMATION_IDS = Object.freeze({
  preflightId: "vap-bbbbbbbbbbbbbbbbbbbbbbbb",
  timelineId: "vtl-cccccccccccccccccccccccc",
  programId: "vag-dddddddddddddddddddddddd",
  firstRunId: "vtr-eeeeeeeeeeeeeeeeeeeeeeee",
  replayRunId: "vtr-ffffffffffffffffffffffff",
});

function animationPreflightFixture() {
  return {
    schema: "vista-animation-timeline-preflight-service/v1",
    import_artifact_id: "vim_fixture",
    profile_id: buildFixture().profile_id,
    plan_id: buildFixture().plan.plan_id,
    scene_id: buildFixture().plan.scene_id,
    preflight_id: ANIMATION_IDS.preflightId,
    timeline_id: ANIMATION_IDS.timelineId,
    program_id: ANIMATION_IDS.programId,
    ready: true,
    expires_at: "2030-07-21T00:05:00.000Z",
    runtime_revision_digest: "1".repeat(64),
    fps: 30,
    duration_sec: 12,
    events: [
      { event_id: "beat-0001", order: 0, at_sec: 0, at_frame: 0, frame_order: 0, action: "look_at" },
      { event_id: "beat-0002", order: 1, at_sec: 2, at_frame: 60, frame_order: 0, action: "drag" },
      { event_id: "beat-0003-brace", order: 2, at_sec: 5, at_frame: 150, frame_order: 0, action: "brace" },
      { event_id: "beat-0003-lift_foot", order: 3, at_sec: 5, at_frame: 150, frame_order: 1, action: "lift_foot" },
      { event_id: "beat-0004", order: 4, at_sec: 9, at_frame: 270, frame_order: 0, action: "pause" },
    ],
  };
}

function animationStartFixture(runId, replayOf = null) {
  const preflight = animationPreflightFixture();
  return {
    schema: "vista-animation-timeline-start/v1",
    import_artifact_id: "vim_fixture",
    profile_id: preflight.profile_id,
    plan_id: preflight.plan_id,
    scene_id: preflight.scene_id,
    preflight_id: preflight.preflight_id,
    timeline_id: preflight.timeline_id,
    program_id: preflight.program_id,
    operation_id: runId,
    run_id: runId,
    status: "pending",
    replay_of: replayOf,
  };
}

function animationStatusFixture(runId, state) {
  const preflight = animationPreflightFixture();
  const terminal = ["completed", "failed", "cancelled"].includes(state);
  const completed = state === "completed";
  const running = state === "running";
  const events = preflight.events.map((event, index) => ({
    event_id: event.event_id,
    planned_sec: event.at_sec,
    actual_sec: completed || index === 0 ? event.at_sec + (index + 1) * 0.004 : null,
    engine_time: completed || index === 0 ? 100 + index : null,
    drift_ms: completed || index === 0 ? (index + 1) * 4 : null,
    state: completed || index === 0 ? "completed" : running && index === 1 ? "running" : state === "cancelled" ? "cancelled" : "pending",
    attempt: completed || index < 2 ? 1 : 0,
    started_at: completed || index < 2 ? "2026-07-21T00:00:00.000Z" : null,
    ended_at: completed || index === 0 ? "2026-07-21T00:00:01.000Z" : null,
    cleanup_state: terminal ? "completed" : "not_required",
  }));
  const checkpoints = terminal
    ? preflight.events.filter((_event, index) => completed || index === 0).map((event, index) => ({
      checkpoint_id: `vek-${String(index + 1).padStart(20, "0")}`,
      phase: "after",
      event_id: event.event_id,
      action: event.action,
      attempt: 1,
      at_sec: event.at_sec,
      at_frame: event.at_frame,
      frame_order: event.frame_order,
      captured_at: "2026-07-21T00:00:12.000Z",
      evidence: [{
        kind: "screenshot",
        evidence_id: `screenshot-${index + 1}`,
        artifact_ref: `animation/${runId}/event-${index + 1}.png`,
        sha256: "2".repeat(64),
        assertion: completed ? "pass" : null,
      }],
    }))
    : [];
  return {
    ...animationStartFixture(runId, runId === ANIMATION_IDS.replayRunId ? ANIMATION_IDS.firstRunId : null),
    schema: "vista-animation-timeline-status/v1",
    status: state,
    run: {
      schema: "vista-animation-timeline-run-status/v1",
      run_id: runId,
      timeline_id: preflight.timeline_id,
      scene_revision: preflight.scene_id,
      duration_sec: 12,
      state,
      created_at: "2026-07-21T00:00:00.000Z",
      started_at: "2026-07-21T00:00:00.000Z",
      ended_at: terminal ? "2026-07-21T00:00:12.000Z" : null,
      events,
      cleanup: {
        state: terminal ? "completed" : "pending",
        pending_items: terminal ? [] : ["beat-0002"],
        confirmed_stopped: state === "cancelled",
        ended_pie: terminal,
      },
    },
    evidence: terminal ? {
      schema: "vista-animation-evidence/v1",
      manifest_id: `vae-${runId.slice(4)}`,
      program_id: preflight.program_id,
      run_id: runId,
      timeline_id: preflight.timeline_id,
      scene_revision: preflight.scene_id,
      content_revision: "ue-content-r1",
      fps: 30,
      duration_sec: 12,
      run_state: state,
      created_at: "2026-07-21T00:00:00.000Z",
      finalized_at: "2026-07-21T00:00:12.000Z",
      run_digest: "3".repeat(64),
      checkpoints,
      coverage: {
        required_event_count: preflight.events.length,
        completed_event_count: completed ? preflight.events.length : 1,
        checkpoint_count: checkpoints.length,
        missing: completed ? [] : ["beat-0002:after"],
        complete: completed,
      },
    } : null,
    error: null,
    created_at: "2026-07-21T00:00:00.000Z",
    updated_at: "2026-07-21T00:00:12.000Z",
  };
}

test("VISTA preview is allowlisted, reviewable, and explicitly confirmed before commit", async ({ page }) => {
  const previewBodies = [];
  const commitBodies = [];
  const statusIds = [];
  const planBodies = [];
  const preflightBodies = [];
  const executeBodies = [];
  const animationPreflightBodies = [];
  const animationStartBodies = [];
  const animationStopBodies = [];
  const animationReplayBodies = [];
  let stopRequested = false;

  await page.route(
    (url) => url.origin === "http://127.0.0.1:4182" && url.pathname.startsWith("/api/"),
    (route) => route.fulfill({ contentType: "application/json", json: {} }),
  );
  await page.route("**/api/session/heartbeat", (route) => route.fulfill({
    status: 401,
    contentType: "application/json",
    json: { error: "No active fixture session" },
  }));
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
  await page.route("**/api/vista/imports/vim_fixture/animation/preflight", async (route) => {
    animationPreflightBodies.push(route.request().postDataJSON());
    await route.fulfill({ contentType: "application/json", json: animationPreflightFixture() });
  });
  await page.route("**/api/vista/imports/vim_fixture/animation/start", async (route) => {
    animationStartBodies.push(route.request().postDataJSON());
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      json: animationStartFixture(ANIMATION_IDS.firstRunId),
    });
  });
  await page.route(
    (url) => /^\/api\/vista\/imports\/vim_fixture\/animation\/runs\/[^/]+$/.test(url.pathname),
    async (route) => {
      const runId = new URL(route.request().url()).pathname.split("/").pop();
      const state = runId === ANIMATION_IDS.replayRunId ? "completed" : stopRequested ? "cancelled" : "running";
      await route.fulfill({ contentType: "application/json", json: animationStatusFixture(runId, state) });
    },
  );
  await page.route("**/api/vista/imports/vim_fixture/animation/runs/*/stop", async (route) => {
    animationStopBodies.push(route.request().postDataJSON());
    stopRequested = true;
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      json: animationStatusFixture(ANIMATION_IDS.firstRunId, "stopping"),
    });
  });
  await page.route("**/api/vista/imports/vim_fixture/animation/runs/*/replay", async (route) => {
    animationReplayBodies.push(route.request().postDataJSON());
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      json: animationStartFixture(ANIMATION_IDS.replayRunId, ANIMATION_IDS.firstRunId),
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

  for (const second of [0, 2, 9]) {
    await expect(page.getByTestId(`vista-import-beat-${second}`)).toBeVisible();
  }
  await expect(page.getByTestId("vista-import-beat-5")).toHaveCount(2);
  await expect(page.getByText("black wheeled office chair", { exact: true })).toBeVisible();
  await expect(page.getByText("No verified asset selected", { exact: true })).toBeVisible();
  await expect(page.getByText("/Game/Props/SM_Cabinet", { exact: true })).toBeVisible();
  await expect(page.getByText("Unsupported", { exact: true })).toHaveCount(3);
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

  await expect(page.getByTestId("vista-animation-workbench")).toBeVisible();
  await expect(page.getByTestId("vista-animation-readiness")).toContainText("Not checked");
  await expect(page.getByTestId("vista-animation-replay")).toHaveCount(0);

  await page.getByTestId("vista-animation-preflight").click();
  await expect(page.getByTestId("vista-animation-readiness")).toContainText("Verified");
  await expect(page.getByTestId("vista-animation-readiness")).toContainText("30 fps");
  expect(animationPreflightBodies).toEqual([{
    plan_id: buildFixture().plan.plan_id,
    profile_id: buildFixture().profile_id,
  }]);

  expect(animationStartBodies).toHaveLength(0);
  await page.getByTestId("vista-animation-start").click();
  await expect(page.getByText("Start verified 12-second execution", { exact: true })).toBeVisible();
  await expect(page.getByText(ANIMATION_IDS.programId, { exact: true })).toBeVisible();
  expect(animationStartBodies).toHaveLength(0);
  await page.getByTestId("vista-animation-confirm").click();

  await expect(page.getByTestId("vista-animation-workbench")).toContainText("running");
  await expect(page.getByTestId("vista-animation-event-beat-0001")).toContainText("look_at");
  await expect(page.getByTestId("vista-animation-drift")).toContainText("4 ms");
  await expect(page.getByTestId("vista-animation-stop")).toBeVisible();
  await expect(page.getByTestId("vista-animation-replay")).toHaveCount(0);
  expect(animationStartBodies).toEqual([{
    plan_id: buildFixture().plan.plan_id,
    preflight_id: ANIMATION_IDS.preflightId,
    timeline_id: ANIMATION_IDS.timelineId,
    program_id: ANIMATION_IDS.programId,
    confirm: true,
  }]);

  await page.getByTestId("vista-animation-stop").click();
  await expect(page.getByTestId("vista-animation-workbench")).toContainText("cancelled");
  await expect(page.getByTestId("vista-animation-evidence")).toContainText("Evidence manifest");
  await expect(page.getByTestId("vista-animation-replay")).toBeVisible();
  expect(animationStopBodies).toEqual([{}]);

  await page.getByTestId("vista-animation-replay").click();
  await expect(page.getByText("Replay verified 12-second execution", { exact: true })).toBeVisible();
  expect(animationPreflightBodies).toHaveLength(2);
  expect(animationReplayBodies).toHaveLength(0);
  await page.getByTestId("vista-animation-confirm").click();
  await expect(page.getByTestId("vista-animation-workbench")).toContainText("completed");
  await expect(page.getByTestId("vista-animation-evidence")).toContainText("5/5 events");
  expect(animationReplayBodies).toEqual([{
    plan_id: buildFixture().plan.plan_id,
    preflight_id: ANIMATION_IDS.preflightId,
    timeline_id: ANIMATION_IDS.timelineId,
    program_id: ANIMATION_IDS.programId,
    confirm: true,
  }]);
});
