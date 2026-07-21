import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

import {
  commitVistaImport,
  fetchVistaAnimationTimelineStatus,
  fetchVistaImportStatus,
  preflightVistaAnimationTimeline,
  previewVistaImport,
  replayVistaAnimationTimeline,
  startVistaAnimationTimeline,
  stopVistaAnimationTimeline,
} from "../../src/api/appApi.js";
import {
  createVistaImportRequest,
  DEFAULT_VISTA_IMPORT_SELECTION,
  formatVistaTimecode,
  summarizeVistaPreview,
} from "../../src/features/vista/vistaImportModel.js";

const require = createRequire(import.meta.url);
const { createVistaImporter } = require("../../server/vista-importer.js");
const CHECKSUM = "a".repeat(64);

function sceneFixture() {
  return {
    schema: "vista-simworld-scene/v1",
    scene_id: "mmg_040@aaaaaaaaaaaaaaaa",
    profile: "reconstruction",
    privilege: {
      classification: "reconstruction_only",
      evaluation_input_allowed: false,
    },
    provenance: {
      importer_name: "vista-scene-importer",
      importer_version: "1.0.0",
      source_checksum: CHECKSUM,
      bundle_id: "round1_reviewed_latest:mmg_040:sora2:attempt_007",
    },
    source: {
      dataset_revision: "round1_reviewed_latest",
      source_row_id: "mmg_040__multimodal_grounded::attempt_007",
      visual_id: "mmg_040",
      case_scope: "multimodal_grounded_safety_040",
      scenario_type: "multimodal_grounded",
      attempt: { provider: "sora2", index: 7, selected: true },
      source_checksum: CHECKSUM,
    },
    duration_sec: 12,
    timeline: [
      { event_id: "beat-0001", at_sec: 0, action: "look_at", actor_id: "camera_wearer", target_id: "chair", description: "Look at the chair.", source_pointer: "/Scene/Actions/0" },
      { event_id: "beat-0002", at_sec: 2, action: "drag", actor_id: "camera_wearer", target_id: "chair", description: "Drag the chair.", source_pointer: "/Scene/Actions/1" },
      { event_id: "beat-0003", at_sec: 5, action: "brace", actor_id: "camera_wearer", target_id: "cabinet", description: "Brace against the cabinet.", source_pointer: "/Scene/Actions/2" },
      { event_id: "beat-0004", at_sec: 9, action: "pause", actor_id: "camera_wearer", target_id: null, description: "Pause.", source_pointer: "/Scene/Actions/3" },
    ],
    entities: [
      { id: "chair", semantic_query: "black wheeled office chair", required: true, asset_binding: null, source_pointer: "/Scene/Key_Visual_Elements/0" },
      {
        id: "cabinet",
        semantic_query: "metal storage cabinet",
        required: true,
        asset_binding: { snapshot_id: "snapshot-1", asset_id: "cabinet-1", ue_path: "/Game/Props/SM_Cabinet", confidence: 0.91 },
        asset_resolution: {
          query: "metal storage cabinet",
          snapshot_id: "snapshot-1",
          selected_by: "automatic",
          candidates: [{ rank: 1, asset_id: "cabinet-1", ue_path: "/Game/Props/SM_Cabinet", confidence: 0.91 }],
        },
        source_pointer: "/Scene/Key_Visual_Elements/1",
      },
    ],
    unresolved: [
      { mapping_id: "unresolved-action-0002", kind: "action", reason_code: "unsupported_action", message: "Drag requires a runtime adapter", blocking: true, source_pointer: "/Scene/Actions/1", candidates: [] },
      { mapping_id: "unresolved-action-0003", kind: "action", reason_code: "unsupported_action", message: "Brace requires a runtime adapter", blocking: true, source_pointer: "/Scene/Actions/2", candidates: [] },
    ],
  };
}

test("allowlisted selection creates an exact path-free API request", () => {
  const request = createVistaImportRequest(DEFAULT_VISTA_IMPORT_SELECTION);
  assert.deepEqual(request, {
    datasetRevision: "round1_reviewed_latest",
    sampleId: "mmg_040",
    attempt: 7,
  });
  assert.equal(Object.keys(request).some((key) => /path|file|dir/i.test(key)), false);
  assert.throws(
    () => createVistaImportRequest({ datasetRevision: "../private", sampleId: "mmg_040", attempt: 7 }),
    /allowlisted VISTA source/,
  );
});

test("preview view model preserves the 0/2/5/9 beats and explicit unresolved state", () => {
  const request = createVistaImportRequest(DEFAULT_VISTA_IMPORT_SELECTION);
  const summary = summarizeVistaPreview(sceneFixture(), request);
  assert.equal(summary.durationSec, 12);
  assert.deepEqual(summary.timeline.map((beat) => beat.atSec), [0, 2, 5, 9]);
  assert.deepEqual(summary.timeline.map((beat) => beat.timecode), ["00:00", "00:02", "00:05", "00:09"]);
  assert.deepEqual(summary.timeline.filter((beat) => beat.unsupported).map((beat) => beat.action), ["drag", "brace"]);
  assert.equal(summary.entities[0].resolved, false);
  assert.equal(summary.entities[0].path, null);
  assert.equal(summary.entities[1].path, "/Game/Props/SM_Cabinet");
  assert.equal(summary.entities[1].assetId, "cabinet-1");
  assert.equal(summary.entities[1].candidates.length, 1);
  assert.equal(summary.entities[1].selectedBy, "automatic");
  assert.equal(summary.entities[1].confidence, 0.91);
  assert.equal(summary.checks.every((check) => check.passed), true);
  assert.equal(summary.canCommit, true);
  assert.equal(formatVistaTimecode(2.25), "00:02.25");
});

test("preview accepts deterministic same-second compound actions and rejects reversed order", () => {
  const request = createVistaImportRequest(DEFAULT_VISTA_IMPORT_SELECTION);
  const scene = sceneFixture();
  scene.timeline.splice(2, 1,
    {
      ...scene.timeline[2],
      event_id: "beat-0003-brace",
      source_pointer: "/Scene/Actions/2/brace",
    },
    {
      ...scene.timeline[2],
      event_id: "beat-0003-lift_foot",
      action: "lift_foot",
      source_pointer: "/Scene/Actions/2/lift_foot",
    });

  const ordered = summarizeVistaPreview(scene, request);
  assert.equal(ordered.checks.find((check) => check.id === "timeline").passed, true);
  assert.equal(ordered.canCommit, true);
  assert.deepEqual(ordered.timeline.map((event) => event.atSec), [0, 2, 5, 5, 9]);

  const reversed = structuredClone(scene);
  [reversed.timeline[2], reversed.timeline[3]] = [reversed.timeline[3], reversed.timeline[2]];
  const rejected = summarizeVistaPreview(reversed, request);
  assert.equal(rejected.checks.find((check) => check.id === "timeline").passed, false);
  assert.equal(rejected.canCommit, false);
});

test("UI summary accepts the real local mmg_040 importer contract", async () => {
  const fixtureRoot = fileURLToPath(new URL("../../server/tests/fixtures/vista/mmg_040", import.meta.url));
  const importer = createVistaImporter({
    registry: {
      round1_reviewed_latest: { root: fixtureRoot },
    },
  });
  const request = createVistaImportRequest(DEFAULT_VISTA_IMPORT_SELECTION);
  const preview = await importer.preview(request);
  const summary = summarizeVistaPreview(preview, request);

  assert.equal(summary.canCommit, true);
  assert.equal(summary.durationSec, 12);
  assert.deepEqual(summary.timeline.map((beat) => beat.atSec), [0, 2, 5, 5, 9]);
  assert.deepEqual(summary.timeline.filter((beat) => beat.atSec === 5).map((beat) => beat.action), ["brace", "lift_foot"]);
  assert.equal(summary.entities.length, 4);
  assert.equal(summary.entities.every((entity) => entity.path === null), true);
  assert.equal(summary.unresolved.some((item) => item.reason_code === "no_asset_match"), true);
  assert.equal(summary.unresolved.some((item) => item.reason_code === "unsupported_action"), true);
});

test("VISTA import API helpers use preview, commit, and encoded status contracts", async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, options });
    if (url.endsWith("/preview")) return Response.json(sceneFixture());
    if (options.method === "POST") return Response.json({ artifact_id: "vim_fixture", status: "committed", created: true }, { status: 201 });
    return Response.json({ artifact_id: "vim_fixture", status: "committed" });
  };

  const request = createVistaImportRequest(DEFAULT_VISTA_IMPORT_SELECTION);
  await previewVistaImport(request);
  await commitVistaImport(request);
  await fetchVistaImportStatus("vim_fixture");

  assert.deepEqual(calls.map((call) => call.url), [
    "/api/vista/imports/preview",
    "/api/vista/imports",
    "/api/vista/imports/vim_fixture",
  ]);
  assert.deepEqual(JSON.parse(calls[0].options.body), request);
  assert.deepEqual(JSON.parse(calls[1].options.body), request);
  assert.equal(calls[2].options.body, undefined);
});

test("VISTA import API errors retain typed public failure details", async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async () => Response.json({
    error: "Dataset revision is unavailable",
    code: "VISTA_DATASET_UNAVAILABLE",
    retryable: true,
  }, { status: 503 });

  await assert.rejects(
    previewVistaImport(createVistaImportRequest(DEFAULT_VISTA_IMPORT_SELECTION)),
    (error) => (
      error.name === "VistaImportApiError"
      && error.status === 503
      && error.code === "VISTA_DATASET_UNAVAILABLE"
      && error.retryable === true
    ),
  );
});

test("VISTA animation API helpers preserve exact confirmations and encoded run identities", async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, options });
    return Response.json({ status: options.method === "POST" ? "pending" : "running" });
  };
  const importId = "vim_fixture/encoded";
  const animationRunId = "vtr fixture/encoded";
  const preflight = {
    plan_id: "vsp-aaaaaaaaaaaaaaaaaaaaaaaa",
    preflight_id: "vap-bbbbbbbbbbbbbbbbbbbbbbbb",
    timeline_id: "vtl-cccccccccccccccccccccccc",
    program_id: "vag-dddddddddddddddddddddddd",
  };

  await preflightVistaAnimationTimeline(importId, preflight.plan_id, "profile-1");
  await startVistaAnimationTimeline(importId, preflight);
  await fetchVistaAnimationTimelineStatus(importId, animationRunId);
  await stopVistaAnimationTimeline(importId, animationRunId);
  await replayVistaAnimationTimeline(importId, animationRunId, preflight);

  const encodedImportId = encodeURIComponent(importId);
  const encodedAnimationRunId = encodeURIComponent(animationRunId);
  assert.deepEqual(calls.map((call) => call.url), [
    `/api/vista/imports/${encodedImportId}/animation/preflight`,
    `/api/vista/imports/${encodedImportId}/animation/start`,
    `/api/vista/imports/${encodedImportId}/animation/runs/${encodedAnimationRunId}`,
    `/api/vista/imports/${encodedImportId}/animation/runs/${encodedAnimationRunId}/stop`,
    `/api/vista/imports/${encodedImportId}/animation/runs/${encodedAnimationRunId}/replay`,
  ]);
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    plan_id: preflight.plan_id,
    profile_id: "profile-1",
  });
  const confirmation = {
    plan_id: preflight.plan_id,
    preflight_id: preflight.preflight_id,
    timeline_id: preflight.timeline_id,
    program_id: preflight.program_id,
    confirm: true,
  };
  assert.deepEqual(JSON.parse(calls[1].options.body), confirmation);
  assert.equal(calls[2].options.body, undefined);
  assert.deepEqual(JSON.parse(calls[3].options.body), {});
  assert.deepEqual(JSON.parse(calls[4].options.body), confirmation);
});
